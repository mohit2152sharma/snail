//! Routing signals + decisions — the RoutingPolicy interface data (port of `snail.router.signals`).
//!
//! The Router feeds a [`RoutingSignal`] to a policy on each real event; the policy returns a
//! [`RoutingDecision`] or `None` ("no opinion, keep current routing"). A decision is advice: the
//! Router health-gates + validates the target before acting. The structs are `Serialize` so the
//! declarative predicate engine can resolve dotted field paths against a signal.

use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RoutingEventKind {
    UserSpeechFinal,
    ToolResult,
    TransferTo,
    TranscriptDelta,
    Programmatic,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentRole {
    Active,
    Listener,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HealthState {
    Healthy,
    NearDeadline,
    Stale,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RoutingAction {
    Stay,
    Handoff,
    FanoutAdd,
    FanoutRemove,
    Reject,
}

/// When the audio seam happens on a handoff (docs 05).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Seam {
    /// revoke+flush+vendor-cancel, drop half-sentence (barge-in/urgent).
    CutNow,
    /// finish utterance, transfer at silence. THE DEFAULT.
    AtTurnEnd,
    /// wait for a user-turn boundary. zero artifact, unbounded delay.
    AtIdle,
}

/// A flat event — its `kind` selects which fields are meaningful (unused stay `None`).
#[derive(Debug, Clone, Default, Serialize)]
pub struct RoutingEvent {
    pub kind: Option<RoutingEventKind>,
    pub text: Option<String>,
    pub is_final: Option<bool>,
    pub duration_ms: Option<i64>,
    pub agent_id: Option<String>,
    pub status: Option<String>,
    pub tool_name: Option<String>,
    pub retriable: Option<bool>,
    pub data: Option<Value>,
    pub target: Option<String>,
    pub args: Option<Value>,
    pub tag: Option<String>,
}

impl RoutingEvent {
    pub fn of(kind: RoutingEventKind) -> Self {
        Self {
            kind: Some(kind),
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentRef {
    pub id: String,
    pub spec_id: String,
    pub role: AgentRole,
}

/// A promotable/attachable agent the policy may target.
#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
    pub id: String,
    pub spec_id: String,
    pub health: HealthState,
    pub ttl_ms: Option<i64>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SessionMeta {
    pub turn_count: i64,
    pub cost_so_far: f64,
    pub elapsed_ms: i64,
    pub tags: Value,
}

/// What the Router hands a policy on each triggering event (docs 05).
#[derive(Debug, Clone, Serialize)]
pub struct RoutingSignal {
    pub event: RoutingEvent,
    pub active_agent: Option<AgentRef>,
    pub available: Vec<Candidate>,
    pub session_meta: SessionMeta,
}

impl RoutingSignal {
    pub fn new(event: RoutingEvent) -> Self {
        Self {
            event,
            active_agent: None,
            available: Vec::new(),
            session_meta: SessionMeta::default(),
        }
    }

    /// Serialize to a JSON value so the predicate engine can resolve dotted paths.
    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }
}

/// A policy's advice to the Router (docs 05).
#[derive(Debug, Clone, PartialEq)]
pub struct RoutingDecision {
    pub action: RoutingAction,
    pub target: Option<String>,
    pub seam: Seam,
    pub reason: String,
    pub confidence: Option<f64>,
}

impl RoutingDecision {
    pub fn new(action: RoutingAction) -> Self {
        Self {
            action,
            target: None,
            seam: Seam::AtTurnEnd,
            reason: String::new(),
            confidence: None,
        }
    }
}
