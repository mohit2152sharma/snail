//! ToolResult envelope + status taxonomy + speech directives (port of `snail.tools.result`).
//!
//! Every result the model sees has one shape. Sanitization boundary: the model gets
//! `status/reason/retriable/data`; raw errors go to the log only. Constructors apply the
//! framework directive/reason cascade so callers get sane defaults.

use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolStatus {
    Success,
    Error,
    Blocked,
    Skipped,
    InvalidArgs,
    Timeout,
    InvalidOutput,
    NotFound,
    Cancelled,
    Deferred,
}

impl ToolStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ToolStatus::Success => "success",
            ToolStatus::Error => "error",
            ToolStatus::Blocked => "blocked",
            ToolStatus::Skipped => "skipped",
            ToolStatus::InvalidArgs => "invalid_args",
            ToolStatus::Timeout => "timeout",
            ToolStatus::InvalidOutput => "invalid_output",
            ToolStatus::NotFound => "not_found",
            ToolStatus::Cancelled => "cancelled",
            ToolStatus::Deferred => "deferred",
        }
    }

    /// Framework default model-facing reason for a non-success status (docs 03 cascade).
    fn default_reason(self) -> Option<&'static str> {
        match self {
            ToolStatus::Error => Some("the tool failed"),
            ToolStatus::Blocked => Some("not permitted"),
            ToolStatus::Skipped => Some("handled elsewhere"),
            ToolStatus::Timeout => Some("timed out"),
            ToolStatus::InvalidOutput => Some("the tool returned an unexpected result"),
            ToolStatus::NotFound => Some("tool does not exist"),
            ToolStatus::Cancelled => Some("cancelled"),
            _ => None,
        }
    }

    /// Framework default speak directive text for a status, if any.
    fn default_directive(self) -> Option<&'static str> {
        match self {
            ToolStatus::Error => Some("briefly apologize, say you couldn't process the request"),
            ToolStatus::Blocked => Some("tell the user you're unable to do that"),
            ToolStatus::Timeout => Some("say it's taking too long, ask to try again"),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResponseMode {
    Speak,
    Silent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirectiveMode {
    /// natural-language instruction; model paraphrases (portable default).
    Hint,
    /// exact words; best-effort only (vendor owns the voice).
    Verbatim,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SpeakDirective {
    pub text: String,
    pub mode: DirectiveMode,
}

impl SpeakDirective {
    pub fn hint(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            mode: DirectiveMode::Hint,
        }
    }
}

/// The standard contract every `call_id` resolves to (exactly once, docs 04).
#[derive(Debug, Clone, PartialEq)]
pub struct ToolResult {
    pub status: ToolStatus,
    /// output_schema-shaped, success only.
    pub data: Option<Value>,
    /// model-facing, sanitized; non-success.
    pub reason: Option<String>,
    pub retriable: bool,
    pub response_mode: ResponseMode,
    pub speak_directive: Option<SpeakDirective>,
}

impl ToolResult {
    pub fn success(data: Option<Value>) -> Self {
        Self {
            status: ToolStatus::Success,
            data,
            reason: None,
            retriable: false,
            response_mode: ResponseMode::Silent,
            speak_directive: None,
        }
    }

    pub fn error(reason: Option<String>, retriable: bool) -> Self {
        Self::nonsuccess(ToolStatus::Error, reason, retriable, true)
    }

    pub fn blocked(reason: Option<String>) -> Self {
        Self::nonsuccess(ToolStatus::Blocked, reason, false, true)
    }

    pub fn skipped(reason: Option<String>) -> Self {
        Self::nonsuccess(ToolStatus::Skipped, reason, false, false)
    }

    pub fn invalid_args(detail: impl Into<String>) -> Self {
        Self {
            status: ToolStatus::InvalidArgs,
            data: None,
            reason: Some(detail.into()),
            retriable: true,
            response_mode: ResponseMode::Silent,
            speak_directive: None,
        }
    }

    pub fn timeout() -> Self {
        Self::nonsuccess(ToolStatus::Timeout, None, true, true)
    }

    pub fn invalid_output() -> Self {
        Self::nonsuccess(ToolStatus::InvalidOutput, None, false, false)
    }

    pub fn not_found(name: Option<&str>) -> Self {
        let reason = name.map(|n| format!("tool '{n}' does not exist"));
        Self::nonsuccess(ToolStatus::NotFound, reason, false, false)
    }

    pub fn cancelled(reason: Option<String>) -> Self {
        Self::nonsuccess(ToolStatus::Cancelled, reason, false, false)
    }

    fn nonsuccess(
        status: ToolStatus,
        reason: Option<String>,
        retriable: bool,
        speak: bool,
    ) -> Self {
        Self {
            status,
            data: None,
            reason: reason.or_else(|| status.default_reason().map(str::to_string)),
            retriable,
            response_mode: if speak {
                ResponseMode::Speak
            } else {
                ResponseMode::Silent
            },
            speak_directive: if speak {
                status.default_directive().map(SpeakDirective::hint)
            } else {
                None
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn success_is_silent_with_data() {
        let r = ToolResult::success(Some(json!({"x": 1})));
        assert_eq!(r.status, ToolStatus::Success);
        assert_eq!(r.response_mode, ResponseMode::Silent);
        assert_eq!(r.data, Some(json!({"x": 1})));
        assert!(r.reason.is_none());
    }

    #[test]
    fn error_cascades_reason_and_directive() {
        let r = ToolResult::error(None, true);
        assert_eq!(r.reason.as_deref(), Some("the tool failed"));
        assert_eq!(r.response_mode, ResponseMode::Speak);
        assert_eq!(
            r.speak_directive.unwrap().text,
            "briefly apologize, say you couldn't process the request"
        );
        assert!(r.retriable);
    }

    #[test]
    fn explicit_reason_overrides_default() {
        let r = ToolResult::blocked(Some("policy X".into()));
        assert_eq!(r.reason.as_deref(), Some("policy X"));
    }

    #[test]
    fn not_found_formats_name() {
        assert_eq!(
            ToolResult::not_found(Some("foo")).reason.as_deref(),
            Some("tool 'foo' does not exist")
        );
    }
}
