"""RoutingPolicy + built-in policies (see docs 05).

Mechanism (Router) vs decision (policy) split: a policy never touches sockets; it
consumes a :class:`RoutingSignal` and returns advice. ``decide`` runs only on real
events (trigger-driven, no polling); ``None`` = "keep current routing" (the cheap 99%).

Precedence is **not hardcoded** — it is the order of a :class:`ChainPolicy`. The shipped
default puts **Programmatic first** so an explicit app/backend decision beats the
model's ``transfer_to`` (09§E ``chain-default-order``).
"""

from __future__ import annotations

from collections import deque
from typing import Protocol, runtime_checkable

from .predicate import Predicate
from .signals import (
    RoutingAction,
    RoutingDecision,
    RoutingEventKind,
    RoutingSignal,
    Seam,
)


@runtime_checkable
class RoutingPolicy(Protocol):
    def decide(self, signal: RoutingSignal) -> RoutingDecision | None:
        """Advice for this signal, or ``None`` to keep current routing."""
        ...


class ControlToolPolicy:
    """Active agent emitted ``transfer_to`` → HANDOFF(target). Free, deterministic."""

    def __init__(self, seam: Seam = Seam.AT_TURN_END) -> None:
        self._seam = seam

    def decide(self, signal: RoutingSignal) -> RoutingDecision | None:
        ev = signal.event
        if ev.kind is RoutingEventKind.TRANSFER_TO and ev.target:
            return RoutingDecision(
                action=RoutingAction.HANDOFF,
                target=ev.target,
                seam=self._seam,
                reason="control tool transfer_to",
            )
        return None


class ProgrammaticPolicy:
    """App/backend pushes a decision from outside (button, backend event).

    ``push`` queues a decision; ``decide`` returns the oldest queued one (FIFO) on any
    signal and clears it. Placed first in the default chain so it wins.
    """

    def __init__(self) -> None:
        self._queue: deque[RoutingDecision] = deque()

    def push(self, decision: RoutingDecision) -> None:
        self._queue.append(decision)

    def decide(self, signal: RoutingSignal) -> RoutingDecision | None:
        if self._queue:
            return self._queue.popleft()
        return None


class Rule:
    """``when`` predicate → ``then`` decision template (docs 05)."""

    __slots__ = ("when", "then")

    def __init__(self, when: Predicate, then: RoutingDecision) -> None:
        self.when = when
        self.then = then


class RulePolicy:
    """Ordered rules; first matching predicate wins (local precedence)."""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules) if rules else []

    def add(self, rule: Rule) -> "RulePolicy":
        self._rules.append(rule)
        return self

    def decide(self, signal: RoutingSignal) -> RoutingDecision | None:
        for rule in self._rules:
            if rule.when.matches(signal):
                return rule.then
        return None


class ChainPolicy:
    """Ordered composite; first non-``None`` wins. **Order = precedence** (docs 05)."""

    def __init__(self, policies: list[RoutingPolicy]) -> None:
        self._policies = list(policies)

    def decide(self, signal: RoutingSignal) -> RoutingDecision | None:
        for policy in self._policies:
            decision = policy.decide(signal)
            if decision is not None:
                return decision
        return None


def default_chain(
    *,
    programmatic: ProgrammaticPolicy | None = None,
    rules: RulePolicy | None = None,
    control_seam: Seam = Seam.AT_TURN_END,
) -> ChainPolicy:
    """The shipped default: Programmatic → ControlTool → Rule (09§E).

    Programmatic first so an explicit app/backend decision beats the model's
    ``transfer_to``; rules last as the catch-all.
    """
    return ChainPolicy(
        [
            programmatic or ProgrammaticPolicy(),
            ControlToolPolicy(seam=control_seam),
            rules or RulePolicy(),
        ]
    )
