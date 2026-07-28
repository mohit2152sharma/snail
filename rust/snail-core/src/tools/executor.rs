//! Envelope executor: run a Tool handler → a ToolResult (port of `snail.tools.executor`).
//!
//! Validates input against `input_schema` (→ `invalid_args`, with detail so the model
//! self-corrects), runs the handler (an `Err` → `error` with a sanitized reason; the raw message
//! is returned separately for log-only capture), then validates the return against `output_schema`
//! (→ `invalid_output`, generic reason). This is the sync envelope path; async handlers + timeouts
//! are the runtime layer's job.

use super::result::ToolResult;
use super::schema::validate;
use super::tool::Tool;

/// Run `tool` on `args`. Returns `(result, raw_error_for_log_only)`. The second element is
/// non-`None` only on `error`/`invalid_output` — the caller logs it; it never reaches the model.
pub fn execute(tool: &Tool, args: &serde_json::Value) -> (ToolResult, Option<String>) {
    if let Some(err) = validate(args, tool.input_schema.as_ref(), "") {
        return (ToolResult::invalid_args(err), None);
    }

    let data = match (tool.handler)(args) {
        Ok(data) => data,
        Err(raw) => {
            // Sanitized, model-facing reason; the raw message goes to the log only.
            return (
                ToolResult::error(Some(format!("{} failed", tool.name)), false),
                Some(raw),
            );
        }
    };

    if let Some(out_err) = validate(&data, Some(&tool.output_schema), "") {
        // Real detail is a tool-side bug → log only; the model gets a generic reason.
        return (ToolResult::invalid_output(), Some(out_err));
    }

    (ToolResult::success(Some(data)), None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::result::ToolStatus;
    use serde_json::json;

    fn echo_tool() -> Tool {
        Tool::new(
            "echo",
            json!({"type": "object", "properties": {"n": {"type": "integer"}}}),
            Box::new(|args| Ok(json!({"n": args.get("n").cloned().unwrap_or(json!(0))}))),
        )
        .with_input_schema(
            json!({"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}),
        )
    }

    #[test]
    fn valid_call_succeeds() {
        let (r, err) = execute(&echo_tool(), &json!({"n": 5}));
        assert_eq!(r.status, ToolStatus::Success);
        assert_eq!(r.data, Some(json!({"n": 5})));
        assert!(err.is_none());
    }

    #[test]
    fn invalid_args_gives_detail() {
        let (r, err) = execute(&echo_tool(), &json!({"n": "x"}));
        assert_eq!(r.status, ToolStatus::InvalidArgs);
        assert!(r.reason.unwrap().contains("expected integer"));
        assert!(err.is_none());
    }

    #[test]
    fn handler_error_is_sanitized_raw_logged() {
        let tool = Tool::new(
            "boom",
            json!({"type": "object"}),
            Box::new(|_| Err("stacktrace: secret internals".into())),
        );
        let (r, err) = execute(&tool, &json!({}));
        assert_eq!(r.status, ToolStatus::Error);
        assert_eq!(r.reason.as_deref(), Some("boom failed")); // sanitized
        assert_eq!(err.as_deref(), Some("stacktrace: secret internals")); // log only
    }

    #[test]
    fn bad_output_is_invalid_output() {
        let tool = Tool::new(
            "wrong",
            json!({"type": "object", "required": ["ok"]}),
            Box::new(|_| Ok(json!({"nope": 1}))),
        );
        let (r, err) = execute(&tool, &json!({}));
        assert_eq!(r.status, ToolStatus::InvalidOutput);
        assert!(err.is_some()); // real detail logged
    }
}
