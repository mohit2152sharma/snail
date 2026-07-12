"""Router — the multi-agent mechanism (see docs 05).

Owns **mechanism**: the output token (GATE 2 via :class:`OutputGate`), input
subscriptions (GATE 1 via :class:`FanoutBus`), the seam, and the
:class:`ToolCallRegistry`. **Decision** is delegated to a pluggable
:class:`RoutingPolicy` — the Router health-gates + validates a decision against reality
before acting (advice, not command).

Loop-bound side effects (actual vendor cancel, socket reconnect for a Gemini
text→audio flip, real turn-boundary detection) are **injected hooks**, so the Router's
orchestration is testable without a loop or vendor:

* ``on_promote(agent_id, needs_flip)`` — needs_flip=True ⇒ the target was a TEXT-modality
  listener, so promotion needs a text→audio flip (a reconnect on Gemini, 05/07/09§E).
* ``on_demote(agent_id)`` — the ex-active is now a listener (kept for instant re-promote).
* ``on_vendor_cancel(agent_id)`` — stop the vendor's wasted generation (CUT_NOW / barge-in).
"""

from __future__ import annotations

from collections.abc import Callable

from snail.audio import AudioSource, FanoutBus
from snail.registry import ToolCallRegistry
from snail.vendor import ResponseModality

from .gate import OutputGate
from .policy import RoutingPolicy, default_chain
from .signals import (
    AgentRef,
    AgentRole,
    HealthState,
    RoutingAction,
    RoutingDecision,
    RoutingSignal,
    Seam,
)


class AgentRecord:
    """Router-side view of one agent (role/health/modality mutate over its life)."""

    __slots__ = (
        "id",
        "spec_id",
        "role",
        "modality",
        "input_source",
        "target_rate",
        "health",
    )

    def __init__(
        self,
        id: str,
        spec_id: str,
        *,
        modality: ResponseModality,
        input_source: AudioSource,
        target_rate: int,
        role: AgentRole = AgentRole.LISTENER,
        health: HealthState = HealthState.HEALTHY,
    ) -> None:
        self.id = id
        self.spec_id = spec_id
        self.role = role
        self.modality = modality
        self.input_source = input_source
        self.target_rate = target_rate
        self.health = health


