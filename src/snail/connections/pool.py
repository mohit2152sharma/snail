"""ConnectionPool — per-AgentSpec warm-socket pool (see docs 02).

Realtime sessions are **stateful and conversation-bound**, so — unlike DB connections —
there is **no cross-session reuse**: the pool is scoped to one user-session, keyed
per-``AgentSpec`` (``spec.pool_key``). Its job is to take the ~100–300ms handshake off
the critical path by pre-opening + configuring the likely-next agent, then injecting
client context on join.

Pool jobs (docs 02 §Pool jobs), and where each lives here:

* **pre-warm** — :meth:`prewarm`: open + configure a standby ahead of need. Best-effort:
  respects the global ``max_warm`` admission cap and just declines (returns ``None``)
  when full, so the caller falls back to a lazy :meth:`acquire`.
* **acquire** — bucket-first: reuse a matching warm standby (fast path) else lazy
  connect; then inject the ``JoinContext``. Never blocked by the cap (the client is live
  now) — evicts the stalest cold standby to make room instead.
* **park** — :meth:`park`: return an ex-active connection to its bucket, warm, for fast
  re-promotion after a handoff-away.
* **recycle** — :meth:`recycle`: open a fresh socket (native resume via the stored
  handle) and atomic-swap it under the same :class:`AgentConnection` (docs 02
  §Component). Because context lives in the append-only log, a connection is always
  reconstructable — connections are disposable.
* **evict** — :meth:`evict_idle` / internal eviction under cap pressure.

**Deferred (needs the loop):** the *scheduler* that fires proactive recycle at
``deadline − margin`` and periodic keepalive is a session-loop concern, exactly like the
registry's ``sweep_timeouts`` deferral (STATUS §deferrals). This module exposes the pure
mechanism — :meth:`due_for_recycle` (query) + :meth:`recycle` (action) — so the loop
layer only has to schedule the calls.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable

from snail.vendor import JoinContext

from .connection import AgentConnection, ConnectionState
from .connector import Connector
from .spec import AgentSpec

Clock = Callable[[], float]


class ConnectionPool:
    """Per-``AgentSpec`` pool of warm vendor sockets for one user-session."""

    def __init__(
        self,
        *,
        connector: Connector,
        max_warm: int = 8,
        clock: Clock = time.monotonic,
    ) -> None:
        self._connector = connector
        self._max_warm = max_warm
        self._clock = clock
        # standby (warm, unassigned) connections per pool key.
        self._standby: dict[tuple, list[AgentConnection]] = defaultdict(list)
        # every open connection this pool owns (standby + handed-out), for close/stats.
        self._open: set[AgentConnection] = set()

    # --- capacity ---------------------------------------------------------

    @property
    def warm_count(self) -> int:
        return len(self._open)

    def _at_cap(self) -> bool:
        return len(self._open) >= self._max_warm

    # --- pre-warm / acquire ----------------------------------------------

    async def prewarm(self, spec: AgentSpec) -> AgentConnection | None:
        """Open a standby for ``spec`` ahead of need. ``None`` if at the warm cap.

        Best-effort by contract (docs 02 §admission): a full pool declines rather than
        evicting, so the caller lazily :meth:`acquire`\\ s when the agent is actually
        needed.
        """
        if self._at_cap():
            return None
        conn = await self._open_conn(spec)
        self._standby[spec.pool_key].append(conn)
        return conn

    async def acquire(
        self, spec: AgentSpec, join: JoinContext | None = None
    ) -> AgentConnection:
        """Get a warm connection for ``spec`` and inject client context.

        Reuses a matching standby if one exists (the pre-warm payoff); otherwise lazily
        connects, evicting the stalest cold standby first if the pool is at its cap. The
        returned connection is ``WARM`` — the caller (Router) promotes it to ``ACTIVE``.
        """
        bucket = self._standby[spec.pool_key]
        if bucket:
            conn = bucket.pop()
        else:
            if self._at_cap():
                await self._evict_one()
            conn = await self._open_conn(spec)
        if join is not None:
            await conn.inject_history(join)
        return conn

    def park(self, conn: AgentConnection) -> None:
        """Return an ex-active connection to its bucket as a warm standby (docs 02)."""
        conn.park()
        self._standby[conn.spec.pool_key].append(conn)

    async def release(self, conn: AgentConnection) -> None:
        """Close and drop a connection the pool owns (end of a user-session).

        Realtime sessions are conversation-bound — no cross-session reuse (docs 02) — so
        a finished client-session's socket is closed, not returned to a bucket.
        """
        for bucket in self._standby.values():
            if conn in bucket:
                bucket.remove(conn)
                break
        await self._close_conn(conn)

    # --- recycle (mechanism; scheduling is a loop concern) ---------------

    def due_for_recycle(self, *, margin: float) -> list[AgentConnection]:
        """Open connections within ``margin`` seconds of their vendor deadline.

        Pure query for the (deferred) loop scheduler — it decides *when* to call
        :meth:`recycle`. Unassigned standbys age toward max-duration too, so they are
        included: a promoted standby must never be stale (docs 02 §two recycle paths).
        """
        now = self._clock()
        return [c for c in self._open if c.meta.recycle_due(now, margin=margin)]

    async def recycle(self, conn: AgentConnection) -> None:
        """Replace ``conn``'s socket with a fresh one, keeping its identity (docs 02).

        Opens a new transport — resuming via the stored handle so context survives — then
        atomic-swaps it in and closes the old socket. Upper layers never see the change.
        """
        transport = await self._connector.open(
            conn.spec, resumption_handle=conn.meta.resumption_handle
        )
        old = conn.adopt(transport)
        if old is not None:
            await old.close()

    # --- eviction / teardown ---------------------------------------------

    async def evict_idle(self, *, older_than: float) -> int:
        """Close cold standbys idle longer than ``older_than`` seconds. Returns count."""
        now = self._clock()
        evicted = 0
        for key, bucket in self._standby.items():
            keep: list[AgentConnection] = []
            for conn in bucket:
                if now - conn.meta.last_activity > older_than:
                    await self._close_conn(conn)
                    evicted += 1
                else:
                    keep.append(conn)
            self._standby[key] = keep
        return evicted

    async def aclose(self) -> None:
        """Close every connection this pool owns."""
        for conn in list(self._open):
            await self._close_conn(conn)
        self._standby.clear()

    def stats(self) -> dict:
        return {
            "warm_total": len(self._open),
            "standby_total": sum(len(b) for b in self._standby.values()),
            "buckets": {
                k: len(v) for k, v in self._standby.items() if v
            },
            "max_warm": self._max_warm,
        }

    # --- internals --------------------------------------------------------

    async def _open_conn(self, spec: AgentSpec) -> AgentConnection:
        transport = await self._connector.open(spec)
        conn = AgentConnection(
            spec=spec,
            adapter=self._connector.adapter,
            transport=transport,
            clock=self._clock,
        )
        self._open.add(conn)
        return conn

    async def _close_conn(self, conn: AgentConnection) -> None:
        await conn.close()
        self._open.discard(conn)

    async def _evict_one(self) -> None:
        """Drop the stalest cold standby to free a warm slot for a live acquire."""
        stalest: AgentConnection | None = None
        for bucket in self._standby.values():
            for conn in bucket:
                if stalest is None or conn.meta.last_activity < stalest.meta.last_activity:
                    stalest = conn
        if stalest is not None:
            self._standby[stalest.spec.pool_key].remove(stalest)
            await self._close_conn(stalest)
