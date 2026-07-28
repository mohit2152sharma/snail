//! ConnectionPool — per-AgentSpec warm-socket pool (port of `snail.connections.pool`).
//!
//! Realtime sessions are stateful + conversation-bound, so there is no cross-session reuse: the
//! pool is scoped to one user-session, keyed per [`AgentSpec::pool_key`]. Its job is to take the
//! ~100–300ms handshake off the critical path by pre-opening the likely-next agent.
//!
//! The [`Connector`] is the only thing that touches the network (`open` = handshake + config); the
//! real Gemini connector is credential-gated, tests inject a fake. Ownership-adapted from Python:
//! the pool owns **standby** connections in buckets; an acquired connection is moved to the caller
//! (the Router owns the active socket) and returns via [`ConnectionPool::park`]. `live_out` tracks
//! handed-out sockets so the warm cap still counts them.

use std::collections::HashMap;
use std::future::Future;

use snail_core::vendor::Backend;

use crate::connection::{AgentConnection, AgentSpec, LiveTransport};

/// Opens a live transport for a spec (optionally resuming a prior session). The pool owns *when* to
/// open (pre-warm / lazy / recycle); the connector owns *how*.
pub trait Connector {
    type Transport: LiveTransport;
    fn open(
        &self,
        spec: &AgentSpec,
        resumption_handle: Option<&str>,
    ) -> impl Future<Output = Self::Transport> + Send;
}

type Key = (Backend, Vec<u8>);

/// Per-`AgentSpec` pool of warm vendor sockets for one user-session.
pub struct ConnectionPool<C: Connector> {
    connector: C,
    adapter: std::sync::Arc<dyn snail_core::vendor::VendorAdapter>,
    max_warm: usize,
    standby: HashMap<Key, Vec<AgentConnection<C::Transport>>>,
    live_out: usize,
    clock: fn() -> f64,
}

impl<C: Connector> ConnectionPool<C> {
    pub fn new(
        connector: C,
        adapter: std::sync::Arc<dyn snail_core::vendor::VendorAdapter>,
        max_warm: usize,
        clock: fn() -> f64,
    ) -> Self {
        Self {
            connector,
            adapter,
            max_warm,
            standby: HashMap::new(),
            live_out: 0,
            clock,
        }
    }

    /// Total warm sockets this pool owns (standby + handed-out).
    pub fn warm_count(&self) -> usize {
        self.standby_total() + self.live_out
    }

    fn standby_total(&self) -> usize {
        self.standby.values().map(Vec::len).sum()
    }

    fn at_cap(&self) -> bool {
        self.warm_count() >= self.max_warm
    }

    /// Open a standby for `spec` ahead of need. `None` if at the warm cap (best-effort by contract
    /// — a full pool declines rather than evicting; the caller lazily [`acquire`](Self::acquire)s).
    pub async fn prewarm(&mut self, spec: &AgentSpec) -> bool {
        if self.at_cap() {
            return false;
        }
        let conn = self.open_conn(spec).await;
        self.standby.entry(spec.pool_key()).or_default().push(conn);
        true
    }

    /// Get a warm connection for `spec`. Reuses a matching standby (the pre-warm payoff) else lazily
    /// connects, evicting the stalest standby first if at the cap. The returned connection is WARM.
    pub async fn acquire(&mut self, spec: &AgentSpec) -> AgentConnection<C::Transport> {
        let key = spec.pool_key();
        let conn = if let Some(conn) = self.standby.get_mut(&key).and_then(Vec::pop) {
            conn
        } else {
            if self.at_cap() {
                self.evict_one().await;
            }
            self.open_conn(spec).await
        };
        self.live_out += 1;
        conn
    }

    /// Return an ex-active connection to its bucket as a warm standby (fast re-promotion).
    pub fn park(&mut self, mut conn: AgentConnection<C::Transport>) {
        conn.park();
        self.live_out = self.live_out.saturating_sub(1);
        self.standby
            .entry(conn.spec().pool_key())
            .or_default()
            .push(conn);
    }

    /// Close and drop a handed-out connection the pool owns (end of a user-session).
    pub async fn release(&mut self, mut conn: AgentConnection<C::Transport>) {
        self.live_out = self.live_out.saturating_sub(1);
        conn.close().await;
    }

    /// Standby connections within `margin` seconds of their vendor deadline (the recycle scheduler's
    /// query — it decides *when* to call recycle; active sockets are recycled by their owner).
    pub fn due_for_recycle(&self, margin: f64) -> Vec<&str> {
        let now = (self.clock)();
        self.standby
            .values()
            .flatten()
            .filter(|c| c.meta().recycle_due(now, margin))
            .map(|c| c.id())
            .collect()
    }

