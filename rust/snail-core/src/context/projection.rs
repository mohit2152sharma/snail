//! Declarative projection spec — Mode 1 (port of `snail.context.projection`).
//!
//! A projection is a filter/transform over the log producing a vendor-neutral `Vec<Item>`. Mode 1
//! (this file) is the safe, cacheable, declarative default. Both modes stop at `Vec<Item>` — the
//! adapter serializes. (Mode 2 imperative builders are a caller-supplied closure in Rust.)

use super::events::{Event, EventType, Item, Role};
use super::log::EventLog;

fn meta_str(event: &Event, key: &str) -> Option<String> {
    event
        .meta
        .as_ref()
        .and_then(|m| m.get(key))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Map one log event to its neutral conversation item, or `None` for non-turn control events.
fn event_to_item(event: &Event) -> Option<Item> {
    match event.kind {
        EventType::UserSpeech => Some(Item::text(Role::User, event.content.clone())),
        EventType::AgentSpeech => Some(Item::text(Role::Model, event.content.clone())),
        EventType::ExternalContext => Some(Item::text(Role::System, event.content.clone())),
        EventType::ToolCall => Some(Item {
            role: Role::Model,
            text: String::new(),
            name: meta_str(event, "tool_name"),
            tool_call_id: meta_str(event, "tool_call_id"),
            args: event.meta.as_ref().and_then(|m| m.get("args").cloned()),
        }),
        EventType::ToolResult => Some(Item {
            role: Role::Tool,
            text: event.content.clone(),
            name: meta_str(event, "tool_name"),
            tool_call_id: meta_str(event, "tool_call_id"),
            args: None,
        }),
        // HANDOFF and any future control-only events: not conversation context.
        EventType::Handoff => None,
    }
}

/// A declarative filter/transform over the log → `Vec<Item>` (Mode 1).
#[derive(Debug, Clone, Default)]
pub struct Projection {
    /// Event types to include. `None` = all conversation types.
    pub include: Option<Vec<EventType>>,
    /// Which agents this projection may see. `None` = all.
    pub agents: Option<Vec<String>>,
    /// Recency window: keep only the last N matching events. `None` = unbounded.
    pub last_n: Option<usize>,
    /// System-instruction text, prepended as a leading SYSTEM item when set.
    pub instructions: Option<String>,
    /// External context items appended after the conversation.
    pub extra: Vec<Item>,
}

impl Projection {
    /// Project `log` into a point-in-time `Vec<Item>`.
    pub fn apply(&self, log: &EventLog) -> Vec<Item> {
        let mut events: Vec<&Event> = log
            .filter(self.include.as_deref(), self.agents.as_deref())
            .collect();
        if let Some(n) = self.last_n {
            if events.len() > n {
                events = events.split_off(events.len() - n);
            }
        }

        let mut items = Vec::new();
        if let Some(instr) = &self.instructions {
            if !instr.is_empty() {
                items.push(Item::text(Role::System, instr.clone()));
            }
        }
        for e in events {
            if let Some(item) = event_to_item(e) {
                items.push(item);
            }
        }
        items.extend(self.extra.iter().cloned());
        items
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn log() -> EventLog {
        let mut l = EventLog::new();
        l.append(EventType::UserSpeech, None, "hello", None, Some(1.0));
        l.append(
            EventType::ToolCall,
            Some("a".into()),
            "",
            Some(json!({"tool_name": "lookup", "tool_call_id": "c1", "args": {"q": 1}})),
            Some(2.0),
        );
        l.append(
            EventType::ToolResult,
            Some("a".into()),
            "42",
            Some(json!({"tool_name": "lookup", "tool_call_id": "c1"})),
            Some(3.0),
        );
        l.append(EventType::Handoff, None, "", None, Some(4.0));
        l.append(
            EventType::AgentSpeech,
            Some("a".into()),
            "the answer is 42",
            None,
            Some(5.0),
        );
        l
    }

    #[test]
    fn projects_turns_skips_handoff() {
        let items = Projection::default().apply(&log());
        // user, tool_call (model), tool_result (tool), agent_speech (model) — handoff dropped
        assert_eq!(items.len(), 4);
        assert_eq!(items[0].role, Role::User);
        assert_eq!(items[1].role, Role::Model);
        assert_eq!(items[1].name.as_deref(), Some("lookup"));
        assert_eq!(items[1].args, Some(json!({"q": 1})));
        assert_eq!(items[2].role, Role::Tool);
        assert_eq!(items[2].text, "42");
        assert_eq!(items[3].role, Role::Model);
    }

    #[test]
    fn instructions_prepended_and_extra_appended() {
        let p = Projection {
            include: Some(vec![EventType::UserSpeech]),
            instructions: Some("be nice".into()),
            extra: vec![Item::text(Role::System, "acct docs")],
            ..Default::default()
        };
        let items = p.apply(&log());
        assert_eq!(items[0].text, "be nice");
        assert_eq!(items[0].role, Role::System);
        assert_eq!(items[1].role, Role::User);
        assert_eq!(items.last().unwrap().text, "acct docs");
    }

    #[test]
    fn last_n_truncates() {
        let p = Projection {
            last_n: Some(1),
            ..Default::default()
        };
        // last matching conversation event is the agent speech
        let items = p.apply(&log());
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].text, "the answer is 42");
    }
}
