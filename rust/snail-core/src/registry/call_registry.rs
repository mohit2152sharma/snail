//! ToolCallRegistry — the in-flight tracker (port of `snail.registry.call_registry`).
//!
//! Guardian of the invariant: **every `call_id` resolves to exactly one terminal result — never
//! zero, never two.** Single-resolution is enforced structurally: a terminal transition removes
//! the entry, so any later resolve/cancel/timeout for that id finds nothing and no-ops.

use std::collections::{HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::tools::result::ToolResult;

use super::pending::{CallState, Destination, PendingCall, Promise};

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Error registering a call.
#[derive(Debug, PartialEq, Eq)]
pub enum RegisterError {
    /// duplicate `call_id`.
    Duplicate,
    /// concurrent in-flight cap hit (backpressure, docs 04).
    Full,
}

/// Optional registration fields (defaults match the Python keyword args).
pub struct RegisterOpts {
    pub origin_connection_id: Option<String>,
    pub destination: Destination,
    pub deadline: Option<f64>,
    pub response_group_id: Option<String>,
    pub schedule: Option<String>,
    pub now: Option<f64>,
}

impl Default for RegisterOpts {
    fn default() -> Self {
        Self {
            origin_connection_id: None,
            destination: Destination::Handler,
            deadline: None,
            response_group_id: None,
            schedule: None,
            now: None,
        }
    }
}

pub struct ToolCallRegistry {
    entries: HashMap<String, PendingCall>,
    by_group: HashMap<String, HashSet<String>>,
    by_conn: HashMap<String, HashSet<String>>,
    max_concurrent: usize,
}

impl ToolCallRegistry {
    pub fn new(max_concurrent: usize) -> Self {
        Self {
            entries: HashMap::new(),
            by_group: HashMap::new(),
            by_conn: HashMap::new(),
            max_concurrent,
        }
    }

    pub fn with_defaults() -> Self {
        Self::new(64)
    }

    /// Create an entry for a freshly-emitted vendor call.
    pub fn register(
        &mut self,
        call_id: &str,
        tool_name: &str,
        args: Value,
        opts: RegisterOpts,
    ) -> Result<(), RegisterError> {
        if self.entries.contains_key(call_id) {
            return Err(RegisterError::Duplicate);
        }
        if self.entries.len() >= self.max_concurrent {
            return Err(RegisterError::Full);
        }
        let entry = PendingCall {
            call_id: call_id.to_string(),
            tool_name: tool_name.to_string(),
            args,
            origin_connection_id: opts.origin_connection_id.clone(),
            destination: opts.destination,
            state: CallState::Received,
            future: Promise::new(),
            created_at: opts.now.unwrap_or_else(now_secs),
            deadline: opts.deadline,
            response_group_id: opts.response_group_id.clone(),
            schedule: opts.schedule,
        };
        if let Some(gid) = &opts.response_group_id {
            self.by_group
                .entry(gid.clone())
                .or_default()
                .insert(call_id.to_string());
        }
        if let Some(cid) = &opts.origin_connection_id {
            self.by_conn
                .entry(cid.clone())
                .or_default()
                .insert(call_id.to_string());
        }
        self.entries.insert(call_id.to_string(), entry);
        Ok(())
    }

    /// Move a live entry to a non-terminal lifecycle state. Returns false on missing/terminal.
    pub fn advance(&mut self, call_id: &str, state: CallState) -> bool {
        assert!(
            !state.is_terminal(),
            "use resolve()/cancel() for terminal states"
        );
        match self.entries.get_mut(call_id) {
            Some(e) if !e.is_terminal() => {
                e.state = state;
                true
            }
            _ => false,
        }
    }

    /// Resolve a call with its terminal result. First terminal wins; returns whether it resolved
    /// a live entry (false = already terminal / late / dropped).
    pub fn resolve(&mut self, call_id: &str, result: ToolResult) -> bool {
        self.terminate(call_id, CallState::Done, result)
    }

    pub fn cancel(&mut self, call_id: &str, reason: Option<String>) -> bool {
        self.terminate(call_id, CallState::Cancelled, ToolResult::cancelled(reason))
    }

    /// Cancel all calls in a response batch (barge-in scope). Returns the count.
    pub fn sweep_response_group(&mut self, response_group_id: &str) -> usize {
        let ids: Vec<String> = self
            .by_group
            .get(response_group_id)
            .map(|s| s.iter().cloned().collect())
            .unwrap_or_default();
        self.sweep(&ids)
    }

    /// Cancel all of a connection's calls (handoff/close scope).
    pub fn sweep_connection(&mut self, connection_id: &str) -> usize {
        let ids: Vec<String> = self
            .by_conn
            .get(connection_id)
            .map(|s| s.iter().cloned().collect())
            .unwrap_or_default();
        self.sweep(&ids)
    }

    pub fn sweep_all(&mut self) -> usize {
        let ids: Vec<String> = self.entries.keys().cloned().collect();
        self.sweep(&ids)
    }

    /// Resolve every entry past its deadline as `timeout`. Returns their ids.
    pub fn sweep_timeouts(&mut self, now: Option<f64>) -> Vec<String> {
        let t = now.unwrap_or_else(now_secs);
        let expired: Vec<String> = self
            .entries
            .iter()
            .filter(|(_, e)| e.deadline.is_some_and(|d| d <= t) && !e.is_terminal())
            .map(|(id, _)| id.clone())
            .collect();
        for id in &expired {
            self.terminate(id, CallState::Timeout, ToolResult::timeout());
        }
        expired
    }

    fn sweep(&mut self, call_ids: &[String]) -> usize {
        let mut count = 0;
        for cid in call_ids {
            if self.terminate(cid, CallState::Cancelled, ToolResult::cancelled(None)) {
                count += 1;
            }
        }
        count
    }

    fn terminate(&mut self, call_id: &str, state: CallState, result: ToolResult) -> bool {
        let mut entry = match self.entries.remove(call_id) {
            Some(e) => e,
            None => return false, // already terminal + removed, or never existed → no-op
        };
        entry.state = state;
        entry.future.set_result(result);
        if let Some(gid) = &entry.response_group_id {
            if let Some(set) = self.by_group.get_mut(gid) {
                set.remove(call_id);
                if set.is_empty() {
                    self.by_group.remove(gid);
                }
            }
        }
        if let Some(cid) = &entry.origin_connection_id {
            if let Some(set) = self.by_conn.get_mut(cid) {
                set.remove(call_id);
                if set.is_empty() {
                    self.by_conn.remove(cid);
                }
            }
        }
        true
    }

    pub fn get(&self, call_id: &str) -> Option<&PendingCall> {
        self.entries.get(call_id)
    }
    pub fn contains(&self, call_id: &str) -> bool {
        self.entries.contains_key(call_id)
    }
    pub fn in_flight(&self) -> usize {
        self.entries.len()
    }
    pub fn group_size(&self, response_group_id: &str) -> usize {
        self.by_group.get(response_group_id).map_or(0, HashSet::len)
    }
    pub fn group_call_ids(&self, response_group_id: &str) -> Vec<String> {
        self.by_group
            .get(response_group_id)
            .map(|s| s.iter().cloned().collect())
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reg() -> ToolCallRegistry {
        ToolCallRegistry::with_defaults()
    }

    fn opts_group(gid: &str) -> RegisterOpts {
        RegisterOpts {
            response_group_id: Some(gid.to_string()),
            ..Default::default()
        }
    }

    #[test]
    fn register_rejects_duplicate() {
        let mut r = reg();
        assert!(r
            .register("c1", "t", json!({}), RegisterOpts::default())
            .is_ok());
        assert_eq!(
            r.register("c1", "t", json!({}), RegisterOpts::default()),
            Err(RegisterError::Duplicate)
        );
    }

    #[test]
    fn cap_enforced() {
        let mut r = ToolCallRegistry::new(1);
        r.register("c1", "t", json!({}), RegisterOpts::default())
            .unwrap();
        assert_eq!(
            r.register("c2", "t", json!({}), RegisterOpts::default()),
            Err(RegisterError::Full)
        );
    }

    #[test]
    fn resolve_is_single_terminal() {
        let mut r = reg();
        r.register("c1", "t", json!({}), RegisterOpts::default())
            .unwrap();
        assert!(r.resolve("c1", ToolResult::success(None)));
        assert!(!r.resolve("c1", ToolResult::success(None))); // second is a no-op
        assert!(!r.contains("c1"));
        assert_eq!(r.in_flight(), 0);
    }

    #[test]
    fn advance_rejects_terminal_entry() {
        let mut r = reg();
        r.register("c1", "t", json!({}), RegisterOpts::default())
            .unwrap();
        r.resolve("c1", ToolResult::success(None));
        assert!(!r.advance("c1", CallState::Executing)); // gone → false
    }

    #[test]
    fn sweep_group_cancels_batch() {
        let mut r = reg();
        r.register("c1", "t", json!({}), opts_group("g1")).unwrap();
        r.register("c2", "t", json!({}), opts_group("g1")).unwrap();
        r.register("c3", "t", json!({}), opts_group("g2")).unwrap();
        assert_eq!(r.sweep_response_group("g1"), 2);
        assert_eq!(r.group_size("g1"), 0);
        assert!(r.contains("c3"));
    }

    #[test]
    fn sweep_timeouts_resolves_expired() {
        let mut r = reg();
        r.register(
            "c1",
            "t",
            json!({}),
            RegisterOpts {
                deadline: Some(100.0),
                ..Default::default()
            },
        )
        .unwrap();
        r.register("c2", "t", json!({}), RegisterOpts::default())
            .unwrap();
        let expired = r.sweep_timeouts(Some(200.0));
        assert_eq!(expired, vec!["c1".to_string()]);
        assert!(r.contains("c2"));
    }
}
