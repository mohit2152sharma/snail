//! Session — the loop-bound orchestrator (port of `snail.session.session`), on tokio.
//!
//! Ties the vendor-neutral pieces together on **one task per session** (docs 06). It consumes
//! [`ParsedEvent`]s from an adapter, drives the event log, runs tools as concurrent tokio tasks
//! (with timeout + cooperative cancel on barge-in), resolves the [`ToolCallRegistry`], and sends
//! vendor-bound messages through an injected channel.
//!
//! **Single-owner, lock-free.** The session task owns all mutable state (log, registry). Tool
//! tasks never touch it — they post a [`ToolOutcome`] back over an mpsc channel and the session
//! applies it. This preserves the "one loop per session, no locks" model faithfully in Rust:
//! barge-in aborts the tool `JoinHandle`s and sweeps the registry group, so a late outcome that
//! still arrives finds a terminal entry and is dropped ("first terminal wins").

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use snail_core::context::{EventLog, EventType};
use snail_core::registry::{RegisterOpts, ToolCallRegistry};
use snail_core::tools::{execute, ToolRegistry, ToolResult, ToolStatus};
use snail_core::vendor::{ParsedEvent, VendorAdapter};

/// A completed tool run, posted back to the session task.
struct ToolOutcome {
    call_id: String,
    name: String,
    result: ToolResult,
    /// raw handler error — log-only (never reaches the model).
    raw: Option<String>,
    active: Option<String>,
}

/// Orchestrates one user-session's runtime on a tokio task.
pub struct Session {
    adapter: Arc<dyn VendorAdapter>,
    tools: Arc<ToolRegistry>,
    log: EventLog,
    registry: ToolCallRegistry,
    active_id: Option<String>,
    current_group: String,
    group_counter: u64,
    tool_tasks: HashMap<String, JoinHandle<()>>,
    outcome_tx: mpsc::UnboundedSender<ToolOutcome>,
    outcome_rx: mpsc::UnboundedReceiver<ToolOutcome>,
    /// vendor-bound messages (serialized tool results, etc.) leave here.
    send_tx: mpsc::UnboundedSender<Value>,
}

impl Session {
    pub fn new(
        adapter: Arc<dyn VendorAdapter>,
        tools: Arc<ToolRegistry>,
        active_id: Option<String>,
        send_tx: mpsc::UnboundedSender<Value>,
    ) -> Self {
        let (outcome_tx, outcome_rx) = mpsc::unbounded_channel();
        Self {
            adapter,
            tools,
            log: EventLog::new(),
            registry: ToolCallRegistry::with_defaults(),
            active_id,
            current_group: "r0".to_string(),
            group_counter: 0,
            tool_tasks: HashMap::new(),
            outcome_tx,
            outcome_rx,
            send_tx,
        }
    }

    pub fn log(&self) -> &EventLog {
        &self.log
    }
    pub fn registry(&self) -> &ToolCallRegistry {
        &self.registry
    }
    pub fn current_group(&self) -> &str {
        &self.current_group
    }
    pub fn in_flight(&self) -> usize {
        self.tool_tasks.len()
    }

    // --- inbound ----------------------------------------------------------

    /// React to one neutral vendor event (may spawn tool tasks).
    pub async fn handle_event(&mut self, ev: ParsedEvent) {
        match ev {
            ParsedEvent::UserTranscript { text, is_final } => {
                if is_final {
                    self.log
                        .append(EventType::UserSpeech, None, text, None, None);
                }
            }
            ParsedEvent::AgentTranscript { text, is_final } => {
                if is_final {
                    self.log.append(
                        EventType::AgentSpeech,
                        self.active_id.clone(),
                        text,
                        None,
                        None,
                    );
                }
            }
            ParsedEvent::ToolCallRequest {
                call_id,
                name,
                args,
            } => {
                self.start_tool(call_id, name, args);
            }
            ParsedEvent::TurnComplete => self.new_group(),
            ParsedEvent::Interrupted => self.barge_in(),
            ParsedEvent::VendorError { code, message } => {
                self.log.append(
                    EventType::ExternalContext,
                    None,
                    "",
                    Some(serde_json::json!({"vendor_error": code, "message": message})),
                    None,
                );
            }
            ParsedEvent::GoAway { .. } | ParsedEvent::ResumptionUpdate { .. } => {}
        }
    }

    // --- tool execution ---------------------------------------------------

