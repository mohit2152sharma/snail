"""``ToolRun`` + ``RunSlots`` — the run-level state machine (see docs 14).

A **run** is the executor's unit of work. A **call** is the vendor's. They are not the
same loop: a call must close exactly once (doc 04's invariant), while a run may span
several calls — it closes its carrier call with ``input_required``, waits for a human,
and is resumed by whatever call carries the answer back.

**One run per agent.** A single agent can only be doing one thing at a time, so a newer
tool call from that agent displaces whatever it was doing. Multi-agent sessions run
several concurrently — displacement never crosses agents.

Correlation needs no identifier from the model: the connection the call arrived on names
the agent, and that agent has at most one blocked run. ``for_tool`` and ``key`` are
carried anyway — they give the model context for the question it is asking, and they let
a stale answer be rejected instead of misapplied.

Pure and loop-free: no timers, no tasks, no ``asyncio``. ``resume`` is any object with
``set_result``; the session supplies a real future, tests supply a
:class:`~snail.registry.pending.Promise`.
"""

from __future__ import annotations

import enum
import time
from typing import Any

from snail.tools.input_required import InputRequired
from snail.tools.schema import validate


class InvalidRunState(RuntimeError):
    """Raised on an illegal run transition."""


class RunState(enum.Enum):
    """Per-run lifecycle. Internal — the model only ever sees call-level results."""

    EXECUTING = "executing"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATES = frozenset({RunState.DONE, RunState.CANCELLED})


class SubmitOutcome(enum.Enum):
    """Why a ``provide_input`` was accepted, or was not. Logged verbatim (docs 14)."""

    ACCEPTED = "accepted"
    NO_BLOCKED_RUN = "no_blocked_run"
    FOR_TOOL_MISMATCH = "for_tool_mismatch"
    KEY_MISMATCH = "key_mismatch"
    TYPE_MISMATCH = "type_mismatch"


class ToolRun:
    """One unit of executor work, owned by exactly one agent. Flat, slotted."""

    __slots__ = (
        "run_id",
        "agent_id",
        "tool_name",
        "state",
        "pending",
        "carrier_call_id",
        "resume",
        "deadline",
        "started_at",
    )

    def __init__(
        self,
        run_id: str,
        agent_id: str,
        tool_name: str,
        *,
        carrier_call_id: str | None,
        started_at: float,
    ) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.tool_name = tool_name
        self.state = RunState.EXECUTING
        #: What this run is waiting for, while ``BLOCKED``.
        self.pending: InputRequired | None = None
        #: The call that will carry this run's next output. ``None`` between outputs —
        #: a run whose ask has already been sent has nothing open to answer on.
        self.carrier_call_id = carrier_call_id
        #: Resolved with the submitted value; the parked handler continues from there.
        self.resume: Any = None
        self.deadline: float | None = None
        self.started_at = started_at

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"ToolRun({self.run_id} agent={self.agent_id} "
            f"tool={self.tool_name} {self.state.value})"
        )


class RunSlots:
    """At most one live run per agent. Enforces the displacement + resume rules."""

    __slots__ = ("_by_agent", "_counter")

    def __init__(self) -> None:
        self._by_agent: dict[str, ToolRun] = {}
        self._counter = 0

    # --- starting / displacing -------------------------------------------

    def start(
        self,
        agent_id: str,
        tool_name: str,
        *,
        carrier_call_id: str | None = None,
        now: float | None = None,
    ) -> tuple[ToolRun, ToolRun | None]:
        """Take ``agent_id``'s slot. Returns ``(new_run, displaced_run_or_None)``.

        The displaced run is handed back rather than cleaned up here: only the caller
        can cancel its task and close its carrier call with ``skipped`` (rule 5).
        Displacement is scoped to this agent — other agents' runs are untouched.
        """
        displaced = self._by_agent.get(agent_id)
        if displaced is not None:
            displaced.state = RunState.CANCELLED
        self._counter += 1
        run = ToolRun(
            f"R{self._counter}",
            agent_id,
            tool_name,
            carrier_call_id=carrier_call_id,
            started_at=time.time() if now is None else now,
        )
        self._by_agent[agent_id] = run
        return run, displaced

    # --- blocking / resuming ----------------------------------------------

    def block(
        self,
        run: ToolRun,
        pending: InputRequired,
        *,
        resume: Any,
        now: float | None = None,
    ) -> None:
        """Park ``run`` on ``pending``. Its carrier is consumed by the ask itself."""
        if run.is_terminal:
            raise InvalidRunState(f"{run.run_id} is {run.state.value}")
        run.state = RunState.BLOCKED
        run.pending = pending
        run.resume = resume
        run.deadline = (time.time() if now is None else now) + pending.budget_s
        run.carrier_call_id = None

    def submit(
        self, agent_id: str, *, for_tool: str, key: str, value: Any
    ) -> tuple[SubmitOutcome, ToolRun | None]:
        """Deliver a value to ``agent_id``'s blocked run.

        Every rejection leaves the run untouched and still answerable — a wrong answer
        never costs the user the chance to give the right one.
        """
        run = self._by_agent.get(agent_id)
        if run is None or run.state is not RunState.BLOCKED or run.pending is None:
            return SubmitOutcome.NO_BLOCKED_RUN, None
        if for_tool != run.tool_name:
            return SubmitOutcome.FOR_TOOL_MISMATCH, run
        if key != run.pending.key:
            return SubmitOutcome.KEY_MISMATCH, run
        if validate(value, run.pending.schema) is not None:
            return SubmitOutcome.TYPE_MISMATCH, run
        resume = run.resume
        run.state = RunState.EXECUTING
        run.pending = None
        run.resume = None
        run.deadline = None
        if resume is not None:
            resume.set_result(value)
        return SubmitOutcome.ACCEPTED, run

    # --- finishing ---------------------------------------------------------

    def finish(self, run: ToolRun) -> bool:
        """Mark ``run`` done and free its agent's slot. ``False`` if already displaced."""
        return self._release(run, RunState.DONE)

    def cancel(self, run: ToolRun) -> bool:
        """Mark ``run`` cancelled and free its agent's slot."""
        return self._release(run, RunState.CANCELLED)

    def cancel_agent(self, agent_id: str) -> ToolRun | None:
        """Cancel whatever ``agent_id`` is doing (handoff / connection close)."""
        run = self._by_agent.get(agent_id)
        if run is None:
            return None
        self.cancel(run)
        return run

    def sweep_expired(self, now: float | None = None) -> list[ToolRun]:
        """Cancel every blocked run past its budget. Returns them, for logging."""
        t = time.time() if now is None else now
        expired = [
            r
            for r in self._by_agent.values()
            if r.state is RunState.BLOCKED and r.deadline is not None and r.deadline <= t
        ]
        for run in expired:
            self.cancel(run)
        return expired

    def _release(self, run: ToolRun, state: RunState) -> bool:
        run.state = state
        # Identity check: a displaced run must not evict its replacement.
        if self._by_agent.get(run.agent_id) is run:
            del self._by_agent[run.agent_id]
            return True
        return False

    # --- introspection ------------------------------------------------------

    def get(self, agent_id: str) -> ToolRun | None:
        return self._by_agent.get(agent_id)

    def blocked(self, agent_id: str) -> ToolRun | None:
        run = self._by_agent.get(agent_id)
        return run if run is not None and run.state is RunState.BLOCKED else None

    def agents(self) -> tuple[str, ...]:
        return tuple(self._by_agent)

    def __len__(self) -> int:
        return len(self._by_agent)
