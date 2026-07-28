//! AgentConnection — the live vendor session (port of `snail.connections`).
//!
//! The **expensive, swappable** half of the `AgentSpec (stable) → AgentConnection (swappable)`
//! split. Send goes through the adapter's serialize seams (vendor-neutral); receive is a bare loop
//! over the transport that forwards each raw message to the session over a channel. Deadline /
//! resumption bookkeeping lives in [`ConnectionMeta`] (the recycle scheduler's input).
//!
//! [`LiveTransport`] is the raw vendor socket surface (native async-in-trait). The real Gemini
//! socket satisfies it; tests inject a fake — so this layer is exercisable without a key. The
//! concrete Gemini Live WebSocket transport is the one remaining credential-gated piece.

use std::future::Future;
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::mpsc;

use snail_core::vendor::{Backend, MediaChunk, RealtimeControl, SetupParam, VendorAdapter};

/// Static agent identity — the pool key. No I/O; no socket.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentSpec {
    pub id: String,
    pub backend: Backend,
    pub setup: SetupParam,
}

impl AgentSpec {
    pub fn model(&self) -> &str {
        &self.setup.model
    }

    /// Stable bucket key: backend + canonical `SetupParam` bytes. Two specs share a pool bucket iff
    /// same backend and byte-identical setup (model, voice, instruction, tools, modality, source).
    pub fn pool_key(&self) -> (Backend, Vec<u8>) {
        let bytes = serde_json::to_vec(&self.setup).expect("SetupParam serializes");
        (self.backend, bytes)
    }
}

/// Lifecycle of a connection (docs 02).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConnectionState {
    Cold,
    Connecting,
    Warm,
    Active,
    Closed,
}

/// Per-connection lifecycle bookkeeping — what the pool's recycle scheduler needs, without the
/// connection re-parsing the stream. Times are on an injected monotonic clock (seconds).
#[derive(Debug, Clone)]
pub struct ConnectionMeta {
    pub created_at: f64,
    pub last_activity: f64,
    pub deadline: Option<f64>,
    pub resumption_handle: Option<String>,
}

impl ConnectionMeta {
    pub fn new(now: f64) -> Self {
        Self {
            created_at: now,
            last_activity: now,
            deadline: None,
            resumption_handle: None,
        }
    }

    pub fn touch(&mut self, now: f64) {
        self.last_activity = now;
    }

    /// Record a vendor termination deadline from a `GoAway`.
    pub fn note_goaway(&mut self, time_left_ms: Option<i64>, now: f64) {
        if let Some(ms) = time_left_ms {
            self.deadline = Some(now + ms as f64 / 1000.0);
        }
    }

    pub fn note_resumption(&mut self, handle: String) {
        self.resumption_handle = Some(handle);
    }

    /// Seconds until the vendor deadline, or `None` if no deadline known.
    pub fn ttl_headroom(&self, now: f64) -> Option<f64> {
        self.deadline.map(|d| d - now)
    }

    /// True when within `margin` seconds of the deadline (recycle-due).
    pub fn recycle_due(&self, now: f64, margin: f64) -> bool {
        self.ttl_headroom(now).is_some_and(|head| head <= margin)
    }
}

/// The raw vendor socket surface the connection drives. Native async-in-trait; a fake stands in for
/// tests. Send methods take the value the adapter's serialize seams produce.
pub trait LiveTransport {
    fn send_realtime_input(&mut self, msg: Value) -> impl Future<Output = ()> + Send;
    fn send_client_content(&mut self, msg: Value) -> impl Future<Output = ()> + Send;
    fn send_tool_response(&mut self, resp: Value) -> impl Future<Output = ()> + Send;
    /// One raw inbound message, or `None` when the socket closes.
    fn recv(&mut self) -> impl Future<Output = Option<Value>> + Send;
    fn close(&mut self) -> impl Future<Output = ()> + Send;
}

/// One live vendor session for an [`AgentSpec`], generic over its transport.
pub struct AgentConnection<T: LiveTransport> {
    spec: AgentSpec,
    adapter: Arc<dyn VendorAdapter>,
    transport: T,
    state: ConnectionState,
    meta: ConnectionMeta,
}

impl<T: LiveTransport> AgentConnection<T> {
    pub fn new(spec: AgentSpec, adapter: Arc<dyn VendorAdapter>, transport: T, now: f64) -> Self {
        Self {
            spec,
            adapter,
            transport,
            state: ConnectionState::Warm,
            meta: ConnectionMeta::new(now),
        }
    }

    pub fn id(&self) -> &str {
        &self.spec.id
    }
    pub fn spec(&self) -> &AgentSpec {
        &self.spec
    }
    pub fn state(&self) -> ConnectionState {
        self.state
    }
    pub fn meta(&self) -> &ConnectionMeta {
        &self.meta
    }

