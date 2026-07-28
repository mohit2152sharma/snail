//! ClientBridge — wires one client socket to one live agent (port of `snail.transport.bridge`).
//!
//! The transport layer's default behaviour: *whatever the agent generates is passed to the client*.
//! Two pumps over one connection:
//! * **client → agent:** binary frames become realtime audio; the `end` control becomes
//!   `audio_stream_end`; the `playout` control feeds the [`PlayoutClock`].
//! * **agent → client:** each raw vendor message is scanned for output audio (→ the client socket)
//!   and for `Interrupted` → a `flush` control so a barge-in actually cuts the client's buffered
//!   playout (revoking the server token alone can't).
//!
//! Ownership: rather than aliasing one connection for both directions, the vendor side is two
//! channels — `VendorSend` out, raw `Value` in — so the bridge is pure, testable pump logic. A thin
//! actor over [`crate::AgentConnection`] wires those channels to the real socket (the connection
//! task owns the transport and selects over outbound commands + inbound frames).

use std::sync::Arc;

use serde_json::Value;
use tokio::sync::mpsc;

use snail_core::vendor::{ParsedEvent, RealtimeControl, VendorAdapter};

use crate::transport::protocol::{encode_control, Control, ControlType};

/// One frame from the client socket.
#[derive(Debug, Clone, PartialEq)]
pub enum ClientMsg {
    /// binary → media (raw PCM16LE mono).
    Bytes(Vec<u8>),
    /// text → control (JSON).
    Text(String),
    Disconnect,
}

/// The client socket seam — a fake stands in for tests; an axum/tungstenite WS impl runs in prod.
pub trait ClientSocket {
    fn accept(&mut self) -> impl std::future::Future<Output = ()> + Send;
    fn receive(&mut self) -> impl std::future::Future<Output = ClientMsg> + Send;
    fn send_bytes(&mut self, data: Vec<u8>) -> impl std::future::Future<Output = ()> + Send;
    fn send_text(&mut self, text: String) -> impl std::future::Future<Output = ()> + Send;
    fn close(&mut self) -> impl std::future::Future<Output = ()> + Send;
}

/// What the bridge asks the vendor connection to send.
#[derive(Debug, Clone, PartialEq)]
pub enum VendorSend {
    /// realtime audio (PCM16LE mono) at `rate` Hz.
    Audio { data: Vec<u8>, rate: u32 },
    /// an out-of-band realtime control marker.
    Control(RealtimeControl),
}

/// Tracks agent audio sent vs client-reported playout → buffered-ahead. Counts are PCM16 samples.
#[derive(Debug, Default, Clone, Copy)]
pub struct PlayoutClock {
    pub sent: u64,
    pub played: u64,
}

impl PlayoutClock {
    pub fn note_sent(&mut self, n_samples: u64) {
        self.sent += n_samples;
    }
    pub fn note_played(&mut self, position: u64) {
        self.played = position;
    }
    pub fn buffered_ahead(&self) -> u64 {
        self.sent.saturating_sub(self.played)
    }
    /// Barge-in cut: buffered playout is discarded on the client.
    pub fn on_flush(&mut self) {
        self.sent = self.played;
    }
}

/// Bidirectional pump between a client socket and one agent connection (passthrough — no pipeline).
pub struct ClientBridge<S: ClientSocket> {
    socket: S,
    adapter: Arc<dyn VendorAdapter>,
    to_vendor: mpsc::UnboundedSender<VendorSend>,
    from_vendor: mpsc::UnboundedReceiver<Value>,
    input_sample_rate: u32,
    clock: PlayoutClock,
    client_gone: bool,
}

impl<S: ClientSocket> ClientBridge<S> {
    pub fn new(
        socket: S,
        adapter: Arc<dyn VendorAdapter>,
        to_vendor: mpsc::UnboundedSender<VendorSend>,
        from_vendor: mpsc::UnboundedReceiver<Value>,
        input_sample_rate: u32,
    ) -> Self {
        Self {
            socket,
            adapter,
            to_vendor,
            from_vendor,
            input_sample_rate,
            clock: PlayoutClock::default(),
            client_gone: false,
        }
    }

    pub fn playout(&self) -> PlayoutClock {
        self.clock
    }

    /// Accept the socket and pump until either side ends.
    pub async fn run(&mut self) {
        self.socket.accept().await;
        self.send_control(Control::of(ControlType::Ready)).await;
        loop {
            tokio::select! {
                msg = self.socket.receive() => {
                    match msg {
                        ClientMsg::Disconnect => {
                            self.client_gone = true;
                            break;
                        }
                        ClientMsg::Bytes(data) => self.ingest_audio(data),
                        ClientMsg::Text(text) => self.handle_control(&text).await,
                    }
                }
                raw = self.from_vendor.recv() => {
                    match raw {
                        Some(raw) => self.on_vendor_msg(&raw).await,
                        None => break, // vendor connection closed
                    }
                }
            }
        }
        self.teardown().await;
    }

    // --- client → agent ---------------------------------------------------

    fn ingest_audio(&mut self, data: Vec<u8>) {
        let _ = self.to_vendor.send(VendorSend::Audio {
            data,
            rate: self.input_sample_rate,
        });
    }

    async fn handle_control(&mut self, text: &str) {
        let ctrl = match crate::transport::protocol::decode_control(text) {
            Ok(c) => c,
            Err(_) => return,
        };
        match ctrl.kind {
            ControlType::Playout => {
                if let Some(samples) = ctrl.samples {
                    self.clock.note_played(samples.max(0) as u64);
                }
            }
            ControlType::End => {
                let _ = self
                    .to_vendor
                    .send(VendorSend::Control(RealtimeControl::AudioStreamEnd));
            }
            _ => {}
        }
    }