    /// Close standbys idle longer than `older_than` seconds. Returns count.
    pub async fn evict_idle(&mut self, older_than: f64) -> usize {
        let now = (self.clock)();
        let mut evicted = 0;
        let mut to_close = Vec::new();
        for bucket in self.standby.values_mut() {
            let mut keep = Vec::new();
            for conn in bucket.drain(..) {
                if now - conn.meta().last_activity > older_than {
                    to_close.push(conn);
                } else {
                    keep.push(conn);
                }
            }
            *bucket = keep;
        }
        for mut conn in to_close {
            conn.close().await;
            evicted += 1;
        }
        evicted
    }

    /// Close every standby connection this pool owns.
    pub async fn aclose(&mut self) {
        let all: Vec<_> = self.standby.drain().flat_map(|(_, v)| v).collect();
        for mut conn in all {
            conn.close().await;
        }
    }

    pub fn standby_count(&self) -> usize {
        self.standby_total()
    }

    async fn open_conn(&self, spec: &AgentSpec) -> AgentConnection<C::Transport> {
        let transport = self.connector.open(spec, None).await;
        AgentConnection::new(
            spec.clone(),
            self.adapter.clone(),
            transport,
            (self.clock)(),
        )
    }

    /// Drop the stalest standby to free a warm slot for a live acquire.
    async fn evict_one(&mut self) {
        let mut stalest: Option<(Key, usize, f64)> = None;
        for (key, bucket) in &self.standby {
            for (i, conn) in bucket.iter().enumerate() {
                let la = conn.meta().last_activity;
                if stalest.as_ref().map_or(true, |(_, _, best)| la < *best) {
                    stalest = Some((key.clone(), i, la));
                }
            }
        }
        if let Some((key, idx, _)) = stalest {
            let mut conn = self.standby.get_mut(&key).unwrap().remove(idx);
            conn.close().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::Value;
    use snail_core::vendor::{MockVendorAdapter, SetupParam};

    use super::*;

    struct FakeTransport;
    impl LiveTransport for FakeTransport {
        async fn send_realtime_input(&mut self, _m: Value) {}
        async fn send_client_content(&mut self, _m: Value) {}
        async fn send_tool_response(&mut self, _r: Value) {}
        async fn recv(&mut self) -> Option<Value> {
            None
        }
        async fn close(&mut self) {}
    }

    struct FakeConnector {
        opens: std::sync::atomic::AtomicUsize,
    }
    impl Connector for FakeConnector {
        type Transport = FakeTransport;
        async fn open(&self, _spec: &AgentSpec, _r: Option<&str>) -> FakeTransport {
            self.opens
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            FakeTransport
        }
    }

    fn spec(id: &str, model: &str) -> AgentSpec {
        AgentSpec {
            id: id.into(),
            backend: Backend::Mock,
            setup: SetupParam::new(model),
        }
    }

    fn pool() -> ConnectionPool<FakeConnector> {
        ConnectionPool::new(
            FakeConnector {
                opens: std::sync::atomic::AtomicUsize::new(0),
            },
            Arc::new(MockVendorAdapter::default()),
            2,
            || 0.0,
        )
    }

    #[tokio::test]
    async fn prewarm_then_acquire_reuses_standby() {
        let mut p = pool();
        assert!(p.prewarm(&spec("a", "m")).await);
        assert_eq!(p.standby_count(), 1);
        let _conn = p.acquire(&spec("a", "m")).await; // reuses the standby (no new open)
        assert_eq!(
            p.connector.opens.load(std::sync::atomic::Ordering::Relaxed),
            1
        ); // only the prewarm opened
        assert_eq!(p.standby_count(), 0);
        assert_eq!(p.warm_count(), 1); // now handed out
    }

    #[tokio::test]
    async fn prewarm_declines_at_cap() {
        let mut p = pool(); // max_warm = 2
        assert!(p.prewarm(&spec("a", "m")).await);
        assert!(p.prewarm(&spec("b", "m2")).await);
        assert!(!p.prewarm(&spec("c", "m3")).await); // at cap → declines
    }

    #[tokio::test]
    async fn park_returns_to_bucket_for_reuse() {
        let mut p = pool();
        let conn = p.acquire(&spec("a", "m")).await;
        assert_eq!(p.live_out, 1);
        p.park(conn);
        assert_eq!(p.live_out, 0);
        assert_eq!(p.standby_count(), 1); // back as a warm standby
    }

    #[tokio::test]
    async fn acquire_at_cap_evicts_stalest_standby() {
        let mut p = pool(); // cap 2
        p.prewarm(&spec("a", "m")).await; // standby 1
        p.prewarm(&spec("b", "m2")).await; // standby 2 → at cap
                                           // acquire a THIRD distinct spec → at cap with no matching standby → evict one, then open
        let _conn = p.acquire(&spec("c", "m3")).await;
        assert_eq!(p.standby_count(), 1); // one standby evicted
        assert_eq!(p.warm_count(), 2); // 1 standby + 1 live-out
    }
}
