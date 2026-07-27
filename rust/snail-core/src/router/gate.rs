//! OutputGate — GATE 2: single-producer output ring + atomic ownership token
//! (port of `snail.router.gate`).
//!
//! Only the **token holder** (the active agent) may write audio that reaches the user;
//! everyone else's write is suppressed. Promotion = atomic token transfer. With one token,
//! overlap is structurally impossible — worst case is a gap or a clipped tail, never two voices.
//!
//! Egress frames are plain int16 vectors (egress is a single active stream, not pooled), so the
//! gate owns `Vec<i16>` payloads directly.

use std::collections::VecDeque;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GateStats {
    pub queued: usize,
    pub suppressed_total: u64,
    pub dropped_total: u64,
}

pub struct OutputGate {
    holder: Option<String>,
    ring: VecDeque<Vec<i16>>,
    depth: usize,
    suppressed: u64,
    dropped: u64,
}

impl OutputGate {
    pub fn new(depth: usize) -> Self {
        assert!(depth >= 1, "depth must be >= 1");
        Self {
            holder: None,
            ring: VecDeque::new(),
            depth,
            suppressed: 0,
            dropped: 0,
        }
    }

    pub fn with_defaults() -> Self {
        Self::new(32)
    }

    pub fn holder(&self) -> Option<&str> {
        self.holder.as_deref()
    }

    /// Give the token to `agent_id`. Must be free (use [`OutputGate::transfer`] to move it).
    pub fn grant(&mut self, agent_id: &str) {
        if let Some(h) = &self.holder {
            assert!(
                h == agent_id,
                "token held by {h:?}; use transfer() to move it"
            );
        }
        self.holder = Some(agent_id.to_string());
    }

    /// Drop the token (user-facing drain stops). Ring is left intact. Returns old holder.
    pub fn revoke(&mut self) -> Option<String> {
        self.holder.take()
    }

    /// Atomically move the token to `new_agent_id`. Returns the old holder.
    pub fn transfer(&mut self, new_agent_id: &str) -> Option<String> {
        self.holder.replace(new_agent_id.to_string())
    }

    /// Enqueue `frame` for the user — only if `agent_id` holds the token. Non-holder writes are
    /// suppressed; a full ring evicts oldest (playout: newest matters most). Returns whether
    /// the frame was enqueued.
    pub fn write(&mut self, agent_id: &str, frame: Vec<i16>) -> bool {
        if self.holder.as_deref() != Some(agent_id) {
            self.suppressed += 1;
            self.dropped += 1;
            return false;
        }
        if self.ring.len() >= self.depth {
            self.ring.pop_front();
            self.dropped += 1;
        }
        self.ring.push_back(frame);
        true
    }

    /// Paced drain to the speaker. `None` if empty.
    pub fn pop(&mut self) -> Option<Vec<i16>> {
        self.ring.pop_front()
    }

    /// Drop all queued audio (the CUT_NOW hard cut). Returns the count dropped.
    pub fn flush(&mut self) -> usize {
        let n = self.ring.len();
        self.dropped += n as u64;
        self.ring.clear();
        n
    }

    pub fn len(&self) -> usize {
        self.ring.len()
    }
    pub fn is_empty(&self) -> bool {
        self.ring.is_empty()
    }

    pub fn stats(&self) -> GateStats {
        GateStats {
            queued: self.ring.len(),
            suppressed_total: self.suppressed,
            dropped_total: self.dropped,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_holder_writes() {
        let mut g = OutputGate::new(8);
        g.grant("a");
        assert!(g.write("a", vec![1, 2]));
        assert!(!g.write("b", vec![3, 4])); // suppressed
        assert_eq!(g.stats().suppressed_total, 1);
        assert_eq!(g.len(), 1);
    }

    #[test]
    fn transfer_is_atomic_single_holder() {
        let mut g = OutputGate::new(8);
        g.grant("a");
        assert_eq!(g.transfer("b"), Some("a".to_string()));
        assert!(!g.write("a", vec![1]));
        assert!(g.write("b", vec![2]));
    }

    #[test]
    fn full_ring_evicts_oldest() {
        let mut g = OutputGate::new(2);
        g.grant("a");
        g.write("a", vec![1]);
        g.write("a", vec![2]);
        g.write("a", vec![3]); // evicts [1]
        assert_eq!(g.pop().unwrap(), vec![2]);
        assert_eq!(g.pop().unwrap(), vec![3]);
        assert_eq!(g.stats().dropped_total, 1);
    }

    #[test]
    fn flush_clears() {
        let mut g = OutputGate::new(8);
        g.grant("a");
        g.write("a", vec![1]);
        g.write("a", vec![2]);
        assert_eq!(g.flush(), 2);
        assert!(g.is_empty());
    }
}
