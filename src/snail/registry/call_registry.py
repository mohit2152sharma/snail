"""ToolCallRegistry — the in-flight tracker (see docs 04).

Guardian of the invariant: **every ``call_id`` resolves to exactly one terminal
result — never zero, never two.** Owned/driven by the Router; one per user-session.
It records outcomes and offers sweeps; it is *not* authority (the Router decides
blocked/skipped/reroute) and *not* the durable home (the log is — the session appends
``tool_call``/``tool_result`` events around these calls).

Single-resolution is enforced structurally: a terminal transition removes the entry,
so any later resolve/cancel/timeout for that ``call_id`` finds nothing and no-ops
(this also covers a late result arriving after close — it is simply dropped).

Timeouts are explicit: the session pumps :meth:`sweep_timeouts` from the loop clock
(loop-bound timers live in the session, docs 06). Cancellation of a running handler
task is likewise the session's job; this registry resolves the *model's wait*.
"""

from __future__ import annotations

import time

from snail.tools.result import ToolResult

from .pending import (
    CallState,
    Destination,
    PendingCall,
    TERMINAL_STATES,
)


class RegistryFull(RuntimeError):
    """Raised when the concurrent in-flight cap is hit (backpressure, docs 04)."""


class ToolCallRegistry:
    """Tracks in-flight calls by ``call_id`` with group/connection indexes."""

    __slots__ = ("_entries", "_by_group", "_by_conn", "_max_concurrent")

    def __init__(self, max_concurrent: int = 64) -> None:
        self._entries: dict[str, PendingCall] = {}
        self._by_group: dict[str, set[str]] = {}
        self._by_conn: dict[str, set[str]] = {}
        self._max_concurrent = max_concurrent

    # --- registration -----------------------------------------------------

    def register(
        self,
        call_id: str,
        tool_name: str,
        args: dict,
        *,
        origin_connection_id: str | None = None,
        destination: Destination = Destination.HANDLER,
        deadline: float | None = None,
        response_group_id: str | None = None,
        schedule: str | None = None,
        now: float | None = None,
    ) -> PendingCall:
        """Create an entry for a freshly-emitted vendor call. Returns the entry.

        Raises :class:`ValueError` on a duplicate ``call_id`` and :class:`RegistryFull`
        when the concurrent cap is reached.
        """
        if call_id in self._entries:
            raise ValueError(f"duplicate call_id {call_id!r}")
        if len(self._entries) >= self._max_concurrent:
            raise RegistryFull(f"in-flight cap {self._max_concurrent} reached")
        entry = PendingCall(
            call_id,
            tool_name,
            args,
            origin_connection_id=origin_connection_id,
            destination=destination,
            created_at=time.time() if now is None else now,
            deadline=deadline,
            response_group_id=response_group_id,
            schedule=schedule,
        )
        self._entries[call_id] = entry
        if response_group_id is not None:
            self._by_group.setdefault(response_group_id, set()).add(call_id)
        if origin_connection_id is not None:
            self._by_conn.setdefault(origin_connection_id, set()).add(call_id)
        return entry

    # --- non-terminal FSM transitions (internal, guarded) -----------------

    def advance(self, call_id: str, state: CallState) -> None:
        """Move a live entry to a non-terminal lifecycle state.

        Rejects terminal targets (use :meth:`resolve`/:meth:`cancel`) and no-ops-raises
        on a missing/already-terminal entry.
        """
        if state in TERMINAL_STATES:
            raise ValueError("use resolve()/cancel() for terminal states")
        entry = self._entries.get(call_id)
        if entry is None or entry.is_terminal:
            raise KeyError(f"no live entry for call_id {call_id!r}")
        entry.state = state

    # --- terminal transitions ---------------------------------------------

    def resolve(self, call_id: str, result: ToolResult) -> bool:
        """Resolve a call with its terminal result. First terminal wins.

        Returns ``True`` if this call resolved a live entry, ``False`` if there was
        nothing to resolve (already terminal / late / dropped after close).
        """
        return self._terminate(call_id, CallState.DONE, result)

    def cancel(self, call_id: str, *, reason: str | None = None) -> bool:
        """Cancel a single call → ``cancelled``."""
        return self._terminate(
            call_id, CallState.CANCELLED, ToolResult.cancelled(reason)
        )

    def sweep_response_group(self, response_group_id: str) -> int:
        """Cancel all calls in a response batch (barge-in scope). Returns the count."""
        return self._sweep(self._by_group.get(response_group_id))

    def sweep_connection(self, connection_id: str) -> int:
        """Cancel all of a connection's calls (handoff/close scope)."""
        return self._sweep(self._by_conn.get(connection_id))

    def sweep_all(self) -> int:
        """Cancel every in-flight call (session close)."""
        return self._sweep(set(self._entries))

    def sweep_timeouts(self, now: float | None = None) -> list[str]:
        """Resolve every entry past its deadline as ``timeout``. Returns their ids.

        The session pumps this from the loop clock; there are no self-arming timers.
        """
        t = time.time() if now is None else now
        expired = [
            cid
            for cid, e in self._entries.items()
            if e.deadline is not None and e.deadline <= t and not e.is_terminal
        ]
        for cid in expired:
            self._terminate(cid, CallState.TIMEOUT, ToolResult.timeout())
        return expired

    # --- internals --------------------------------------------------------

    def _sweep(self, call_ids: set[str] | None) -> int:
        if not call_ids:
            return 0
        count = 0
        for cid in list(call_ids):
            if self._terminate(cid, CallState.CANCELLED, ToolResult.cancelled()):
                count += 1
        return count

    def _terminate(
        self, call_id: str, state: CallState, result: ToolResult
    ) -> bool:
        entry = self._entries.get(call_id)
        if entry is None:  # already terminal + removed, or never existed → no-op
            return False
        entry.state = state
        entry.future.set_result(result)
        self._remove(call_id, entry)
        return True

    def _remove(self, call_id: str, entry: PendingCall) -> None:
        self._entries.pop(call_id, None)
        gid = entry.response_group_id
        if gid is not None:
            group = self._by_group.get(gid)
            if group is not None:
                group.discard(call_id)
                if not group:
                    del self._by_group[gid]
        cid = entry.origin_connection_id
        if cid is not None:
            conn = self._by_conn.get(cid)
            if conn is not None:
                conn.discard(call_id)
                if not conn:
                    del self._by_conn[cid]

    # --- introspection ----------------------------------------------------

    def get(self, call_id: str) -> PendingCall | None:
        return self._entries.get(call_id)

    def __contains__(self, call_id: object) -> bool:
        return call_id in self._entries

    @property
    def in_flight(self) -> int:
        return len(self._entries)

    def group_size(self, response_group_id: str) -> int:
        """Live calls remaining in a response batch (0 = batch complete)."""
        return len(self._by_group.get(response_group_id, ()))

    def group_call_ids(self, response_group_id: str) -> tuple[str, ...]:
        """Snapshot of live call_ids in a response batch (for task cancellation)."""
        return tuple(self._by_group.get(response_group_id, ()))
