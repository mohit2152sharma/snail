//! Parsed vendor events — the neutral signals an adapter emits (port of `snail.vendor.events`).
//!
//! A vendor adapter turns raw wire messages into these neutral signals. The session folds
//! transcripts into the event log; the Router consumes tool calls, interrupts (barge-in), and
//! deadline signals. Modeled as one enum (the Python `ParsedEvent` union) — match by variant.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum ParsedEvent {
    /// Vendor-supplied transcript of user speech (async, may be partial/late).
    UserTranscript { text: String, is_final: bool },
    /// Transcript of the agent's own output.
    AgentTranscript { text: String, is_final: bool },
    /// The model asked to call a tool (intent, not command — docs 03).
    ToolCallRequest {
        call_id: String,
        name: String,
        args: Value,
    },
    /// The agent finished its turn (a natural seam boundary).
    TurnComplete,
    /// Vendor server-VAD detected user speech over agent output → barge-in.
    Interrupted,
    /// Vendor is about to terminate the session (Gemini). Recycle now.
    GoAway { time_left_ms: Option<i64> },
    /// New session-resumption handle (Gemini) to survive a recycle.
    ResumptionUpdate { handle: String },
    /// A vendor-reported error.
    VendorError { code: String, message: String },
}
