"""Tests for snail.connections (docs 02) — AgentConnection + ConnectionPool.

Key-free: a fake ``LiveTransport`` + fake ``Connector`` stand in for the vendor socket,
and ``MockVendorAdapter`` does the serialization. No network.
"""

from __future__ import annotations

import pytest

from snail.connections import (
    AgentConnection,
    AgentSpec,
    ConnectionPool,
    ConnectionState,
)
from snail.context import Item, Role
from snail.vendor import (
    Backend,
    JoinContext,
    MediaChunk,
    MockVendorAdapter,
    RealtimeControl,
    SetupParam,
)


# --- fakes ---------------------------------------------------------------


class FakeTransport:
    """Records send kwargs; ``receive`` yields a scripted list of raw messages."""

    def __init__(self, messages=()):
        self.realtime: list[dict] = []
        self.client_content: list[dict] = []
        self.tool_responses: list = []
        self.closed = False
        self._messages = list(messages)

    async def send_realtime_input(self, **kwargs):
        self.realtime.append(kwargs)

    async def send_client_content(self, **kwargs):
        self.client_content.append(kwargs)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.append(function_responses)

    async def receive(self):
        for m in self._messages:
            yield m

    async def close(self):
        self.closed = True


class FakeConnector:
    """Hands out ``FakeTransport``s and records every open (with resumption handle)."""

    def __init__(self, adapter=None, messages=()):
        self._adapter = adapter or MockVendorAdapter()
        self._messages = messages
        self.opens: list[tuple[str, str | None]] = []
        self.transports: list[FakeTransport] = []

    @property
    def adapter(self):
        return self._adapter

    async def open(self, spec, *, resumption_handle=None):
        self.opens.append((spec.id, resumption_handle))
        t = FakeTransport(self._messages)
        self.transports.append(t)
        return t


def _spec(id="a1", *, instruction="be nice") -> AgentSpec:
    return AgentSpec(
        id=id,
        backend=Backend.GEMINI_DEV,
        setup=SetupParam(model="gemini-2.5-flash-live", system_instruction=instruction),
    )


def _conn(transport=None, *, clock=None):
    kw = {"clock": clock} if clock else {}
    return AgentConnection(
        spec=_spec(),
        adapter=MockVendorAdapter(),
        transport=transport or FakeTransport(),
        **kw,
    )


# --- AgentSpec -----------------------------------------------------------


def test_pool_key_distinguishes_instruction_variants() -> None:
    assert _spec(instruction="a").pool_key != _spec(instruction="b").pool_key
    # same setup, different id → same bucket (key is per-setup, not per-id).
    assert _spec("x").pool_key == _spec("y").pool_key


# --- AgentConnection: state ----------------------------------------------


def test_state_transitions() -> None:
    c = _conn()
    assert c.state is ConnectionState.WARM
    c.activate()
    assert c.state is ConnectionState.ACTIVE
    c.park()
    assert c.state is ConnectionState.WARM


def test_cold_without_transport() -> None:
    c = AgentConnection(spec=_spec(), adapter=MockVendorAdapter())
    assert c.state is ConnectionState.COLD


@pytest.mark.asyncio
async def test_seams_go_through_transport() -> None:
    t = FakeTransport()
    c = _conn(t)
    await c.send_realtime(MediaChunk.audio(b"pcm", sample_rate=16000))
    await c.send_realtime_control(RealtimeControl.ACTIVITY_START)
    await c.send_turns([Item(role=Role.USER, text="hi")], complete=True)
    await c.send_tool_result({"result": "ok"})
    assert len(t.realtime) == 2  # audio + control both ride send_realtime_input
    assert t.client_content[0]["turn_complete"] is True
    assert t.tool_responses == [{"result": "ok"}]


@pytest.mark.asyncio
async def test_inject_history_seeds_without_completing() -> None:
    t = FakeTransport()
    c = _conn(t)
    await c.inject_history(
        JoinContext(history=(Item(role=Role.USER, text="earlier"),))
    )
    assert t.client_content[0]["turn_complete"] is False


@pytest.mark.asyncio
async def test_inject_history_noop_when_empty() -> None:
    t = FakeTransport()
    c = _conn(t)
    await c.inject_history(JoinContext())
    assert t.client_content == []


@pytest.mark.asyncio
async def test_run_pumps_messages_and_touches() -> None:
    clock = iter([1.0, 2.0, 3.0, 4.0, 5.0])
    t = FakeTransport(messages=["m1", "m2"])
    c = AgentConnection(
        spec=_spec(), adapter=MockVendorAdapter(), transport=t, clock=lambda: next(clock)
    )
    seen: list = []

    async def handler(msg):
        seen.append(msg)

    await c.run(handler)
    assert seen == ["m1", "m2"]
    assert c.meta.last_activity >= 1.0


@pytest.mark.asyncio
async def test_run_routes_output_audio() -> None:
    # MockVendorAdapter has no extract_output_audio → on_audio must be skipped safely.
    t = FakeTransport(messages=["m1"])
    c = _conn(t)
    got: list = []

    async def on_audio(pcm):
        got.append(pcm)

    async def handler(_):
        pass

    await c.run(handler, on_audio=on_audio)
    assert got == []  # mock adapter exposes no audio extractor


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    t = FakeTransport()
    c = _conn(t)
    await c.close()
    assert t.closed and c.state is ConnectionState.CLOSED
    await c.close()  # no raise


