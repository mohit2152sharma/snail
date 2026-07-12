"""Router / Arbiter: mechanism (token, subscriptions, seam) + pluggable policy (docs 05)."""

from .gate import OutputGate
from .policy import (
    ChainPolicy,
    ControlToolPolicy,
    ProgrammaticPolicy,
    RoutingPolicy,
    Rule,
    RulePolicy,
    default_chain,
)
from .predicate import (
    BoolOp,
    CallablePredicate,
    Comparison,
    F,
    Not,
    Predicate,
    predicate_from_dict,
)
from .router import AgentRecord, Router
from .signals import (
    AgentRef,
    AgentRole,
    Candidate,
    HealthState,
    RoutingAction,
    RoutingDecision,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
    Seam,
    SessionMeta,
)

__all__ = [
    "Router",
    "AgentRecord",
    "OutputGate",
    # policy
    "RoutingPolicy",
    "ControlToolPolicy",
    "ProgrammaticPolicy",
    "RulePolicy",
    "Rule",
    "ChainPolicy",
    "default_chain",
    # predicate
    "Predicate",
    "Comparison",
    "BoolOp",
    "Not",
    "CallablePredicate",
    "F",
    "predicate_from_dict",
    # signals
    "RoutingSignal",
    "RoutingEvent",
    "RoutingEventKind",
    "RoutingDecision",
    "RoutingAction",
    "Seam",
    "AgentRef",
    "AgentRole",
    "Candidate",
    "HealthState",
    "SessionMeta",
]