    // --- agent → client ---------------------------------------------------

    async fn on_vendor_msg(&mut self, raw: &Value) {
        if let Some(pcm) = self.adapter.extract_output_audio(raw) {
            self.clock.note_sent((pcm.len() / 2) as u64); // PCM16 mono
            self.socket.send_bytes(pcm).await;
        }
        for ev in self.adapter.parse_event(raw) {
            if ev == ParsedEvent::Interrupted {
                self.flush_client().await;
            }
        }
    }

    async fn flush_client(&mut self) {
        self.clock.on_flush();
        self.send_control(Control::of(ControlType::Flush)).await;
    }

    // --- helpers ----------------------------------------------------------

    async fn send_control(&mut self, control: Control) {
        self.socket.send_text(encode_control(&control)).await;
    }

    async fn teardown(&mut self) {
        if self.client_gone {
            return;
        }
        self.send_control(Control::of(ControlType::Bye)).await;
        self.socket.close().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use snail_core::vendor::{Backend, GeminiAdapter};
    use tokio::sync::mpsc;

    /// Fake client socket: scripted inbound frames; records everything sent back.
    struct FakeSocket {
        inbound: mpsc::UnboundedReceiver<ClientMsg>,
        pub sent_bytes: mpsc::UnboundedSender<Vec<u8>>,
        pub sent_text: mpsc::UnboundedSender<String>,
        pub accepted: bool,
    }
    impl ClientSocket for FakeSocket {
        async fn accept(&mut self) {
            self.accepted = true;
        }
        async fn receive(&mut self) -> ClientMsg {
            self.inbound.recv().await.unwrap_or(ClientMsg::Disconnect)
        }
        async fn send_bytes(&mut self, data: Vec<u8>) {
            let _ = self.sent_bytes.send(data);
        }
        async fn send_text(&mut self, text: String) {
            let _ = self.sent_text.send(text);
        }
        async fn close(&mut self) {}
    }

    fn gemini() -> Arc<GeminiAdapter> {
        Arc::new(GeminiAdapter::new(
            Backend::GeminiDev,
            "gemini-2.5-flash-live",
            false,
        ))
    }

    #[tokio::test]
    async fn pumps_both_directions_and_flushes_on_barge_in() {
        use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

        let (in_tx, in_rx) = mpsc::unbounded_channel();
        let (sb_tx, mut sb_rx) = mpsc::unbounded_channel();
        let (st_tx, mut st_rx) = mpsc::unbounded_channel();
        let socket = FakeSocket {
            inbound: in_rx,
            sent_bytes: sb_tx,
            sent_text: st_tx,
            accepted: false,
        };
        let (to_vendor, mut to_vendor_rx) = mpsc::unbounded_channel();
        let (from_vendor, from_vendor_rx) = mpsc::unbounded_channel();

        // Script: client sends mic audio, vendor sends audio then an interrupt, client disconnects.
        in_tx.send(ClientMsg::Bytes(vec![1, 2, 3, 4])).unwrap();
        let pcm = [7u8, 0, 8, 0]; // 2 samples
        from_vendor
            .send(json!({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": B64.encode(pcm)}}]}}}))
            .unwrap();
        from_vendor
            .send(json!({"serverContent": {"interrupted": true}}))
            .unwrap();
        in_tx.send(ClientMsg::Disconnect).unwrap();

        let mut bridge = ClientBridge::new(socket, gemini(), to_vendor, from_vendor_rx, 16000);
        bridge.run().await;

        // client → vendor: mic audio forwarded at the input rate
        assert_eq!(
            to_vendor_rx.recv().await.unwrap(),
            VendorSend::Audio {
                data: vec![1, 2, 3, 4],
                rate: 16000
            }
        );
        // vendor → client: the decoded PCM reached the socket
        assert_eq!(sb_rx.recv().await.unwrap(), pcm.to_vec());
        // READY then FLUSH (barge-in) on the control channel
        let texts: Vec<String> = std::iter::from_fn(|| st_rx.try_recv().ok()).collect();
        assert!(texts.iter().any(|t| t.contains("ready")));
        assert!(texts.iter().any(|t| t.contains("flush")));
    }

    #[tokio::test]
    async fn end_control_forwards_audio_stream_end() {
        let (in_tx, in_rx) = mpsc::unbounded_channel();
        let (sb_tx, _sb_rx) = mpsc::unbounded_channel();
        let (st_tx, _st_rx) = mpsc::unbounded_channel();
        let socket = FakeSocket {
            inbound: in_rx,
            sent_bytes: sb_tx,
            sent_text: st_tx,
            accepted: false,
        };
        let (to_vendor, mut to_vendor_rx) = mpsc::unbounded_channel();
        let (_from_vendor, from_vendor_rx) = mpsc::unbounded_channel::<Value>();

        in_tx
            .send(ClientMsg::Text(r#"{"type":"end"}"#.into()))
            .unwrap();
        in_tx.send(ClientMsg::Disconnect).unwrap();

        let mut bridge = ClientBridge::new(socket, gemini(), to_vendor, from_vendor_rx, 16000);
        bridge.run().await;
        assert_eq!(
            to_vendor_rx.recv().await.unwrap(),
            VendorSend::Control(RealtimeControl::AudioStreamEnd)
        );
    }
}
