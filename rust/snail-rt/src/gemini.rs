//! GeminiConnector + GeminiLiveTransport — the real Gemini Live WebSocket wire (port of the
//! `google-genai`-backed `snail.connections.connector` GeminiConnector).
//!
//! This is the one **credential-gated** piece: the pure translation lives in
//! [`snail_core::vendor::GeminiAdapter`] (unit-tested against sample wire JSON, no key); here we
//! carry those payloads over a live `tokio-tungstenite` WebSocket to
//! `BidiGenerateContent`. It compiles and speaks the documented v1beta wire, but end-to-end
//! verification needs a real `GEMINI_API_KEY` against the live service.
//!
//! Wire framing: the [`LiveTransport`] payloads from the adapter are wrapped in their top-level key
//! (`realtimeInput` / `clientContent` / `toolResponse`) and sent as JSON text frames; inbound text
//! or binary frames are parsed as JSON `serverContent` / `toolCall` / `goAway` / resumption.

use std::sync::Arc;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

use snail_core::vendor::{GeminiAdapter, MediaChunk};

use crate::bridge::VendorSend;
use crate::connection::{AgentSpec, LiveTransport};
use crate::pool::Connector;

const HOST: &str = "generativelanguage.googleapis.com";
const PATH: &str = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// A live Gemini Live socket adapting the raw WebSocket to [`LiveTransport`].
pub struct GeminiLiveTransport {
    ws: Ws,
}

impl GeminiLiveTransport {
    async fn send_json(&mut self, msg: Value) {
        if let Ok(text) = serde_json::to_string(&msg) {
            let _ = self.ws.send(Message::Text(text)).await;
        }
    }
}

impl LiveTransport for GeminiLiveTransport {
    async fn send_realtime_input(&mut self, msg: Value) {
        self.send_json(json!({ "realtimeInput": msg })).await;
    }

    async fn send_client_content(&mut self, msg: Value) {
        self.send_json(json!({ "clientContent": msg })).await;
    }

    async fn send_tool_response(&mut self, resp: Value) {
        // adapter emits one functionResponse; the wire wants a list.
        self.send_json(json!({ "toolResponse": { "functionResponses": [resp] } }))
            .await;
    }

    async fn recv(&mut self) -> Option<Value> {
        while let Some(frame) = self.ws.next().await {
            match frame {
                Ok(Message::Text(t)) => {
                    if let Ok(v) = serde_json::from_str::<Value>(&t) {
                        return Some(v);
                    }
                }
                Ok(Message::Binary(b)) => {
                    if let Ok(v) = serde_json::from_slice::<Value>(&b) {
                        return Some(v);
                    }
                }
                Ok(Message::Close(_)) | Err(_) => return None,
                Ok(_) => continue, // ping/pong/frame — keep reading
            }
        }
        None
    }

    async fn close(&mut self) {
        let _ = self.ws.close(None).await;
    }
}

/// Opens Gemini Live sockets for one client + adapter (docs 02/07). Dev-API auth is the `?key=`
/// query param; Vertex/ADC bearer auth is a follow-up (the adapter/capabilities already model both
/// backends).
pub struct GeminiConnector {
    api_key: String,
    adapter: Arc<GeminiAdapter>,
}

impl GeminiConnector {
    pub fn new(api_key: impl Into<String>, adapter: Arc<GeminiAdapter>) -> Self {
        Self {
            api_key: api_key.into(),
            adapter,
        }
    }

    fn url(&self) -> String {
        format!("wss://{HOST}{PATH}?key={}", self.api_key)
    }

