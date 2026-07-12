"""Tests for snail.transport — wire protocol, bridge pumps, and the FastAPI server.

The bridge runs against a fake ``ClientSocket`` and a fake connection (no server); the
server tests use FastAPI's ``TestClient`` with a fake ``Connector`` (no vendor network).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from snail.connections import AgentSpec
from snail.transport import (
    ClientBridge,
    Control,
    ControlType,
    PlayoutClock,
    create_app,
    decode_control,
    encode_control,
)
from snail.transport.bridge import ClientBridge as _Bridge  # noqa: F401
from snail.transport.socket import DISCONNECT
from snail.vendor import Backend, Interrupted, RealtimeControl, SetupParam


# --- fakes ---------------------------------------------------------------


class FakeSocket:
    """Scriptable ClientSocket: ``receive`` drains a queue; sends are recorded."""

    def __init__(self, incoming=(), *, block_when_empty=False):
        self._incoming = list(incoming)
        self._block = block_when_empty
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        if self._block:
            await asyncio.Event().wait()  # client stays connected, idle
        return {"type": DISCONNECT}

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def close(self, code=1000):
        self.closed = True


class FakeAdapter:
    """Only what the bridge touches: parse_event maps a sentinel to Interrupted."""

    def parse_event(self, raw):
        return [Interrupted()] if raw == "INTERRUPT" else []


class FakeConnection:
    """Records outbound seams; ``run`` replays a script of ('audio'|'msg', payload)."""

    async def send_realtime(self, chunk):
        self.realtime.append(chunk)

    async def send_realtime_control(self, control):
        self.controls.append(control)

    def __init__(self, script=(), *, block=True):
        self.adapter = FakeAdapter()
        self.script = list(script)
        self._block = block
        self.realtime: list = []
        self.controls: list = []

    async def run(self, on_message, *, on_audio=None):
        for kind, payload in self.script:
            if kind == "audio" and on_audio is not None:
                await on_audio(payload)
            elif kind == "msg":
                await on_message(payload)
        if self._block:
            # keep the pump alive so the client-in side drives termination
            await asyncio.Event().wait()


def _spec(id="a1") -> AgentSpec:
    return AgentSpec(
        id=id, backend=Backend.GEMINI_DEV, setup=SetupParam(model="m")
    )


def _bridge(socket, connection):
    return ClientBridge(socket=socket, connection=connection, input_sample_rate=16000)


# --- protocol ------------------------------------------------------------


def test_control_roundtrip_omits_defaults() -> None:
    wire = encode_control(Control(type=ControlType.FLUSH))
    assert wire == '{"type":"flush"}'  # unused fields omitted
    assert decode_control(wire).type is ControlType.FLUSH


def test_control_playout_carries_samples() -> None:
    c = decode_control(encode_control(Control(type=ControlType.PLAYOUT, samples=480)))
    assert c.type is ControlType.PLAYOUT and c.samples == 480


# --- PlayoutClock --------------------------------------------------------


def test_playout_clock_buffered_ahead_and_flush() -> None:
    clk = PlayoutClock()
    clk.note_sent(1000)
    clk.note_played(300)
    assert clk.buffered_ahead == 700
    clk.on_flush()  # barge-in drops the 700 buffered
    assert clk.buffered_ahead == 0 and clk.sent == 300


# --- bridge pumps --------------------------------------------------------


@pytest.mark.asyncio
async def test_to_client_forwards_audio_and_counts_samples() -> None:
    sock = FakeSocket()
    b = _bridge(sock, FakeConnection())
    await b._to_client(b"\x01\x00\x02\x00")  # 2 samples PCM16
    assert sock.sent_bytes == [b"\x01\x00\x02\x00"]
    assert b.playout.sent == 2


@pytest.mark.asyncio
async def test_interrupted_pushes_flush_control() -> None:
    sock = FakeSocket()
    conn = FakeConnection()
    b = _bridge(sock, conn)
    b.playout.note_sent(500)
    await b._on_vendor_msg("INTERRUPT")
    assert decode_control(sock.sent_text[-1]).type is ControlType.FLUSH
    assert b.playout.buffered_ahead == 0  # cut reset the clock


@pytest.mark.asyncio
async def test_on_message_forwarded_to_session() -> None:
    seen = []
    b = ClientBridge(
        socket=FakeSocket(),
        connection=FakeConnection(),
        on_message=lambda raw: _append(seen, raw),
    )
    await b._on_vendor_msg("hello")
    assert seen == ["hello"]


async def _append(sink, x):
    sink.append(x)


@pytest.mark.asyncio
async def test_pump_client_in_routes_audio_playout_end() -> None:
    sock = FakeSocket(
        incoming=[
            {"bytes": b"micaudio"},
            {"text": encode_control(Control(type=ControlType.PLAYOUT, samples=960))},
            {"text": encode_control(Control(type=ControlType.END))},
            {"type": DISCONNECT},
        ]
    )
    conn = FakeConnection()
    b = _bridge(sock, conn)
    await b._pump_client_in()  # returns on disconnect
    assert len(conn.realtime) == 1  # the mic audio frame
    assert b.playout.played == 960
    assert conn.controls == [RealtimeControl.AUDIO_STREAM_END]


@pytest.mark.asyncio
async def test_run_client_disconnect_no_bye() -> None:
    # agent side blocks; client immediately disconnects → we must NOT send after.
    sock = FakeSocket(incoming=[{"type": DISCONNECT}])
    conn = FakeConnection(script=[("audio", b"\x00\x00")])
    b = _bridge(sock, conn)
    await asyncio.wait_for(b.run(), timeout=1.0)
    assert sock.accepted
    kinds = [decode_control(t).type for t in sock.sent_text]
    assert ControlType.READY in kinds
    assert ControlType.BYE not in kinds  # client already gone
    assert not sock.closed  # no post-disconnect close


@pytest.mark.asyncio
async def test_run_vendor_ends_sends_bye_and_closes() -> None:
    # agent side ends (vendor closed) while the client is still connected (idle).
    sock = FakeSocket(block_when_empty=True)
    conn = FakeConnection(script=[("audio", b"\x00\x00")], block=False)
    b = _bridge(sock, conn)
    await asyncio.wait_for(b.run(), timeout=1.0)
    kinds = [decode_control(t).type for t in sock.sent_text]
    assert ControlType.READY in kinds and ControlType.BYE in kinds
    assert sock.closed


# --- server / pool lifecycle ---------------------------------------------


class ServerTransport:
    """A live transport whose receive drains a short script then ends (vendor closed).

    Ending the inbound stream drives the bridge's vendor-ended teardown path — a clean
    BYE + close while the client is still connected — which is what a real
    ``GoAway``/socket-close looks like and keeps the TestClient close handshake sane.
    """

    def __init__(self, messages=("m1",)):
        self.realtime: list = []
        self.closed = False
        self._messages = list(messages)

    async def send_realtime_input(self, **kw):
        self.realtime.append(kw)

    async def send_client_content(self, **kw):
        pass

    async def send_tool_response(self, *, function_responses):
        pass

    async def receive(self):
        for m in self._messages:
            await asyncio.sleep(0)  # yield so client-side sends land first
            yield m

    async def close(self):
        self.closed = True


class ServerConnector:
    def __init__(self):
        from snail.vendor import MockVendorAdapter

        self._adapter = MockVendorAdapter()
        self.transports: list[ServerTransport] = []
        self.opens = 0

    @property
    def adapter(self):
        return self._adapter

    async def open(self, spec, *, resumption_handle=None):
        self.opens += 1
        t = ServerTransport()
        self.transports.append(t)
        return t


def test_server_unknown_agent_closes() -> None:
    connector = ServerConnector()
    app = create_app(connector=connector, resolve_spec=_resolver({}))
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/agents/nope"):
            pass


def test_server_round_and_pool_lifecycle() -> None:
    connector = ServerConnector()
    specs = {"assistant": _spec("assistant")}
    app = create_app(connector=connector, resolve_spec=_resolver(specs))
    client = TestClient(app)
    with client:  # triggers lifespan startup/shutdown
        with client.websocket_connect("/v1/agents/assistant") as ws:
            # server greets with READY, then streams the (mock) agent output.
            assert decode_control(ws.receive_text()).type is ControlType.READY
            ws.send_bytes(b"micaudio")
        # client left → connection released
    assert connector.opens == 1  # one socket for the session, no leak
    assert connector.transports[0].closed  # released on disconnect / pool shutdown


def _resolver(specs):
    def resolve(agent: str) -> AgentSpec:
        return specs[agent]

    return resolve
