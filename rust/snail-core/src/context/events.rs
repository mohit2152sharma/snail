//! Canonical, vendor-neutral event + item schema (port of `snail.context.events`).
//!
//! The event log is the single source of truth: audio-free transcripts (audio never enters the
//! log). [`Item`] is the hard vendor-neutral boundary that projections stop at — the vendor
//! adapter serializes `Vec<Item>` to the wire, so nothing above this layer holds a vendor
//! payload. Both types are treated as immutable (the log is append-only).

use serde_json::Value;

/// Canonical log event kinds (docs 01).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EventType {
    UserSpeech,
    AgentSpeech,
    ToolCall,
    ToolResult,
    ExternalContext,
    Handoff,
}

impl EventType {
    pub fn as_str(self) -> &'static str {
        match self {
            EventType::UserSpeech => "user_speech",
            EventType::AgentSpeech => "agent_speech",
            EventType::ToolCall => "tool_call",
            EventType::ToolResult => "tool_result",
            EventType::ExternalContext => "external_context",
            EventType::Handoff => "handoff",
        }
    }
}

/// Vendor-neutral conversation roles. Gemini forbids `system` *content* turns — down-converting
/// SYSTEM is the adapter's job; the neutral surface keeps the distinction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    User,
    Model,
    System,
    Tool,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::User => "user",
            Role::Model => "model",
            Role::System => "system",
            Role::Tool => "tool",
        }
    }
}

/// One append-only log entry. Flat, audio-free. `seq` is assigned by the [`super::EventLog`].
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub seq: u64,
    pub ts: f64,
    pub kind: EventType,
    pub agent_id: Option<String>,
    pub content: String,
    /// structured extras (tool args/status, handoff target, …).
    pub meta: Option<Value>,
}

/// Vendor-neutral conversation item — the HARD boundary. A projection produces `Vec<Item>`; the
/// vendor adapter serializes it.
#[derive(Debug, Clone, PartialEq)]
pub struct Item {
    pub role: Role,
    pub text: String,
    /// tool name (for a TOOL result or a MODEL function-call item).
    pub name: Option<String>,
    /// correlates a model function-call with its TOOL result.
    pub tool_call_id: Option<String>,
    /// function-call arguments (MODEL → tool).
    pub args: Option<Value>,
}

impl Item {
    /// A plain text turn for `role`.
    pub fn text(role: Role, text: impl Into<String>) -> Self {
        Self {
            role,
            text: text.into(),
            name: None,
            tool_call_id: None,
            args: None,
        }
    }
}