    /// Connect, send the setup message, and return the live transport. Errors surface as a
    /// `String` (connect failure / handshake). The first server `setupComplete` is left in the
    /// stream for the session to observe.
    pub async fn open_result(
        &self,
        spec: &AgentSpec,
        resumption_handle: Option<&str>,
    ) -> Result<GeminiLiveTransport, String> {
        let (mut ws, _resp) = connect_async(self.url())
            .await
            .map_err(|e| format!("gemini connect failed: {e}"))?;
        let setup = self
            .adapter
            .build_setup_with_resumption(&spec.setup, resumption_handle);
        let text = serde_json::to_string(&setup).map_err(|e| e.to_string())?;
        ws.send(Message::Text(text))
            .await
            .map_err(|e| format!("gemini setup send failed: {e}"))?;
        Ok(GeminiLiveTransport { ws })
    }
}

/// The duplex actor that ties [`crate::bridge::ClientBridge`]'s vendor channels to a live Gemini
/// socket. Splitting the `WebSocketStream` into sink + stream sidesteps the `select!` aliasing that
/// blocks send + recv on one `&mut` — the idiomatic-Rust answer to Python's shared-object asyncio.
/// Outbound [`VendorSend`] commands are serialized via the adapter + framed; inbound frames are
/// parsed as JSON and forwarded to the session over `raw_tx`.
pub async fn run_gemini_agent(
    transport: GeminiLiveTransport,
    adapter: std::sync::Arc<GeminiAdapter>,
    mut vendor_rx: mpsc::UnboundedReceiver<VendorSend>,
    raw_tx: mpsc::UnboundedSender<Value>,
) {
    use snail_core::vendor::VendorAdapter;
    let (mut sink, mut stream) = transport.ws.split();
    loop {
        tokio::select! {
            cmd = vendor_rx.recv() => {
                let Some(cmd) = cmd else { break };
                let inner = match cmd {
                    VendorSend::Audio { data, rate } => {
                        adapter.serialize_realtime(&MediaChunk::audio(data, rate))
                    }
                    VendorSend::Control(c) => adapter.serialize_realtime_control(c),
                };
                let wire = json!({ "realtimeInput": inner });
                if sink.send(Message::Text(wire.to_string())).await.is_err() {
                    break;
                }
            }
            frame = stream.next() => {
                match frame {
                    Some(Ok(Message::Text(t))) => {
                        if let Ok(v) = serde_json::from_str::<Value>(&t) {
                            if raw_tx.send(v).is_err() { break; }
                        }
                    }
                    Some(Ok(Message::Binary(b))) => {
                        if let Ok(v) = serde_json::from_slice::<Value>(&b) {
                            if raw_tx.send(v).is_err() { break; }
                        }
                    }
                    Some(Ok(_)) => {} // ping/pong
                    _ => break, // close / error
                }
            }
        }
    }
}

impl Connector for GeminiConnector {
    type Transport = GeminiLiveTransport;

    async fn open(&self, spec: &AgentSpec, resumption_handle: Option<&str>) -> GeminiLiveTransport {
        // The pool's Connector seam is infallible; a real deployment threads open_result's Result
        // through the pool. A connect failure here is a hard, actionable startup error.
        self.open_result(spec, resumption_handle)
            .await
            .unwrap_or_else(|e| panic!("{e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use snail_core::vendor::{Backend, SetupParam};

    #[test]
    fn url_is_dev_bidi_endpoint_with_key() {
        let adapter = Arc::new(GeminiAdapter::new(
            Backend::GeminiDev,
            "gemini-2.5-flash-live",
            false,
        ));
        let c = GeminiConnector::new("SECRET", adapter);
        let url = c.url();
        assert!(url.starts_with("wss://generativelanguage.googleapis.com/ws/"));
        assert!(url.ends_with("BidiGenerateContent?key=SECRET"));
    }

    // NOTE: end-to-end connect/send/recv against the live service is credential-gated and cannot
    // run in CI. `open_result` compiles against the documented v1beta wire; verify manually with a
    // real GEMINI_API_KEY. The pure translation it carries is fully covered in
    // snail_core::vendor::gemini tests.
    #[allow(dead_code)]
    fn _spec() -> AgentSpec {
        AgentSpec {
            id: "a".into(),
            backend: Backend::GeminiDev,
            setup: SetupParam::new("gemini-2.5-flash-live"),
        }
    }
}