    pub fn activate(&mut self) {
        self.state = ConnectionState::Active;
    }
    pub fn park(&mut self) {
        self.state = ConnectionState::Warm;
    }

    // --- outbound seams (neutral → vendor) --------------------------------

    pub async fn send_realtime(&mut self, chunk: &MediaChunk) {
        let msg = self.adapter.serialize_realtime(chunk);
        self.transport.send_realtime_input(msg).await;
    }

    pub async fn send_realtime_control(&mut self, control: RealtimeControl) {
        let msg = self.adapter.serialize_realtime_control(control);
        self.transport.send_realtime_input(msg).await;
    }

    pub async fn send_tool_result(&mut self, function_response: Value) {
        self.transport.send_tool_response(function_response).await;
    }

    // --- inbound read loop ------------------------------------------------

    /// Pump `transport.recv()` into `raw_tx` until the socket closes. Each message refreshes
    /// `last_activity`. (Audio extraction is the adapter/session boundary — the raw stream carries
    /// everything; the session extracts.)
    pub async fn run(&mut self, raw_tx: mpsc::UnboundedSender<Value>, clock: impl Fn() -> f64) {
        while let Some(msg) = self.transport.recv().await {
            self.meta.touch(clock());
            if raw_tx.send(msg).is_err() {
                break; // session gone
            }
        }
    }

    pub async fn close(&mut self) {
        self.transport.close().await;
        self.state = ConnectionState::Closed;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use snail_core::vendor::{MockVendorAdapter, ResponseModality};

    fn spec(id: &str, model: &str) -> AgentSpec {
        AgentSpec {
            id: id.into(),
            backend: Backend::Mock,
            setup: SetupParam::new(model),
        }
    }

    #[test]
    fn pool_key_groups_identical_setups() {
        let a = spec("a", "m1");
        let b = spec("b", "m1"); // same setup, different id
        let c = spec("c", "m2");
        assert_eq!(a.pool_key(), b.pool_key()); // id is not part of the key
        assert_ne!(a.pool_key(), c.pool_key()); // different model → different bucket
    }

    #[test]
    fn pool_key_distinguishes_modality() {
        let mut s1 = spec("a", "m");
        let mut s2 = spec("b", "m");
        s2.setup.response_modality = ResponseModality::Text;
        s1.setup.response_modality = ResponseModality::Audio;
        assert_ne!(s1.pool_key(), s2.pool_key());
    }

    #[test]
    fn meta_recycle_due_within_margin() {
        let mut m = ConnectionMeta::new(0.0);
        assert!(!m.recycle_due(0.0, 5.0)); // no deadline yet
        m.note_goaway(Some(10_000), 0.0); // deadline at t=10s
        assert!(!m.recycle_due(4.0, 5.0)); // 6s headroom > 5s margin
        assert!(m.recycle_due(6.0, 5.0)); // 4s headroom <= 5s margin
        assert_eq!(m.ttl_headroom(7.0), Some(3.0));
    }

    #[test]
    fn meta_resumption_handle_recorded() {
        let mut m = ConnectionMeta::new(0.0);
        m.note_resumption("h-123".into());
        assert_eq!(m.resumption_handle.as_deref(), Some("h-123"));
    }

    // A fake transport exercises the send seams + read loop without a socket.
    struct FakeTransport {
        inbox: std::collections::VecDeque<Value>,
        pub sent_realtime: Vec<Value>,
    }
    impl LiveTransport for FakeTransport {
        async fn send_realtime_input(&mut self, msg: Value) {
            self.sent_realtime.push(msg);
        }
        async fn send_client_content(&mut self, _msg: Value) {}
        async fn send_tool_response(&mut self, _resp: Value) {}
        async fn recv(&mut self) -> Option<Value> {
            self.inbox.pop_front()
        }
        async fn close(&mut self) {}
    }

    #[tokio::test]
    async fn run_forwards_raw_messages_and_send_seams_serialize() {
        let adapter = Arc::new(MockVendorAdapter::default());
        let transport = FakeTransport {
            inbox: [serde_json::json!({"type": "turn_complete"})]
                .into_iter()
                .collect(),
            sent_realtime: Vec::new(),
        };
        let mut conn = AgentConnection::new(spec("a", "m"), adapter, transport, 0.0);
        conn.send_realtime(&MediaChunk::audio(vec![0u8; 4], 16000))
            .await;

        let (tx, mut rx) = mpsc::unbounded_channel();
        conn.run(tx, || 1.0).await;
        assert_eq!(
            rx.recv().await.unwrap()["type"],
            serde_json::json!("turn_complete")
        );
        assert_eq!(conn.meta().last_activity, 1.0); // touched by the message
    }
}
