"""MultiAgentBridge — one client socket ⇄ two live Gemini agents (host + echo).

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
from snail.vendor import Interrupted, MediaChunk, ResponseModality

from .agents import ECHO_ID, HOST_ID, SPECS
from .events import active_agent_changed, error as err_event, to_client_json
from .routing import build_policy
from .tools import echo_tools, host_tools

_AGENT_IDS = (HOST_ID, ECHO_ID)


class MultiAgentBridge:
    """Pump between one FastAPI WebSocket and the host+echo agents."""

    def __init__(self, *, socket, pool) -> None:
        self._socket = socket
        self._pool = pool
        self._conns: dict[str, object] = {}
        self._sessions: dict[str, Session] = {}
        self._tasks: list[asyncio.Task] = []
        self._muted = False
        self._closing = False

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
        self._tools = {HOST_ID: host_tools(), ECHO_ID: echo_tools()}

    # --- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        await self._socket.accept()
        try:
            await self._setup()
        except Exception as exc:  # noqa: BLE001 - surface setup failure to the client
            await self._emit(err_event("setup_failed", str(exc)))
            await self._release_conns()
            return
        for cid in _AGENT_IDS:
            conn = self._conns[cid]
            self._tasks.append(
                asyncio.create_task(
                    conn.run(self._make_on_msg(cid), on_audio=self._make_on_audio(cid))
                )
            )
        self._tasks.append(asyncio.create_task(self._pump_client()))
        try:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self._teardown()

    async def _setup(self) -> None:
        log = EventLog()
        for cid in _AGENT_IDS:
            conn = await self._pool.acquire(SPECS[cid])
            conn.activate()
            self._conns[cid] = conn
            self._router.register_agent(
                cid,
                cid,
                modality=ResponseModality.AUDIO,
                input_source=AudioSource.USER_RAW,
                target_rate=conn.adapter.capabilities.input_sample_rate,
            )
            self._sessions[cid] = Session(
                adapter=conn.adapter,
                log=log,
                tools=self._tools[cid],
                registry=self._registry,
                router=self._router,
                send=self._make_send(conn),
            )
        self._router.set_active(HOST_ID)  # host holds the token + hears user first
        await self._emit(active_agent_changed(HOST_ID))

    async def _teardown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for session in self._sessions.values():
            await session.aclose()
        await self._release_conns()

    async def _release_conns(self) -> None:
        for conn in self._conns.values():
            await self._pool.release(conn)
        self._conns.clear()

    # --- router hooks -----------------------------------------------------

    def _on_promote(self, agent_id: str, needs_flip: bool) -> None:
        # fire-and-forget: hooks are sync, emit is async.
        asyncio.create_task(self._emit(active_agent_changed(agent_id)))

    def _on_demote(self, agent_id: str) -> None:
        # silence the idle agent: drop its user-audio subscription (re-subbed on promote).
        self._pipeline.detach_consumer(agent_id)

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
            for ch in chunks:
                await conn.send_realtime(MediaChunk.audio(ch, sample_rate=rate))

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
                await self._socket.send_bytes(frame)

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