class Router:
    """One active agent + N listeners; promote/demote/handoff over a single token."""

    def __init__(
        self,
        *,
        gate: OutputGate,
        bus: FanoutBus,
        registry: ToolCallRegistry | None = None,
        policy: RoutingPolicy | None = None,
        on_promote: Callable[[str, bool], None] | None = None,
        on_demote: Callable[[str], None] | None = None,
        on_vendor_cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._gate = gate
        self._bus = bus
        self._registry = registry
        self._policy = policy or default_chain()
        self._on_promote = on_promote
        self._on_demote = on_demote
        self._on_vendor_cancel = on_vendor_cancel

        self._agents: dict[str, AgentRecord] = {}
        self._active_id: str | None = None
        self._pending: tuple[str, Seam] | None = None
        self._last_block: tuple[str, str] | None = None

    # --- registration / topology -----------------------------------------

    def register_agent(
        self,
        id: str,
        spec_id: str,
        *,
        modality: ResponseModality,
        input_source: AudioSource,
        target_rate: int,
        health: HealthState = HealthState.HEALTHY,
    ) -> AgentRecord:
        if id in self._agents:
            raise ValueError(f"agent {id!r} already registered")
        rec = AgentRecord(
            id,
            spec_id,
            modality=modality,
            input_source=input_source,
            target_rate=target_rate,
            health=health,
        )
        self._agents[id] = rec
        return rec

    def set_active(self, id: str) -> None:
        """Make ``id`` the initial active agent: grant the token + subscribe input."""
        rec = self._require(id)
        rec.role = AgentRole.ACTIVE
        self._active_id = id
        self._gate.grant(id)
        self._ensure_subscribed(id)

    def add_listener(self, id: str) -> None:
        """Subscribe ``id`` to user audio as a listener (GATE 1)."""
        rec = self._require(id)
        rec.role = AgentRole.LISTENER
        self._ensure_subscribed(id)

    def remove_listener(self, id: str) -> int:
        """Unsubscribe a listener (detach-release its ring). Returns frames released."""
        return self._bus.unsubscribe(id)

    # --- decision entry point --------------------------------------------

    def handle(self, signal: RoutingSignal) -> RoutingDecision | None:
        """Run the policy on ``signal`` and execute its decision. Returns the decision."""
        decision = self._policy.decide(signal)
        if decision is None:
            return None
        self._execute(decision)
        return decision

    def _execute(self, d: RoutingDecision) -> None:
        if d.action is RoutingAction.HANDOFF and d.target:
            self._handoff(d.target, d.seam)
        elif d.action is RoutingAction.FANOUT_ADD and d.target:
            self.add_listener(d.target)
        elif d.action is RoutingAction.FANOUT_REMOVE and d.target:
            self.remove_listener(d.target)
        # STAY / REJECT / malformed → no mechanism

    # --- handoff / seam ---------------------------------------------------

    def _handoff(self, target: str, seam: Seam) -> None:
        rec = self._agents.get(target)
        if rec is None:
            self._last_block = (target, "unknown target")
            return
        if rec.health is not HealthState.HEALTHY:
            # Never promote a stale socket (docs 02); a real Router recycles first.
            self._last_block = (target, f"health={rec.health.value}")
            return
        if seam is Seam.CUT_NOW:
            self._do_transfer(target, cut=True)
        else:
            self._pending = (target, seam)  # transfer at the next matching boundary

    def _do_transfer(self, target: str, *, cut: bool) -> None:
        old = self._active_id
        self._gate.transfer(target)  # atomic token move — overlap impossible
        if cut:
            self._gate.flush()  # drop the old agent's queued half-sentence
            if old is not None and self._on_vendor_cancel is not None:
                self._on_vendor_cancel(old)  # stop wasted generation
            if old is not None and self._registry is not None:
                self._registry.sweep_connection(old)  # cancel old's in-flight calls

        if old is not None and old in self._agents:
            self._agents[old].role = AgentRole.LISTENER  # demote-to-listener
            if self._on_demote is not None:
                self._on_demote(old)

        rec = self._agents[target]
        needs_flip = rec.modality is ResponseModality.TEXT
        rec.role = AgentRole.ACTIVE
        self._active_id = target
        self._ensure_subscribed(target)  # active always subscribed (listener already was)
        if self._on_promote is not None:
            self._on_promote(target, needs_flip)

    def on_turn_end(self) -> bool:
        """Fire a pending AT_TURN_END transfer at the utterance boundary."""
        return self._fire_pending(Seam.AT_TURN_END)

    def on_idle(self) -> bool:
        """Fire a pending AT_IDLE transfer at a user-turn boundary."""
        return self._fire_pending(Seam.AT_IDLE)

    def _fire_pending(self, seam: Seam) -> bool:
        if self._pending is not None and self._pending[1] is seam:
            target = self._pending[0]
            self._pending = None
            self._do_transfer(target, cut=False)
            return True
        return False

    # --- barge-in ---------------------------------------------------------

    def barge_in(self, response_group_id: str | None = None) -> None:
        """User interrupted the active agent (docs 04/05).

        Flush the output ring, cancel the vendor's generation, and sweep the current
        response's in-flight tool calls. The token stays with the active agent — it
        speaks the next turn; this is not a handoff.
        """
        self._gate.flush()
        if self._active_id is not None and self._on_vendor_cancel is not None:
            self._on_vendor_cancel(self._active_id)
        if response_group_id is not None and self._registry is not None:
            self._registry.sweep_response_group(response_group_id)

    # --- helpers / introspection -----------------------------------------

    def _ensure_subscribed(self, id: str) -> None:
        if self._bus.get(id) is None:
            rec = self._agents[id]
            self._bus.subscribe(
                id, source=rec.input_source, target_rate=rec.target_rate
            )

    def _require(self, id: str) -> AgentRecord:
        rec = self._agents.get(id)
        if rec is None:
            raise KeyError(f"agent {id!r} not registered")
        return rec

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def pending(self) -> tuple[str, Seam] | None:
        return self._pending

    @property
    def last_block(self) -> tuple[str, str] | None:
        return self._last_block

    def role_of(self, id: str) -> AgentRole | None:
        rec = self._agents.get(id)
        return rec.role if rec else None

    def agent_ref(self, id: str) -> AgentRef | None:
        """Immutable snapshot of an agent for a RoutingSignal."""
        rec = self._agents.get(id)
        if rec is None:
            return None
        return AgentRef(id=rec.id, spec_id=rec.spec_id, role=rec.role)
