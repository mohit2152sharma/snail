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

log = logging.getLogger("multiagent")

from snail.audio import (
    FRAME_LEN,
    AudioPipeline,
    AudioSource,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
)
from snail.audio.opus_codec import OpusCodec
from snail.audio.soxr_backend import SoxrResampleBackend
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
from snail.vendor import Interrupted, MediaChunk, ResponseModality

from .agents import ECHO_ID, HOST_ID, POOL_KEY, SPECS, TRANSLATE_ID
from .events import active_agent_changed, error as err_event, to_client_json
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
            jitter=JitterBuffer(),
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
        log.info("promote → %s", agent_id)
        asyncio.create_task(self._emit(active_agent_changed(agent_id)))

    def _on_demote(self, agent_id: str) -> None:
        # Drop the ex-active's user-audio subscription (re-subscribed on promote). Do NOT
        # park its receive loop: it must keep draining so a trailing post-tool turn (e.g.
        # the host's confirmation generated after the control tool) is consumed and
        # *dropped* here, not left buffered to replay when the agent is promoted back.
        self._pipeline.detach_consumer(agent_id)
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
                    self._pipeline.on_client_audio(data)
                    await self._forward_drained()
                continue
            text = msg.get("text")
            if text is not None:
                await self._handle_control(text)
                if self._closing:
                    return

    async def _forward_drained(self) -> None:
        for cid, chunks in self._pipeline.drain().items():
            conn = self._conns.get(cid)
            if conn is None:
                continue
            rate = conn.adapter.capabilities.input_sample_rate
            n = 0
            for ch in chunks:
                await conn.send_realtime(MediaChunk.audio(ch, sample_rate=rate))
                n += len(ch)
            self._mic_bytes += n
            if self._mic_bytes - self._mic_logged > 96000:  # ~1s @16k mono
                log.info("mic→%s: %d bytes total", cid, self._mic_bytes)
                self._mic_logged = self._mic_bytes

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
            for ev in conn.adapter.parse_event(raw):
                if isinstance(ev, Interrupted):
                    self._pipeline.cut()
                j = to_client_json(ev, agent_id=cid)
                if j is not None:
                    log.info("event %s from %s", j["type"], cid)
                    await self._emit(j)
            await session.on_vendor_raw(raw)

        return on_msg

    def _make_on_audio(self, cid: str):
        conn = self._conns[cid]
        rate = conn.adapter.capabilities.output_sample_rate

        async def on_audio(pcm: bytes) -> None:
            if self._router.active_id != cid:
                return  # only the token holder's audio reaches the client
            self._pipeline.on_vendor_audio(pcm, vendor_rate=rate)
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