    fn start_tool(&mut self, call_id: String, name: String, args: Value) {
        let active = self.active_id.clone();
        let reg = self.registry.register(
            &call_id,
            &name,
            args.clone(),
            RegisterOpts {
                origin_connection_id: active.clone(),
                response_group_id: Some(self.current_group.clone()),
                ..Default::default()
            },
        );
        if reg.is_err() {
            return; // duplicate call_id or in-flight cap → drop (backpressure)
        }
        self.log.append(
            EventType::ToolCall,
            active.clone(),
            "",
            Some(serde_json::json!({"tool_name": name, "tool_call_id": call_id, "args": args})),
            None,
        );

        let tools = self.tools.clone();
        let tx = self.outcome_tx.clone();
        let timeout_s = self.tools.get(&name).and_then(|t| t.timeout_s);
        let cid = call_id.clone();
        let handle = tokio::spawn(async move {
            let (result, raw) = run_tool(tools, &name, args, timeout_s).await;
            let _ = tx.send(ToolOutcome {
                call_id: cid,
                name,
                result,
                raw,
                active,
            });
        });
        self.tool_tasks.insert(call_id, handle);
    }

    /// Drain every completed tool outcome, resolving the registry + emitting results. Call after
    /// awaiting tasks (or on each loop tick).
    pub fn pump_outcomes(&mut self) {
        while let Ok(outcome) = self.outcome_rx.try_recv() {
            self.apply_outcome(outcome);
        }
    }

    fn apply_outcome(&mut self, o: ToolOutcome) {
        self.tool_tasks.remove(&o.call_id);
        // First terminal wins; if swept by barge-in, resolve() no-ops and we drop.
        if !self.registry.resolve(&o.call_id, o.result.clone()) {
            return;
        }
        let content = result_content(&o.result);
        let mut meta = serde_json::json!({
            "tool_name": o.name,
            "tool_call_id": o.call_id,
            "status": o.result.status.as_str(),
        });
        // Raw handler error is log-only — captured here, never in `content` (sanitized boundary).
        if let Some(raw) = &o.raw {
            meta["_raw_error"] = serde_json::json!(raw);
        }
        self.log.append(
            EventType::ToolResult,
            o.active.clone(),
            content.clone(),
            Some(meta),
            None,
        );
        let meta = serde_json::json!({"status": o.result.status.as_str()});
        let msg = self
            .adapter
            .serialize_tool_result(&o.call_id, &o.name, &content, Some(&meta));
        let _ = self.send_tx.send(msg);
    }

    /// Await all in-flight tool tasks, then apply their outcomes (tests / graceful close).
    pub async fn drain_tools(&mut self) {
        let handles: Vec<JoinHandle<()>> = self.tool_tasks.drain().map(|(_, h)| h).collect();
        for h in handles {
            let _ = h.await;
        }
        self.pump_outcomes();
    }

    // --- barge-in / boundaries -------------------------------------------

    /// User interrupted: abort this turn's tool tasks + sweep the registry group (cancelled).
    pub fn barge_in(&mut self) {
        let gid = self.current_group.clone();
        for call_id in self.registry.group_call_ids(&gid) {
            if let Some(handle) = self.tool_tasks.remove(&call_id) {
                handle.abort();
            }
        }
        self.registry.sweep_response_group(&gid);
    }

    fn new_group(&mut self) {
        self.group_counter += 1;
        self.current_group = format!("r{}", self.group_counter);
    }
}

/// Run one tool to a `(result, raw_error_for_log)` pair, honouring an optional timeout. The sync
/// handler runs on the blocking pool so a slow tool doesn't stall the runtime.
async fn run_tool(
    tools: Arc<ToolRegistry>,
    name: &str,
    args: Value,
    timeout_s: Option<f64>,
) -> (ToolResult, Option<String>) {
    if tools.get(name).is_none() {
        return (ToolResult::not_found(Some(name)), None);
    }
    let name_owned = name.to_string();
    let compute = tokio::task::spawn_blocking(move || match tools.get(&name_owned) {
        Some(tool) => execute(tool, &args),
        None => (ToolResult::not_found(Some(&name_owned)), None),
    });
    match timeout_s {
        Some(t) => match tokio::time::timeout(Duration::from_secs_f64(t), compute).await {
            Ok(Ok(pair)) => pair,
            Ok(Err(_join)) => (
                ToolResult::error(None, false),
                Some("tool task panicked".into()),
            ),
            Err(_elapsed) => (ToolResult::timeout(), None),
        },
        None => compute.await.unwrap_or_else(|_| {
            (
                ToolResult::error(None, false),
                Some("tool task panicked".into()),
            )
        }),
    }
}

fn result_content(result: &ToolResult) -> String {
    if result.status == ToolStatus::Success {
        match &result.data {
            None => String::new(),
            Some(d) => d.to_string(),
        }
    } else {
        result
            .reason
            .clone()
            .unwrap_or_else(|| result.status.as_str().to_string())
    }
}