@pytest.mark.asyncio
async def test_send_after_close_raises() -> None:
    c = _conn()
    await c.close()
    with pytest.raises(RuntimeError):
        await c.send_realtime(MediaChunk.text_("x"))


# --- ConnectionMeta: deadline / resumption -------------------------------


def test_meta_goaway_sets_deadline_and_recycle_due() -> None:
    now = [100.0]
    c = AgentConnection(
        spec=_spec(), adapter=MockVendorAdapter(), transport=FakeTransport(),
        clock=lambda: now[0],
    )

    class _GoAway:
        time_left_ms = 10_000

    c.note_goaway(_GoAway())
    assert c.meta.ttl_headroom(now[0]) == pytest.approx(10.0)
    assert not c.meta.recycle_due(now[0], margin=2.0)
    now[0] = 109.0  # 1s headroom left
    assert c.meta.recycle_due(now[0], margin=2.0)


def test_meta_resumption_handle_recorded() -> None:
    c = _conn()
    c.note_resumption("handle-xyz")
    assert c.meta.resumption_handle == "handle-xyz"


def test_adopt_swaps_transport_and_carries_handle() -> None:
    old_t = FakeTransport()
    c = _conn(old_t)
    c.note_resumption("h1")
    new_t = FakeTransport()
    returned = c.adopt(new_t)
    assert returned is old_t
    assert c.meta.resumption_handle == "h1"  # carried forward for native resume


# --- ConnectionPool ------------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_then_acquire_reuses_standby() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    spec = _spec()
    warmed = await pool.prewarm(spec)
    assert warmed is not None and pool.warm_count == 1
    acquired = await pool.acquire(spec)
    assert acquired is warmed  # reused, not reconnected
    assert len(conn.opens) == 1  # only the pre-warm opened a socket


@pytest.mark.asyncio
async def test_acquire_lazy_connects_when_no_standby() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    c = await pool.acquire(_spec())
    assert c.state is ConnectionState.WARM
    assert len(conn.opens) == 1


@pytest.mark.asyncio
async def test_acquire_injects_join_history() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    await pool.acquire(
        _spec(), JoinContext(history=(Item(role=Role.USER, text="prior"),))
    )
    assert conn.transports[0].client_content[0]["turn_complete"] is False


@pytest.mark.asyncio
async def test_prewarm_respects_cap_returns_none() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn, max_warm=1)
    await pool.prewarm(_spec("a"))
    assert await pool.prewarm(_spec("b", instruction="other")) is None


@pytest.mark.asyncio
async def test_acquire_evicts_stalest_at_cap() -> None:
    clock = [0.0]
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn, max_warm=1, clock=lambda: clock[0])
    stale = await pool.prewarm(_spec("stale", instruction="s"))
    clock[0] = 50.0
    # different bucket, at cap → must evict the stale standby to connect live.
    fresh = await pool.acquire(_spec("fresh", instruction="f"))
    assert stale.state is ConnectionState.CLOSED
    assert fresh.state is ConnectionState.WARM
    assert pool.warm_count == 1


@pytest.mark.asyncio
async def test_park_returns_to_bucket_for_reuse() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    spec = _spec()
    c = await pool.acquire(spec)
    c.activate()
    pool.park(c)
    assert c.state is ConnectionState.WARM
    assert await pool.acquire(spec) is c  # fast re-promote, no new socket
    assert len(conn.opens) == 1


@pytest.mark.asyncio
async def test_recycle_swaps_socket_resumes_and_closes_old() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    c = await pool.acquire(_spec())
    c.note_resumption("resume-1")
    old_transport = conn.transports[0]
    await pool.recycle(c)
    # opened a second socket, passing the resumption handle; old one closed.
    assert conn.opens[-1] == (c.id, "resume-1")
    assert old_transport.closed is True
    assert c.state is ConnectionState.WARM


@pytest.mark.asyncio
async def test_due_for_recycle_flags_near_deadline() -> None:
    now = [0.0]
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn, clock=lambda: now[0])
    c = await pool.acquire(_spec())

    class _GoAway:
        time_left_ms = 5_000

    c.note_goaway(_GoAway())
    assert pool.due_for_recycle(margin=1.0) == []
    now[0] = 4.5
    assert pool.due_for_recycle(margin=1.0) == [c]


@pytest.mark.asyncio
async def test_evict_idle_closes_old_standbys() -> None:
    now = [0.0]
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn, clock=lambda: now[0])
    c = await pool.prewarm(_spec())
    now[0] = 100.0
    assert await pool.evict_idle(older_than=30.0) == 1
    assert c.state is ConnectionState.CLOSED
    assert pool.warm_count == 0


@pytest.mark.asyncio
async def test_aclose_closes_everything() -> None:
    conn = FakeConnector()
    pool = ConnectionPool(connector=conn)
    await pool.acquire(_spec("a", instruction="a"))
    await pool.prewarm(_spec("b", instruction="b"))
    await pool.aclose()
    assert pool.warm_count == 0
    assert all(t.closed for t in conn.transports)
