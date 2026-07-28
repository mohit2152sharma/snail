//! Append-only event log (port of `snail.context.log`).
//!
//! The single source of truth. Append-only: no event is ever mutated or removed, which is what
//! makes concurrent projections torn-read-free. Projections are point-in-time snapshots taken at
//! turn/handoff boundaries.

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use super::events::{Event, EventType};
use super::projection::Projection;

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Ordered, append-only sequence of [`Event`]. `seq` is a monotonic counter assigned here.
#[derive(Default)]
pub struct EventLog {
    events: Vec<Event>,
    seq: u64,
}

impl EventLog {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append a new event, assigning it the next `seq`. `ts` defaults to wall-clock when `None`.
    pub fn append(
        &mut self,
        kind: EventType,
        agent_id: Option<String>,
        content: impl Into<String>,
        meta: Option<Value>,
        ts: Option<f64>,
    ) -> &Event {
        let event = Event {
            seq: self.seq,
            ts: ts.unwrap_or_else(now_secs),
            kind,
            agent_id,
            content: content.into(),
            meta,
        };
        self.events.push(event);
        self.seq += 1;
        self.events.last().unwrap()
    }

    /// Yield events matching the given type / agent constraints (in order). `None` = no constraint.
    pub fn filter<'a>(
        &'a self,
        types: Option<&'a [EventType]>,
        agents: Option<&'a [String]>,
    ) -> impl Iterator<Item = &'a Event> + 'a {
        self.events.iter().filter(move |e| {
            if let Some(ts) = types {
                if !ts.contains(&e.kind) {
                    return false;
                }
            }
            if let Some(ag) = agents {
                match &e.agent_id {
                    Some(a) if ag.iter().any(|x| x == a) => {}
                    _ => return false,
                }
            }
            true
        })
    }

    /// Compute a point-in-time `Vec<Item>` snapshot via a declarative [`Projection`].
    pub fn project(&self, projection: &Projection) -> Vec<super::events::Item> {
        projection.apply(self)
    }

    pub fn events(&self) -> &[Event] {
        &self.events
    }
    pub fn len(&self) -> usize {
        self.events.len()
    }
    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn append_assigns_monotonic_seq() {
        let mut log = EventLog::new();
        log.append(EventType::UserSpeech, None, "hi", None, Some(1.0));
        log.append(
            EventType::AgentSpeech,
            Some("a".into()),
            "yo",
            None,
            Some(2.0),
        );
        assert_eq!(log.events()[0].seq, 0);
        assert_eq!(log.events()[1].seq, 1);
        assert_eq!(log.len(), 2);
    }

    #[test]
    fn filter_by_type_and_agent() {
        let mut log = EventLog::new();
        log.append(EventType::UserSpeech, None, "u", None, Some(1.0));
        log.append(
            EventType::AgentSpeech,
            Some("a".into()),
            "x",
            None,
            Some(2.0),
        );
        log.append(
            EventType::AgentSpeech,
            Some("b".into()),
            "y",
            None,
            Some(3.0),
        );
        let agents = ["a".to_string()];
        let got: Vec<_> = log
            .filter(Some(&[EventType::AgentSpeech]), Some(&agents))
            .collect();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].content, "x");
    }
}
