"""PendingCall entry + FSM enums + a minimal promise (see docs 04).

The registry's ``future`` is deliberately a tiny :class:`Promise`, not an
``asyncio.Future``: the state machine, single-resolution guard, indexes and sweeps are
pure and unit-testable with no event loop. The session layer bridges the promise to
the loop (await + real timers) — that is where the loop lives (docs 06).
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any


class CallState(enum.Enum):
    """Per-entry lifecycle FSM (internal only — never surfaced to the model, docs 04).

    ``executing`` is a lifecycle state, not a terminal status; the model only ever
    sees a *terminal* ToolResult (plus the ``deferred`` interim ack).
    """

    RECEIVED = "received"
    VALIDATING = "validating"
    EXECUTING = "executing"
    AWAITING_EXTERNAL = "awaiting_external"  # deferred path
    RESOLVING = "resolving"
    DONE = "done"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


TERMINAL_STATES = frozenset(
    {CallState.DONE, CallState.CANCELLED, CallState.TIMEOUT}
)


class Destination(enum.Enum):
    """Where a registered call's result comes from (return-path correlation)."""

    HANDLER = "handler"
    HANDOFF = "handoff"
    REROUTE = "reroute"
    DEFERRED_EXTERNAL = "deferred_external"


class InvalidStateError(RuntimeError):
    """Raised on an illegal FSM transition or a double promise-resolution."""


class Promise:
    """A minimal resolve-once future-like. Loop-agnostic."""

    __slots__ = ("_done", "_result", "_callbacks")

    def __init__(self) -> None:
        self._done = False
        self._result: Any = None
        self._callbacks: list[Callable[["Promise"], None]] = []

    def done(self) -> bool:
        return self._done

    def result(self) -> Any:
        if not self._done:
            raise InvalidStateError("promise not resolved")
        return self._result

    def set_result(self, value: Any) -> None:
        if self._done:
            raise InvalidStateError("promise already resolved")
        self._done = True
        self._result = value
        for cb in self._callbacks:
            cb(self)
        self._callbacks.clear()

    def add_done_callback(self, cb: Callable[["Promise"], None]) -> None:
        if self._done:
            cb(self)
        else:
            self._callbacks.append(cb)


class PendingCall:
    """One in-flight tool call, keyed by ``call_id``. Flat, slotted (docs 04)."""

    __slots__ = (
        "call_id",
        "tool_name",
        "args",
        "origin_connection_id",
        "destination",
        "state",
        "future",
        "created_at",
        "deadline",
        "response_group_id",
        "schedule",
    )

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        args: dict,
        *,
        origin_connection_id: str | None,
        destination: Destination,
        created_at: float,
        deadline: float | None,
        response_group_id: str | None,
        schedule: str | None,
    ) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.args = args
        self.origin_connection_id = origin_connection_id
        self.destination = destination
        self.state = CallState.RECEIVED
        self.future = Promise()
        self.created_at = created_at
        self.deadline = deadline
        self.response_group_id = response_group_id
        self.schedule = schedule

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
