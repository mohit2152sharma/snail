"""MultiAgentBridge — one client socket ⇄ N live Gemini agents (host + echo + translate).

The example's own runtime: ``snail.transport.ClientBridge`` is single-connection, so this
composes the multi-agent story from primitives —

* two ``AgentConnection``s from the pool, one shared ``Router`` + ``ToolCallRegistry``;
* one ``AudioPipeline`` whose ``FanoutBus`` + ``OutputGate`` are the *same* instances the
  Router drives (so the Router's subscribe/token moves take effect on the audio plane);
* one ``Session`` per connection sharing the Router — each connection's tool results feed
  the shared routing decision, which is what flips the active agent;
* the client leg speaks the frontend contract: binary = Opus, text = JSON control/events.

Only the token-holding agent's output audio is pushed to egress; on demote the ex-active
is unsubscribed from user audio so the idle agent neither hears nor speaks until promoted
back (the Router re-subscribes it on handoff).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

log = logging.getLogger("multiagent")

from snail.audio import (
    FRAME_LEN,
    AudioPipeline,
    AudioSource,
    EnergyVad,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
    VadEvent,
)
from snail.audio.opus_codec import OpusCodec
from snail.audio.soxr_backend import SoxrResampleBackend


class _RustVad:
    """Adapt ``snail_rs.EnergyVad`` to the Python ``EnergyVad`` surface used by the bridge.

    Feeds each interior frame as PCM16LE bytes (``ndarray.tobytes()`` — cheap, no per-sample
    Python loop) and maps the Rust string event back to :class:`VadEvent`. Behaviour is
    byte-identical to the Python VAD (tests/test_rust_parity.py); only the runtime differs.
    """

    _MAP = {"none": VadEvent.NONE, "start": VadEvent.START, "end": VadEvent.END}

    def __init__(self, kwargs: dict) -> None:
        import snail_rs  # imported lazily so the extension is optional

        self._inner = snail_rs.EnergyVad(**kwargs)

    def push(self, frame) -> VadEvent:
        return self._MAP[self._inner.push(frame.tobytes())]

    def reset(self) -> None:
        self._inner.reset()
from snail.context import EventLog, Item, Role
from snail.registry import ToolCallRegistry
from snail.router import (
    OutputGate,
    Router,
    RoutingAction,
    RoutingDecision,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
    Seam,
)
from snail.session import Session
from snail.tools import ToolRegistry
from snail.vendor import (
    Interrupted,
    MediaChunk,
    RealtimeControl,
    ResponseModality,
    TurnComplete,
)

from .agents import ECHO_ID, HOST_ID, POOL_KEY, REANCHOR, SPECS, TRANSLATE_ID
from .events import active_agent_changed, error as err_event, to_client_json, turn_ttfb
from .routing import build_policy
from .tools import echo_tools, host_tools


def _tools_for(cid: str) -> ToolRegistry:
    if cid == HOST_ID:
        return host_tools()
    if cid == ECHO_ID:
        return echo_tools()
    return ToolRegistry()  # translate: no tools (model constraint)


class MultiAgentBridge:
    """Pump between one FastAPI WebSocket and the multi-agent runtime.

    ``agent_ids`` is the ordered set of agents to run this session (host first = default
    active); ``pools`` maps a pool-key (see ``agents.POOL_KEY``) to a ConnectionPool.
    """

    def __init__(self, *, socket, pools: dict, agent_ids) -> None:
        self._socket = socket
        self._pools = pools
        self._agent_ids = list(agent_ids)
        self._conns: dict[str, object] = {}
        self._pool_of: dict[str, object] = {}
        self._sessions: dict[str, Session] = {}
        self._tasks: list[asyncio.Task] = []
        self._muted = False
        self._closing = False
        self._mic_bytes = 0
        self._mic_logged = 0
        self._out_bytes = 0
        # --- endpointing (server-side VAD in manual-activity mode) -------------
        # The bridge, not Gemini, decides end-of-speech: an energy VAD + hangover sends
        # ACTIVITY_END after `hangover` frames of sub-threshold audio instead of Gemini's
        # flat 800ms wait, cutting the dominant per-turn TTFB term.
        #
        # DEFAULT IS MAXIMALLY AGGRESSIVE (1 frame / 10ms) to meet the 50% TTFB target —
        # live paired A/B measures a 50.9% median cut at this setting. It trades all
        # pause-tolerance for latency and WILL clip mid-sentence pauses (barge-in). For a
        # conversational profile use SNAIL_VAD_HANGOVER_FRAMES=15 (150ms, ~32% cut) or 30
        # (300ms, ~24%). See docs/superpowers/2026-07-25-live-ttfb-benchmark.md.
        hangover = int(os.environ.get("SNAIL_VAD_HANGOVER_FRAMES", "1"))
        vad_kw = dict(
            hangover_frames=hangover,
            start_frames=int(os.environ.get("SNAIL_VAD_START_FRAMES", "3")),
            # margin 4.0: live testing showed 3.0 let agent echo trip spurious barge-ins.
            margin=float(os.environ.get("SNAIL_VAD_MARGIN", "4.0")),
        )
        # SNAIL_RUST_VAD=1 routes the TTFB-critical endpoint decision through the Rust
        # `snail_rs.EnergyVad` (byte-identical to the Python one — see tests/test_rust_parity.py).
        # The deterministic Rust hot path has no GIL/GC tail pauses, so the aggressive 10ms
        # hangover runs without the jitter that risks clipping speech on the Python path.
        if os.environ.get("SNAIL_RUST_VAD") == "1":
            self._vad = _RustVad(vad_kw)
        else:
            self._vad = EnergyVad(**vad_kw)
        self._hangover_s = hangover * 0.010  # frames → seconds (10ms/frame)
        self._in_speech = False
        # Half-duplex echo guard: on speakers (no headphones) the agent's own voice loops
        # back through the mic and trips the energy VAD → false ACTIVITY_START → self-
        # interrupt. While the agent is producing audio (+ a short tail for the playout/echo
        # to drain) the mic is gated: no VAD, no forward. The explicit Barge-in button still
        # works for intentional interruption. Set SNAIL_HALF_DUPLEX=0 (headphones) to disable.
        self._half_duplex = os.environ.get("SNAIL_HALF_DUPLEX", "1") != "0"
        self._echo_guard_s = float(os.environ.get("SNAIL_ECHO_GUARD_MS", "400")) / 1000.0
        self._mic_gate_until = 0.0
        # Single source of truth for the manual-activity handshake: exactly one
        # ACTIVITY_START must precede each ACTIVITY_END. Gemini rejects a double-start or
        # an end-without-start with a 1007 "Precondition check failed" that kills the
        # connection, so open/close are made idempotent (guarded on this flag).
        self._activity_open = False
        self._preroll: deque[bytes] = deque(maxlen=32)  # pre-roll / pre-open buffer
        # Don't open a manual activity until the segment carries this much real speech:
        # Gemini rejects an activity_start→activity_end that carried too little audio with
        # a 1007 precondition failure. Sub-threshold blips (noise/echo after barge-in)
        # thus never emit markers. 16kHz mono s16 = 32 bytes/ms.
        self._min_open_bytes = int(os.environ.get("SNAIL_VAD_MIN_OPEN_MS", "120")) * 32
        self._seg_bytes = 0  # audio bytes buffered in the not-yet-opened segment
        # --- per-turn TTFB instrumentation -------------------------------------
        # Now that the bridge decides end-of-speech, t0 is the last speech frame
        # (END_time − hangover), so this measures the full end-of-speech→first-byte
        # window — the target metric — server-side.
        self._ttfb_t0: float | None = None
        self._ttfb_pending = False  # armed at ACTIVITY_END until first audio byte fires
        # per-agent "you hold the token" gate: only the active agent pumps receive.
        self._active_ev: dict[str, asyncio.Event] = {
            cid: asyncio.Event() for cid in self._agent_ids
        }

        # audio plane — bus + gate are shared with the Router below.
        frames = FramePool(capacity=256, slab_samples=FRAME_LEN)
        self._bus = FanoutBus(frames)
        self._gate = OutputGate(depth=64)
        self._pipeline = AudioPipeline(
            pool=frames,
            bus=self._bus,
            resampler=LazyResampler(SoxrResampleBackend()),
            gate=self._gate,
            # egress prebuffer: 1 frame (10ms) instead of the default 3 (30ms) — shaves
            # ~20ms off first-byte-out at a small underrun-smoothing cost (Gemini bursts
            # are large, so playout re-arms immediately). Part of the per-turn TTFB budget.
            jitter=JitterBuffer(prefill_frames=1),
            codec=OpusCodec(),  # client leg: opus ⇄ 48k int16 mono
            client_rate=48000,  # opus is native 48k → no resample around the codec
        )

        chain, programmatic = build_policy()
        self._programmatic = programmatic
        self._registry = ToolCallRegistry()
        self._router = Router(
            gate=self._gate,
            bus=self._bus,
            registry=self._registry,
            policy=chain,
            on_promote=self._on_promote,
            on_demote=self._on_demote,
        )

    # --- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        await self._socket.accept()
        try:
            await self._setup()
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the client
            await self._emit(err_event("setup_failed", str(exc)))
            await self._release_conns()
            return
        named: dict[asyncio.Task, str] = {}
        for cid in self._agent_ids:
            t = asyncio.create_task(self._agent_loop(cid))
            named[t] = f"run[{cid}]"
            self._tasks.append(t)
        client = asyncio.create_task(self._pump_client())
        named[client] = "client_in"
        self._tasks.append(client)
        try:
            done, _ = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                exc = t.exception()
                if exc is not None:
                    log.error("task %s crashed: %r", named.get(t, "?"), exc, exc_info=exc)
                else:
                    log.info("task %s finished cleanly → tearing down", named.get(t, "?"))
        finally:
            await self._teardown()

    async def _agent_loop(self, cid: str) -> None:
        """Drive one connection's receive loop across turns — only while active.

        Gemini Live's ``session.receive()`` ends after each turn (docs pattern:
        ``while: async for``), and ``connection.run`` is a single ``async for``. So we
        re-enter it per turn, but *only* while this agent holds the token: an idle
        agent's ``receive()`` returns immediately, which would hot-spin, so it parks on
        its activation event until the Router promotes it.
        """
        conn = self._conns[cid]
        on_msg = self._make_on_msg(cid)
        on_audio = self._make_on_audio(cid)
        ev = self._active_ev[cid]
        while not self._closing:
            if not ev.is_set():
                await ev.wait()  # never-activated agent: park until first promote
                continue
            await conn.run(on_msg, on_audio=on_audio)  # one turn; re-enter for the next
            # If demoted (inactive) and receive() returned instantly, back off so an
            # idle drained socket can't hot-spin the loop.
            if self._router.active_id != cid and not self._closing:
                await asyncio.sleep(0.1)

    async def _setup(self) -> None:
        event_log = EventLog()
        for cid in self._agent_ids:
            pool = self._pools[POOL_KEY[cid]]
            conn = await pool.acquire(SPECS[cid])
            conn.activate()
            self._conns[cid] = conn
            self._pool_of[cid] = pool
            self._router.register_agent(
                cid,
                cid,
                modality=ResponseModality.AUDIO,
                input_source=AudioSource.USER_RAW,
                target_rate=conn.adapter.capabilities.input_sample_rate,
            )
            self._sessions[cid] = Session(
                adapter=conn.adapter,
                log=event_log,
                tools=_tools_for(cid),
                registry=self._registry,
                router=self._router,
                send=self._make_send(conn),
            )
        self._router.set_active(HOST_ID)  # host holds the token + hears user first
        self._active_ev[HOST_ID].set()  # host pumps receive from the start
        log.info("setup complete: agents=%s active=%s", list(self._conns), HOST_ID)
        await self._emit(active_agent_changed(HOST_ID))

    async def _teardown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for session in self._sessions.values():
            await session.aclose()
        await self._release_conns()

    async def _release_conns(self) -> None:
        for cid, conn in self._conns.items():
            await self._pool_of[cid].release(conn)
        self._conns.clear()

    # --- router hooks -----------------------------------------------------

    def _on_promote(self, agent_id: str, needs_flip: bool) -> None:
        # start the promoted agent's receive loop; announce the switch.
        self._active_ev[agent_id].set()
        # drop any residual audio the previous agent left in the jitter/gate rings so the
        # newly-active agent starts clean (no tail of the old agent bleeding through).
        self._pipeline.cut()
        # reset endpointing so the new agent isn't handed a dangling half-turn. The old
        # agent's activity (if any) is closed on demote; the new agent starts closed.
        self._vad.reset()
        self._in_speech = False
        self._activity_open = False
        self._seg_bytes = 0
        self._preroll.clear()
        log.info("promote → %s", agent_id)
        asyncio.create_task(self._emit(active_agent_changed(agent_id)))
        # re-anchor the agent's behavior after an excursion (non-triggering context turn).
        reanchor = REANCHOR.get(agent_id)
        if reanchor is not None:
            asyncio.create_task(self._reanchor(agent_id, reanchor))

    async def _reanchor(self, cid: str, text: str) -> None:
        conn = self._conns.get(cid)
        if conn is not None:
            try:
                await conn.send_turns([Item(role=Role.USER, text=text)], complete=False)
            except Exception:  # noqa: BLE001 - best-effort re-anchor
                pass

    def _on_demote(self, agent_id: str) -> None:
        # Drop the ex-active's user-audio subscription (re-subscribed on promote). Do NOT
        # park its receive loop: it must keep draining so a trailing post-tool turn (e.g.
        # the host's confirmation generated after the control tool) is consumed and
        # *dropped* here, not left buffered to replay when the agent is promoted back.
        self._pipeline.detach_consumer(agent_id)
        # Close any open manual-activity on the demoted connection so it isn't left
        # dangling — a later promote-back would ACTIVITY_START over it (1007).
        if self._activity_open:
            self._activity_open = False
            self._in_speech = False
            conn = self._conns.get(agent_id)
            if conn is not None:
                asyncio.create_task(
                    conn.send_realtime_control(RealtimeControl.ACTIVITY_END)
                )
        log.info("demote → %s", agent_id)

    # --- client → agents --------------------------------------------------

    async def _pump_client(self) -> None:
        while True:
            msg = await self._socket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                if not self._muted:
                    # Decode always (keep the opus decoder + resampler streaming state
                    # continuous), but while the agent is speaking, gate the mic: skip the
                    # VAD and discard the drained mic audio (half-duplex echo guard).
                    frames = self._pipeline.on_client_audio(data)
                    if self._half_duplex and time.monotonic() < self._mic_gate_until:
                        self._pipeline.drain()  # clear the fan-out rings, discard mic
                    else:
                        for f in frames:
                            ev = self._vad.push(f)
                            if ev is VadEvent.START:
                                self._in_speech = True
                            elif ev is VadEvent.END:
                                await self._end_speech()
                        await self._forward_drained()
                continue
            text = msg.get("text")
            if text is not None:
                await self._handle_control(text)
                if self._closing:
                    return

    async def _forward_drained(self) -> None:
        """Forward mic audio to the active agent, but only between VAD START/END, with an
        ACTIVITY_START/END-bracketed manual-activity turn and a pre-roll onset guard."""
        active = self._router.active_id
        for cid, chunks in self._pipeline.drain().items():
            conn = self._conns.get(cid)
            if conn is None or cid != active:
                continue
            rate = conn.adapter.capabilities.input_sample_rate
            if not getattr(conn.adapter, "manual_activity", False):
                # auto-VAD agent (e.g. translate): stream mic continuously, no markers.
                for ch in chunks:
                    await conn.send_realtime(MediaChunk.audio(ch, sample_rate=rate))
                continue
            if not self._in_speech:
                # silent: retain as pre-roll (bounded), do not forward yet
                self._preroll.extend(chunks)
                continue
            if not self._activity_open:
                # In speech but activity not open yet: buffer until the segment carries
                # >= min_open_bytes of real audio, so a spurious blip never emits markers
                # (Gemini 1007s on a too-short activity). Then open + flush the buffer.
                self._preroll.extend(chunks)
                self._seg_bytes += sum(len(c) for c in chunks)
                if self._seg_bytes < self._min_open_bytes:
                    continue
                await self._open_activity(conn)
                for pre in self._preroll:
                    await conn.send_realtime(MediaChunk.audio(pre, sample_rate=rate))
                self._preroll.clear()
                self._seg_bytes = 0
                continue  # this tick's audio already flushed via the pre-roll
            n = 0
            for ch in chunks:
                await conn.send_realtime(MediaChunk.audio(ch, sample_rate=rate))
                n += len(ch)
            self._mic_bytes += n
            if self._mic_bytes - self._mic_logged > 96000:  # ~1s @16k mono
                log.info("mic→%s: %d bytes total", cid, self._mic_bytes)
                self._mic_logged = self._mic_bytes

    async def _end_speech(self) -> None:
        """VAD END: close the user turn with ACTIVITY_END and arm the TTFB timer.

        ``t0`` is the last speech frame (END fires exactly ``hangover`` after it), so the
        logged latency is the full end-of-speech→first-byte window.
        """
        self._in_speech = False
        # discard any sub-threshold buffered segment that never opened an activity
        self._seg_bytes = 0
        self._preroll.clear()
        if self._activity_open:  # only arm TTFB for a turn we actually opened
            await self._close_activity()
            self._ttfb_t0 = time.monotonic() - self._hangover_s
            self._ttfb_pending = True

    async def _open_activity(self, conn) -> None:
        """Send ACTIVITY_START once per speech segment (idempotent — a double-start is a
        Gemini 1007 precondition failure that kills the connection)."""
        if self._activity_open:
            return
        await conn.send_realtime_control(RealtimeControl.ACTIVITY_START)
        self._activity_open = True
        log.info("→ACTIVITY_START (%s)", self._router.active_id)

    async def _close_activity(self) -> None:
        """Send ACTIVITY_END once (idempotent — an end-without-open is a 1007 too)."""
        if not self._activity_open:
            return
        self._activity_open = False
        conn = self._conns.get(self._router.active_id)
        if conn is not None:
            await conn.send_realtime_control(RealtimeControl.ACTIVITY_END)
        log.info("→ACTIVITY_END (%s)", self._router.active_id)

    async def _handle_control(self, text: str) -> None:
        try:
            ctl = json.loads(text)
        except ValueError:
            return
        t = ctl.get("type")
        if t == "mute":
            self._muted = bool(ctl.get("on"))
        elif t == "barge_in":
            self._pipeline.cut()
            self._router.barge_in()
        elif t == "handoff":
            target = ctl.get("agent_id")
            if target in self._conns and target != self._router.active_id:
                self._programmatic.push(
                    RoutingDecision(
                        action=RoutingAction.HANDOFF, target=target, seam=Seam.CUT_NOW
                    )
                )
                self._router.handle(
                    RoutingSignal(
                        event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC),
                        active_agent=self._router.agent_ref(self._router.active_id),
                    )
                )
        elif t == "text":
            active = self._conns.get(self._router.active_id)
            if active is not None:
                await active.send_turns(
                    [Item(role=Role.USER, text=ctl.get("text", ""))], complete=True
                )
        elif t == "stop":
            self._closing = True

    # --- agents → client --------------------------------------------------

    def _make_on_msg(self, cid: str):
        conn = self._conns[cid]
        session = self._sessions[cid]

        async def on_msg(raw) -> None:
            parsed = conn.adapter.parse_event(raw)  # parse once; reused by the session
            for ev in parsed:
                if isinstance(ev, Interrupted):
                    self._pipeline.cut()
                # TTFB is armed at ACTIVITY_END (_end_speech); disarm on turn end so a
                # stale timer can't fire against the next turn.
                if self._router.active_id == cid and isinstance(ev, TurnComplete):
                    self._ttfb_pending = False
                j = to_client_json(ev, agent_id=cid)
                if j is not None:
                    txt = j.get("text")
                    if txt is not None:
                        log.info("event %s from %s: %r", j["type"], cid, txt[:80])
                    else:
                        log.info("event %s from %s", j["type"], cid)
                    await self._emit(j)
            await session.on_events(parsed)  # was on_vendor_raw(raw) — no second parse

        return on_msg

    def _make_on_audio(self, cid: str):
        conn = self._conns[cid]
        rate = conn.adapter.capabilities.output_sample_rate

        async def on_audio(pcm: bytes) -> None:
            if self._router.active_id != cid:
                return  # only the token holder's audio reaches the client
            # Agent is producing audio → hold the half-duplex mic gate open, plus a tail
            # so the client-side playout + room echo drain before the mic re-arms.
            self._mic_gate_until = time.monotonic() + self._echo_guard_s
            # TTFB: first audio byte of this turn. Break it down so we can see where the
            # time goes:  total = (bridge hangover we impose) + (Gemini end→first-byte).
            #   t0                = last speech sample  (END_time − hangover_s)
            #   t0 + hangover_s   = when ACTIVITY_END was actually sent to Gemini
            # so model_ms (ACTIVITY_END → first byte) is Gemini's real generation latency;
            # hangover_ms is purely the endpointing delay the bridge adds. Both are measured
            # at the SERVER receiving Gemini's byte — the user hears it later (downlink adds
            # jitter-buffer + opus + network + client decode).
            if self._ttfb_pending and self._ttfb_t0 is not None:
                self._ttfb_pending = False
                now = time.monotonic()
                total_ms = (now - self._ttfb_t0) * 1000.0
                model_ms = (now - (self._ttfb_t0 + self._hangover_s)) * 1000.0
                hangover_ms = self._hangover_s * 1000.0
                log.info(
                    "TTFB %s: total=%.0fms = hangover %.0fms + gemini(end→byte) %.0fms",
                    cid, total_ms, hangover_ms, model_ms,
                )
                await self._emit(
                    turn_ttfb(agent_id=cid, ttfb_ms=round(total_ms), model_ms=round(model_ms))
                )
            self._pipeline.on_vendor_audio(pcm, vendor_rate=rate)
            # One WS binary message == one opus packet: the frontend downlink decodes each
            # message as a single EncodedAudioChunk, so egress frames are NOT coalesced
            # (concatenated opus packets would fail to decode). Per-frame send is required
            # by the wire protocol; coalescing would need length-framing (out of scope).
            while True:
                frame = self._pipeline.playout(cid)
                if frame is None:
                    break
                self._out_bytes += len(frame)
                await self._socket.send_bytes(frame)
            log.debug("agent %s audio out, total=%d opus bytes", cid, self._out_bytes)

        return on_audio

    # --- helpers ----------------------------------------------------------

    def _make_send(self, conn):
        async def send(payload) -> None:
            await conn.send_tool_result(payload)

        return send

    async def _emit(self, obj: dict) -> None:
        if self._closing:
            return
        try:
            await self._socket.send_text(json.dumps(obj))
        except Exception:  # noqa: BLE001 - client gone mid-send
            self._closing = True
