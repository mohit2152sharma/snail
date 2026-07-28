//! snail-rt — the Snail async runtime (tokio) over `snail-core`'s vendor-neutral surface.
//!
//! Phase P3 of the Rust port: the loop-bound orchestration that the pure core deferred. Today it
//! hosts the [`session::Session`] orchestrator; the connection pool, transport server, and the
//! Gemini Live WebSocket adapter land here next (the WS adapter needs live credentials to verify).

pub mod connection;
pub mod gemini;
pub mod pool;
pub mod session;
pub mod transport;

pub use connection::{AgentConnection, AgentSpec, ConnectionMeta, ConnectionState, LiveTransport};
pub use gemini::{GeminiConnector, GeminiLiveTransport};
pub use pool::{ConnectionPool, Connector};
pub use session::Session;
pub use transport::{Control, ControlType};

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::json;
    use tokio::sync::mpsc;

    use snail_core::context::EventType;
    use snail_core::tools::{Tool, ToolRegistry};
    use snail_core::vendor::{MockVendorAdapter, ParsedEvent};

    use super::Session;

    fn echo_registry() -> Arc<ToolRegistry> {
        let mut reg = ToolRegistry::new();
        reg.register(Tool::new(
            "echo",
            json!({"type": "object"}),
            Box::new(|args| Ok(json!({"echo": args.get("msg").cloned().unwrap_or(json!(""))}))),
        ));
        Arc::new(reg)
    }

    fn session_with(
        adapter: Arc<MockVendorAdapter>,
        tools: Arc<ToolRegistry>,
    ) -> (Session, mpsc::UnboundedReceiver<serde_json::Value>) {
        let (send_tx, send_rx) = mpsc::unbounded_channel();
        let sess = Session::new(adapter, tools, Some("agent-1".into()), send_tx);
        (sess, send_rx)
    }

    #[tokio::test]
    async fn tool_call_resolves_logs_and_sends_result() {
        let adapter = Arc::new(MockVendorAdapter::default());
        let (mut sess, mut send_rx) = session_with(adapter.clone(), echo_registry());

        sess.handle_event(ParsedEvent::ToolCallRequest {
            call_id: "c1".into(),
            name: "echo".into(),
            args: json!({"msg": "hi"}),
        })
        .await;
        assert_eq!(sess.in_flight(), 1);

        sess.drain_tools().await;

        // registry resolved (single-resolution → removed)
        assert_eq!(sess.registry().in_flight(), 0);
        assert!(!sess.registry().contains("c1"));
        // TOOL_CALL + TOOL_RESULT both logged
        let kinds: Vec<_> = sess.log().events().iter().map(|e| e.kind).collect();
        assert!(kinds.contains(&EventType::ToolCall));
        assert!(kinds.contains(&EventType::ToolResult));
        // vendor-bound tool result was sent + recorded by the adapter
        let sent = send_rx.try_recv().expect("a tool result should be sent");
        assert_eq!(sent["type"], json!("tool_result"));
        assert_eq!(sent["call_id"], json!("c1"));
        assert_eq!(adapter.sent_tool_results.borrow().len(), 1);
    }

    #[tokio::test]
    async fn unknown_tool_resolves_not_found() {
        let adapter = Arc::new(MockVendorAdapter::default());
        let (mut sess, mut send_rx) = session_with(adapter, echo_registry());
        sess.handle_event(ParsedEvent::ToolCallRequest {
            call_id: "c1".into(),
            name: "nope".into(),
            args: json!({}),
        })
        .await;
        sess.drain_tools().await;
        let sent = send_rx.try_recv().unwrap();
        // content carries the not_found reason
        assert!(sent["content"].as_str().unwrap().contains("does not exist"));
    }

    #[tokio::test]
    async fn barge_in_sweeps_group_and_drops_result() {
        let adapter = Arc::new(MockVendorAdapter::default());
        let (mut sess, mut send_rx) = session_with(adapter, echo_registry());
        sess.handle_event(ParsedEvent::ToolCallRequest {
            call_id: "c1".into(),
            name: "echo".into(),
            args: json!({"msg": "hi"}),
        })
        .await;
        // interrupt before the outcome is applied → sweep the group
        sess.barge_in();
        sess.drain_tools().await;

        assert_eq!(sess.registry().in_flight(), 0); // swept
                                                    // no TOOL_RESULT logged (the swept call's late outcome no-ops on resolve)
        let has_result = sess
            .log()
            .events()
            .iter()
            .any(|e| e.kind == EventType::ToolResult);
        assert!(!has_result);
        assert!(send_rx.try_recv().is_err()); // nothing sent to the vendor
    }

    #[tokio::test]
    async fn turn_complete_advances_group_and_transcripts_log() {
        let adapter = Arc::new(MockVendorAdapter::default());
        let (mut sess, _rx) = session_with(adapter, echo_registry());
        assert_eq!(sess.current_group(), "r0");
        sess.handle_event(ParsedEvent::UserTranscript {
            text: "hello".into(),
            is_final: true,
        })
        .await;
        sess.handle_event(ParsedEvent::TurnComplete).await;
        assert_eq!(sess.current_group(), "r1");
        sess.handle_event(ParsedEvent::AgentTranscript {
            text: "hi there".into(),
            is_final: true,
        })
        .await;
        let contents: Vec<_> = sess
            .log()
            .events()
            .iter()
            .map(|e| e.content.clone())
            .collect();
        assert!(contents.contains(&"hello".to_string()));
        assert!(contents.contains(&"hi there".to_string()));
    }
}
