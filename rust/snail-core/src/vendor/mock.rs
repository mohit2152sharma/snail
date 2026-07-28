//! MockVendorAdapter — deterministic, key-free vendor stand-in (port of `snail.vendor.mock`).
//!
//! Does real translation (so serialization is exercised) plus two test affordances: it **records**
//! everything the framework sent (via `RefCell`), and `parse_event` accepts a small documented mock
//! wire schema so a test can drive neutral events without a socket.
//!
//! Mock wire schema for [`MockVendorAdapter::parse_event`] (`raw["type"]` selects the event):
//! `user_transcript` {text, final} · `agent_transcript` {text, final} ·
//! `tool_call` {call_id, name, args} · `turn_complete` · `interrupted` ·
//! `go_away` {time_left_ms} · `resumption` {handle} · `error` {code, message} · else → [].
//!
//! Note: realtime audio bytes are recorded as a `data_len` field (not the raw bytes) — the mock is
//! for assertions, not byte-fidelity.

use std::cell::RefCell;

use serde_json::{json, Value};

use crate::context::{Item, Role};

use super::adapter::VendorAdapter;
use super::capabilities::{Backend, VendorCapabilities};
use super::events::ParsedEvent;
use super::media::{MediaChunk, MediaKind, RealtimeControl};
use super::params::SetupParam;

fn gemini_dev_profile() -> VendorCapabilities {
    VendorCapabilities {
        native_async_tools: true,
        session_resumption: true,
        ..VendorCapabilities::new("mock", "mock-live", Backend::Mock)
    }
}

fn role_str(role: Role) -> &'static str {
    role.as_str()
}

fn kind_str(kind: MediaKind) -> &'static str {
    match kind {
        MediaKind::Audio => "audio",
        MediaKind::Image => "image",
        MediaKind::Text => "text",
    }
}

fn control_str(c: RealtimeControl) -> &'static str {
    match c {
        RealtimeControl::ActivityStart => "activity_start",
        RealtimeControl::ActivityEnd => "activity_end",
        RealtimeControl::AudioStreamEnd => "audio_stream_end",
    }
}

/// A deterministic [`VendorAdapter`] for tests, with recorders for what the framework pushed.
pub struct MockVendorAdapter {
    caps: VendorCapabilities,
    pub sent_setups: RefCell<Vec<Value>>,
    pub sent_items: RefCell<Vec<Value>>,
    pub sent_tool_results: RefCell<Vec<Value>>,
    pub sent_realtime: RefCell<Vec<Value>>,
    pub sent_controls: RefCell<Vec<String>>,
    pub sent_turns: RefCell<Vec<Value>>,
}

impl Default for MockVendorAdapter {
    fn default() -> Self {
        Self::new(gemini_dev_profile())
    }
}

impl MockVendorAdapter {
    pub fn new(capabilities: VendorCapabilities) -> Self {
        Self {
            caps: capabilities,
            sent_setups: RefCell::new(Vec::new()),
            sent_items: RefCell::new(Vec::new()),
            sent_tool_results: RefCell::new(Vec::new()),
            sent_realtime: RefCell::new(Vec::new()),
            sent_controls: RefCell::new(Vec::new()),
            sent_turns: RefCell::new(Vec::new()),
        }
    }

    fn item_value(&self, item: &Item) -> Value {
        let mut msg = if item.role == Role::System && !self.caps.system_content_turn {
            json!({"role": "user", "text": format!("[system] {}", item.text), "_downconverted": true})
        } else {
            json!({"role": role_str(item.role), "text": item.text})
        };
        let obj = msg.as_object_mut().unwrap();
        if let Some(name) = &item.name {
            obj.insert("name".into(), json!(name));
        }
        if let Some(id) = &item.tool_call_id {
            obj.insert("tool_call_id".into(), json!(id));
        }
        if let Some(args) = &item.args {
            obj.insert("args".into(), args.clone());
        }
        msg
    }
}

impl VendorAdapter for MockVendorAdapter {
    fn name(&self) -> &str {
        "mock"
    }
    fn capabilities(&self) -> &VendorCapabilities {
        &self.caps
    }

    fn build_setup(&self, setup: &SetupParam) -> Value {
        let tools: Vec<Value> = setup
            .tools
            .iter()
            .map(|t| json!({"name": t.name, "description": t.description, "parameters": t.parameters, "non_blocking": t.non_blocking}))
            .collect();
        let msg = json!({
            "type": "setup",
            "model": setup.model,
            "voice": setup.voice,
            "system_instruction": setup.system_instruction,
            "tools": tools,
            "response_modality": match setup.response_modality {
                super::params::ResponseModality::Audio => "audio",
                super::params::ResponseModality::Text => "text",
            },
            "input_source": match setup.input_source {
                super::params::InputSource::Clean => "clean",
                super::params::InputSource::Raw => "raw",
            },
        });
        self.sent_setups.borrow_mut().push(msg.clone());
        msg
    }

