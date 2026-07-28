//! PendingCall entry + FSM enums + a minimal promise (port of `snail.registry.pending`).
//!
//! The registry's `future` is a tiny loop-agnostic [`Promise`], not an async future: the state
//! machine, single-resolution guard, indexes and sweeps are pure and unit-testable with no
//! runtime. The session layer bridges the promise to the loop (await + real timers).

use serde_json::Value;

use crate::tools::result::ToolResult;

/// Per-entry lifecycle FSM (internal only — never surfaced to the model).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallState {
    Received,
    Validating,
    Executing,
    AwaitingExternal,
    Resolving,
    Done,
    Cancelled,
    Timeout,
}

impl CallState {
    pub fn as_str(self) -> &'static str {
        match self {
            CallState::Received => "received",
            CallState::Validating => "validating",
            CallState::Executing => "executing",
            CallState::AwaitingExternal => "awaiting_external",
            CallState::Resolving => "resolving",
            CallState::Done => "done",
            CallState::Cancelled => "cancelled",
            CallState::Timeout => "timeout",
        }
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            CallState::Done | CallState::Cancelled | CallState::Timeout
        )
    }
}

/// Where a registered call's result comes from (return-path correlation).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Destination {
    Handler,
    Handoff,
    Reroute,
    DeferredExternal,
}

/// A minimal resolve-once future-like. Loop-agnostic — the async bridge layer polls `result`.
#[derive(Debug, Default)]
pub struct Promise {
    done: bool,
    result: Option<ToolResult>,
}

impl Promise {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn done(&self) -> bool {
        self.done
    }
    pub fn result(&self) -> Option<&ToolResult> {
        self.result.as_ref()
    }

    /// Resolve once. Panics on a double-resolution (a broken single-resolution contract).
    pub fn set_result(&mut self, value: ToolResult) {
        assert!(!self.done, "promise already resolved");
        self.done = true;
        self.result = Some(value);
    }
}

/// One in-flight tool call, keyed by `call_id`. Flat (docs 04).
pub struct PendingCall {
    pub call_id: String,
    pub tool_name: String,
    pub args: Value,
    pub origin_connection_id: Option<String>,
    pub destination: Destination,
    pub state: CallState,
    pub future: Promise,
    pub created_at: f64,
    pub deadline: Option<f64>,
    pub response_group_id: Option<String>,
    pub schedule: Option<String>,
}

impl PendingCall {
    pub fn is_terminal(&self) -> bool {
        self.state.is_terminal()
    }
}
