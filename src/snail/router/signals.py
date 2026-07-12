"""Routing signals + decisions — the RoutingPolicy interface data (see docs 05).

The Router feeds a :class:`RoutingSignal` to a policy on each real event; the policy
returns a :class:`RoutingDecision` or ``None`` ("no opinion, keep current routing" —
the cheap 99% path). Decision is **advice**: the Router health-gates + validates the
target before acting.
"""

from __future__ import annotations

import enum

import msgspec


class RoutingEventKind(enum.Enum):
    USER_SPEECH_FINAL = "user_speech_final"
    TOOL_RESULT = "tool_result"
    TRANSFER_TO = "transfer_to"  # active agent's control tool (handoff request)
    TRANSCRIPT_DELTA = "transcript_delta"
    PROGRAMMATIC = "programmatic"  # app/backend pushed signal


class AgentRole(enum.Enum):
    ACTIVE = "active"
    LISTENER = "listener"


class HealthState(enum.Enum):
    HEALTHY = "healthy"
    NEAR_DEADLINE = "near_deadline"  # promote → recycle first (docs 02)
    STALE = "stale"  # never promote


class RoutingAction(enum.Enum):
    STAY = "stay"
    HANDOFF = "handoff"
    FANOUT_ADD = "fanout_add"  # add a listener subscription
    FANOUT_REMOVE = "fanout_remove"  # drop a listener subscription
    REJECT = "reject"


class Seam(enum.Enum):
    """When the audio seam happens on a handoff (docs 05)."""

    CUT_NOW = "cut_now"  # revoke+flush+vendor-cancel, drop half-sentence (barge-in/urgent)
    AT_TURN_END = "at_turn_end"  # finish utterance, transfer at silence. THE DEFAULT.
    AT_IDLE = "at_idle"  # wait for a user-turn boundary. zero artifact, unbounded delay.


class RoutingEvent(msgspec.Struct, frozen=True, kw_only=True):
    """A flat event — its ``kind`` selects which fields are meaningful.

    Flat (not a union) so predicates can read ``event.<field>`` uniformly. Unused
    fields stay ``None``. Mirrors the RulePolicy predicate surface in docs 05.
    """

    kind: RoutingEventKind
    text: str | None = None
    is_final: bool | None = None
    duration_ms: int | None = None
    agent_id: str | None = None
    # tool_result payload
    status: str | None = None
    tool_name: str | None = None
    retriable: bool | None = None
    data: dict | None = None
    # transfer_to payload
    target: str | None = None
    args: dict | None = None
    # programmatic payload
    tag: str | None = None


class AgentRef(msgspec.Struct, frozen=True, kw_only=True):
    id: str
    spec_id: str
    role: AgentRole


class Candidate(msgspec.Struct, frozen=True, kw_only=True):
    """A promotable/attachable agent the policy may target."""

    id: str
    spec_id: str
    health: HealthState = HealthState.HEALTHY
    ttl_ms: int | None = None


class SessionMeta(msgspec.Struct, frozen=True, kw_only=True):
    turn_count: int = 0
    cost_so_far: float = 0.0
    elapsed_ms: int = 0
    tags: dict = {}


class RoutingSignal(msgspec.Struct, frozen=True, kw_only=True):
    """What the Router hands a policy on each triggering event (docs 05)."""

    event: RoutingEvent
    active_agent: AgentRef | None = None
    available: tuple[Candidate, ...] = ()
    session_meta: SessionMeta = SessionMeta()


class RoutingDecision(msgspec.Struct, frozen=True, kw_only=True):
    """A policy's advice to the Router (docs 05)."""

    action: RoutingAction
    target: str | None = None  # agent id / spec id (HANDOFF, FANOUT_*)
    seam: Seam = Seam.AT_TURN_END
    reason: str = ""
    confidence: float | None = None  # LLM policies only