    fn serialize_item(&self, item: &Item) -> Value {
        let msg = self.item_value(item);
        self.sent_items.borrow_mut().push(msg.clone());
        msg
    }

    fn serialize_realtime(&self, chunk: &MediaChunk) -> Value {
        let msg = json!({
            "kind": kind_str(chunk.kind),
            "data_len": chunk.data.as_ref().map(|d| d.len()),
            "text": chunk.text,
            "mime_type": chunk.mime_type,
            "sample_rate": chunk.sample_rate,
        });
        self.sent_realtime.borrow_mut().push(msg.clone());
        msg
    }

    fn serialize_realtime_control(&self, control: RealtimeControl) -> Value {
        self.sent_controls
            .borrow_mut()
            .push(control_str(control).into());
        json!({"control": control_str(control)})
    }

    fn serialize_turns(&self, items: &[Item], complete: bool) -> Value {
        let turns: Vec<Value> = items.iter().map(|i| self.item_value(i)).collect();
        let msg = json!({"turns": turns, "turn_complete": complete});
        self.sent_turns.borrow_mut().push(msg.clone());
        msg
    }

    fn serialize_tool_result(
        &self,
        call_id: &str,
        name: &str,
        content: &str,
        meta: Option<&Value>,
    ) -> Value {
        let msg = json!({
            "type": "tool_result",
            "call_id": call_id,
            "name": name,
            "content": content,
            "meta": meta.cloned(),
        });
        self.sent_tool_results.borrow_mut().push(msg.clone());
        msg
    }

    fn parse_event(&self, raw: &Value) -> Vec<ParsedEvent> {
        let kind = raw.get("type").and_then(Value::as_str).unwrap_or("");
        let s = |k: &str| raw.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let b = |k: &str| raw.get(k).and_then(Value::as_bool).unwrap_or(false);
        match kind {
            "user_transcript" => vec![ParsedEvent::UserTranscript {
                text: s("text"),
                is_final: b("final"),
            }],
            "agent_transcript" => vec![ParsedEvent::AgentTranscript {
                text: s("text"),
                is_final: b("final"),
            }],
            "tool_call" => vec![ParsedEvent::ToolCallRequest {
                call_id: s("call_id"),
                name: s("name"),
                args: raw.get("args").cloned().unwrap_or_else(|| json!({})),
            }],
            "turn_complete" => vec![ParsedEvent::TurnComplete],
            "interrupted" => vec![ParsedEvent::Interrupted],
            "go_away" => vec![ParsedEvent::GoAway {
                time_left_ms: raw.get("time_left_ms").and_then(Value::as_i64),
            }],
            "resumption" => vec![ParsedEvent::ResumptionUpdate {
                handle: s("handle"),
            }],
            "error" => vec![ParsedEvent::VendorError {
                code: s("code"),
                message: s("message"),
            }],
            _ => vec![],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_event_drives_neutral_events() {
        let m = MockVendorAdapter::default();
        assert_eq!(
            m.parse_event(
                &json!({"type": "tool_call", "call_id": "c1", "name": "t", "args": {"x": 1}})
            ),
            vec![ParsedEvent::ToolCallRequest {
                call_id: "c1".into(),
                name: "t".into(),
                args: json!({"x": 1})
            }]
        );
        assert_eq!(
            m.parse_event(&json!({"type": "interrupted"})),
            vec![ParsedEvent::Interrupted]
        );
        assert_eq!(m.parse_event(&json!({"type": "unknown"})), vec![]);
    }

    #[test]
    fn system_item_downconverted_and_recorded() {
        let m = MockVendorAdapter::default(); // system_content_turn = false
        let v = m.serialize_item(&Item::text(Role::System, "be brief"));
        assert_eq!(v["role"], json!("user"));
        assert_eq!(v["text"], json!("[system] be brief"));
        assert_eq!(v["_downconverted"], json!(true));
        assert_eq!(m.sent_items.borrow().len(), 1);
    }

    #[test]
    fn realtime_control_recorded() {
        let m = MockVendorAdapter::default();
        m.serialize_realtime_control(RealtimeControl::ActivityEnd);
        assert_eq!(
            m.sent_controls.borrow().as_slice(),
            &["activity_end".to_string()]
        );
    }
}
