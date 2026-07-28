//! Output routing primitives — the token-guarded [`gate::OutputGate`] (GATE 2). The full
//! Router/policy layer stays in Python for now (migrated in a later phase).

pub mod gate;
pub mod policy;
pub mod predicate;
pub mod signals;

pub use gate::{GateStats, OutputGate};
pub use policy::{
    default_chain, ChainPolicy, ControlToolPolicy, ProgrammaticPolicy, RoutingPolicy, Rule,
    RulePolicy,
};
pub use predicate::{f, Comparison, Predicate};
pub use signals::{
    AgentRef, AgentRole, Candidate, HealthState, RoutingAction, RoutingDecision, RoutingEvent,
    RoutingEventKind, RoutingSignal, Seam, SessionMeta,
};
