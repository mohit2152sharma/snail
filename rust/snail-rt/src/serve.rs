//! Client-facing WebSocket server entry point (port of `snail.transport.server`), behind the
//! `server` feature. This is the outermost deployment shell: it adapts an `axum` WebSocket to the
//! [`crate::bridge::ClientSocket`] seam and wires the full stack on each client connection:
//!
//! ```text
//! client WS ──▶ ClientBridge ──(VendorSend / raw Value channels)──▶ run_gemini_agent ──▶ Gemini WS
//! ```
//!
//! Everything below the socket bindings is already ported + unit-tested (bridge, connector,
//! adapter). End-to-end verification needs a `GEMINI_API_KEY` and a live client.

use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use tokio::sync::mpsc;

use snail_core::vendor::{Backend, GeminiAdapter};

use crate::bridge::{ClientBridge, ClientMsg, ClientSocket};
use crate::connection::AgentSpec;
use crate::gemini::{run_gemini_agent, GeminiConnector};

/// Server config: the Gemini key + the agent identity every client session runs.
#[derive(Clone)]
pub struct ServeConfig {
    pub api_key: String,
    pub spec: AgentSpec,
    pub input_sample_rate: u32,
}

/// Adapts an `axum` WebSocket to the [`ClientSocket`] seam.
pub struct AxumClientSocket {
    ws: WebSocket,
}

impl ClientSocket for AxumClientSocket {
    async fn accept(&mut self) {} // axum already upgraded the connection

    async fn receive(&mut self) -> ClientMsg {
        match self.ws.recv().await {
            Some(Ok(Message::Binary(b))) => ClientMsg::Bytes(b.to_vec()),
            Some(Ok(Message::Text(t))) => ClientMsg::Text(t.to_string()),
            Some(Ok(_)) => ClientMsg::Text(String::new()), // ping/pong → ignored control
            _ => ClientMsg::Disconnect,
        }
    }

    async fn send_bytes(&mut self, data: Vec<u8>) {
        let _ = self.ws.send(Message::Binary(data.into())).await;
    }

    async fn send_text(&mut self, text: String) {
        let _ = self.ws.send(Message::Text(text.into())).await;
    }

    async fn close(&mut self) {
        let _ = self.ws.send(Message::Close(None)).await;
    }
}

/// Build the router: `GET /ws` upgrades to the session handler.
pub fn app(config: ServeConfig) -> Router {
    Router::new()
        .route("/ws", get(ws_handler))
        .with_state(Arc::new(config))
}

async fn ws_handler(ws: WebSocketUpgrade, State(config): State<Arc<ServeConfig>>) -> Response {
    ws.on_upgrade(move |socket| handle_session(socket, config))
}

/// Wire one client session: open the Gemini socket, spawn the duplex actor, run the bridge.
async fn handle_session(socket: WebSocket, config: Arc<ServeConfig>) {
    let adapter = Arc::new(GeminiAdapter::new(
        Backend::GeminiDev,
        config.spec.setup.model.clone(),
        false,
    ));
    let connector = GeminiConnector::new(config.api_key.clone(), adapter.clone());
    let transport = match connector.open_result(&config.spec, None).await {
        Ok(t) => t,
        Err(e) => {
            eprintln!("[snail-rt] gemini connect failed: {e}");
            return; // drop the client session
        }
    };
    eprintln!(
        "[snail-rt] client session up — gemini connected (model={})",
        config.spec.setup.model
    );

    let (to_vendor, vendor_rx) = mpsc::unbounded_channel();
    let (raw_tx, raw_rx) = mpsc::unbounded_channel();

    // vendor side: the duplex actor owns the split Gemini socket.
    let actor = tokio::spawn(run_gemini_agent(
        transport,
        adapter.clone(),
        vendor_rx,
        raw_tx,
    ));

    // client side: the bridge pumps between the browser WS and the vendor channels.
    let mut bridge = ClientBridge::new(
        AxumClientSocket { ws: socket },
        adapter,
        to_vendor,
        raw_rx,
        config.input_sample_rate,
    );
    bridge.run().await;
    actor.abort();
}
