"""Integration: ClientBridge wired to an AudioPipeline (end-to-end audio plane).

The mock adapter is configured with 48k in/out so the lazy resampler is a proven no-op
(no native backend needed); this exercises the *wiring* — ingress through the plane to
the vendor, egress through jitter+gate+codec to the client, and barge-in cutting both.
A guard resampler backend asserts no conversion is attempted at equal rates.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from snail.audio import (
    AudioPipeline,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
    PcmCodec,
)
from snail.connections import AgentConnection, AgentSpec
from snail.router import OutputGate
from snail.transport import ClientBridge, ControlType, decode_control
from snail.transport.socket import DISCONNECT
from snail.vendor import (
    Backend,
    InputSource,
    MockVendorAdapter,
    SetupParam,
    TurnComplete,
    VendorCapabilities,
)

_CAPS_48K = VendorCapabilities(
    vendor="mock",
    model="mock-live",
    backend=Backend.MOCK,
    native_async_tools=True,
    session_resumption=True,
    system_content_turn=False,
    mid_session_config_update=False,
    item_truncate=False,
    input_sample_rate=48000,  # 48k in/out → resampler is a no-op
    output_sample_rate=48000,
)


class NoResampleBackend:
    def stream(self, f, t):  # pragma: no cover
        raise AssertionError("resample must be a no-op at 48k")


class FakeSocket:
    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.closed = False

    async def accept(self):
        pass

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": DISCONNECT}

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def close(self, code=1000):
        self.closed = True


class FakeTransport:
    def __init__(self):
        self.realtime: list = []

    async def send_realtime_input(self, **kw):
        self.realtime.append(kw)

    async def send_client_content(self, **kw):
        pass

    async def send_tool_response(self, *, function_responses):
        pass

    async def receive(self):
        if False:  # pragma: no cover
            yield

    async def close(self):
        pass


def _conn(transport):
    return AgentConnection(
        spec=AgentSpec(
            id="agent",
            backend=Backend.MOCK,
            setup=SetupParam(model="m", input_source=InputSource.RAW),
        ),
        adapter=MockVendorAdapter(capabilities=_CAPS_48K),
        transport=transport,
    )


def _pipeline():
    pool = FramePool(capacity=64, slab_samples=480)
    return AudioPipeline(
        pool=pool,
        bus=FanoutBus(pool),
        resampler=LazyResampler(NoResampleBackend()),
        gate=OutputGate(depth=16),
        jitter=JitterBuffer(frame_size=480, prefill_frames=1),
        codec=PcmCodec(),
        client_rate=48000,
    )


def _samples(n):
    return (np.arange(n) % 200 - 100).astype(np.int16)


@pytest.mark.asyncio
async def test_ingress_flows_client_to_vendor_through_plane() -> None:
    pipe = _pipeline()
    transport = FakeTransport()
    conn = _conn(transport)
    x = _samples(960)  # two 480-sample frames
    sock = FakeSocket(
        incoming=[{"bytes": PcmCodec().encode(x)}, {"type": DISCONNECT}]
    )
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    await asyncio.wait_for(b.run(), timeout=1.0)
    sent = b"".join(kw["data"] for kw in transport.realtime if kw.get("kind") == "audio")
    assert np.array_equal(np.frombuffer(sent, dtype=np.int16), x)


@pytest.mark.asyncio
async def test_egress_flows_vendor_to_client_through_plane() -> None:
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket()
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    pipe.hold_token("agent")  # this connection holds the output token
    await b._to_client(_samples(960).tobytes())  # arms jitter (prefill 1)
    assert sock.sent_bytes
    assert all(len(f) == 480 * 2 for f in sock.sent_bytes)


@pytest.mark.asyncio
async def test_egress_dropped_without_token() -> None:
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket()
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    # no token granted → gate suppresses; nothing reaches the client.
    await b._to_client(_samples(960).tobytes())
    assert sock.sent_bytes == []


@pytest.mark.asyncio
async def test_barge_in_cuts_plane_and_flushes_client() -> None:
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket()
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    pipe.hold_token("agent")
    pipe.on_vendor_audio(_samples(2400).tobytes(), vendor_rate=48000)
    await b._flush_client()
    assert decode_control(sock.sent_text[-1]).type is ControlType.FLUSH
    assert pipe.stats["jitter"]["buffered"] == 0  # plane cut server-side


@pytest.mark.asyncio
async def test_ttfb_measures_last_ingest_to_first_agent_byte() -> None:
    """The end-to-end number: last mic chunk in → first agent byte out, wall clock."""
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket()
    ticks = iter([100.0, 100.35])  # ingest at t=100.0s, first byte out at t=100.35s
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe, clock=lambda: next(ticks))
    pipe.hold_token("agent")
    assert b.ttfb_stats["count"] == 0  # nothing measured yet

    await b._ingest_audio(PcmCodec().encode(_samples(480)))
    await b._to_client(_samples(960).tobytes())  # arms jitter (prefill 1) → first byte

    stats = b.ttfb_stats
    assert stats["count"] == 1
    assert stats["last_ms"] == pytest.approx(350.0)


@pytest.mark.asyncio
async def test_ttfb_rearms_on_turn_complete_and_barge_in() -> None:
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket()
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    pipe.hold_token("agent")

    await b._ingest_audio(PcmCodec().encode(_samples(480)))
    await b._to_client(_samples(960).tobytes())  # turn 1's first byte
    assert b.ttfb_stats["count"] == 1

    await b._to_client(_samples(960).tobytes())  # more of the same turn: not re-measured
    assert b.ttfb_stats["count"] == 1

    await b._on_vendor_msg({"type": "turn_complete"})  # arms turn 2
    await b._ingest_audio(PcmCodec().encode(_samples(480)))
    await b._to_client(_samples(960).tobytes())
    assert b.ttfb_stats["count"] == 2

    await b._flush_client()  # barge-in also arms a fresh measurement
    await b._to_client(_samples(960).tobytes())
    assert b.ttfb_stats["count"] == 3


@pytest.mark.asyncio
async def test_run_attaches_then_detaches_consumer() -> None:
    pipe = _pipeline()
    conn = _conn(FakeTransport())
    sock = FakeSocket(incoming=[{"type": DISCONNECT}])
    b = ClientBridge(socket=sock, connection=conn, pipeline=pipe)
    await asyncio.wait_for(b.run(), timeout=1.0)
    assert pipe._bus.get("agent") is None  # detached on teardown (slabs released)
