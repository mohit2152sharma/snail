//! GeminiAdapter — Gemini Live translation (port of `snail.vendor.gemini`).
//!
//! Pure translation (neutral ↔ the Gemini Live *wire JSON*, `BidiGenerateContent`). Unlike the
//! Python port — which leaned on the `google-genai` typed objects — this speaks the wire shapes
//! directly (raw JSON [`Value`]s), because there is no Rust Gemini SDK. The live socket lives in
//! the connection layer (`snail-rt`); this is socket-free and fully unit-testable against sample
//! wire messages, no key required.
//!
//! Wire references (v1beta BidiGenerateContent):
//! - client setup: `{"setup": {...}}` (first message)
//! - realtime: `{"realtimeInput": {"audio"|"activityStart"|"activityEnd"|"audioStreamEnd": ...}}`
//! - turns: `{"clientContent": {"turns": [...], "turnComplete": bool}}`
//! - tool result: `{"toolResponse": {"functionResponses": [...]}}`
//! - server: `serverContent` / `toolCall` / `goAway` / `sessionResumptionUpdate`
//!
//! The connection-layer transport wraps each `serialize_*` payload in its top-level key
//! (`realtimeInput` / `clientContent` / `toolResponse`) — same split as the Python adapter→SDK.

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use serde_json::{json, Value};

use crate::context::{Item, Role};

use super::adapter::VendorAdapter;
use super::capabilities::{Backend, VendorCapabilities};
use super::events::ParsedEvent;
use super::media::{MediaChunk, MediaKind, RealtimeControl};
use super::params::{ResponseModality, SetupParam};

/// Parse a Gemini duration string (`"10s"`, `"250ms"`, bare seconds) to milliseconds.
fn parse_duration_ms(s: &str) -> Option<i64> {
    let s = s.trim();
    if let Some(body) = s.strip_suffix("ms") {
        return body.trim().parse::<f64>().ok().map(|v| v as i64);
    }
    if let Some(body) = s.strip_suffix('s') {
        return body.trim().parse::<f64>().ok().map(|v| (v * 1000.0) as i64);
    }
    s.parse::<f64>().ok().map(|v| (v * 1000.0) as i64)
}

/// Capability cells per Gemini backend (docs 07).
pub fn gemini_capabilities(
    backend: Backend,
    model: &str,
    self_denoise: bool,
) -> VendorCapabilities {
    assert!(
        matches!(backend, Backend::GeminiDev | Backend::GeminiVertex),
        "{backend:?} is not a Gemini backend"
    );
    VendorCapabilities {
        // NON_BLOCKING async tools are Dev-API only; Vertex emulates (#1739).
        native_async_tools: backend == Backend::GeminiDev,
        session_resumption: true,
        self_denoise,
        input_sample_rate: 16000,
        output_sample_rate: 24000,
        ..VendorCapabilities::new("gemini", model, backend)
    }
}

/// Translate the neutral surface to/from Gemini Live for one (model, backend).
pub struct GeminiAdapter {
    model: String,
    caps: VendorCapabilities,
}

impl GeminiAdapter {
    pub fn new(backend: Backend, model: impl Into<String>, self_denoise: bool) -> Self {
        let model = model.into();
        let caps = gemini_capabilities(backend, &model, self_denoise);
        Self { model, caps }
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// Serialize the static identity to a `setup` message, optionally resuming a prior session.
    pub fn build_setup_with_resumption(
        &self,
        setup: &SetupParam,
        resumption_handle: Option<&str>,
    ) -> Value {
        let modality = match setup.response_modality {
            ResponseModality::Audio => "AUDIO",
            ResponseModality::Text => "TEXT",
        };
        let mut gen_config = json!({ "responseModalities": [modality] });
        if let Some(voice) = &setup.voice {
            gen_config["speechConfig"] =
                json!({"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}});
        }
        let mut inner = json!({
            "model": format!("models/{}", self.model),
            "generationConfig": gen_config,
            // transcripts so the session can log user + agent turns.
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "sessionResumption": match resumption_handle {
                Some(h) => json!({"handle": h}),
                None => json!({}),
            },
        });
        if !setup.system_instruction.is_empty() {
            inner["systemInstruction"] = json!({"parts": [{"text": setup.system_instruction}]});
        }
        if !setup.tools.is_empty() {
            let decls: Vec<Value> = setup
                .tools
                .iter()
                .map(|t| self.function_declaration(t))
                .collect();
            inner["tools"] = json!([{ "functionDeclarations": decls }]);
        }
        json!({ "setup": inner })
    }

    fn function_declaration(&self, spec: &super::params::ToolSpec) -> Value {
        let mut decl = json!({ "name": spec.name });
        if !spec.description.is_empty() {
            decl["description"] = json!(spec.description);
        }
        if let Some(params) = &spec.parameters {
            // neutral lowercase JSON-schema passes through via parametersJsonSchema.
            decl["parametersJsonSchema"] = params.clone();
        }
        // NON_BLOCKING is Dev-API only; on Vertex leave BLOCKING and emulate (#1739).
        if spec.non_blocking && self.caps.native_async_tools {
            decl["behavior"] = json!("NON_BLOCKING");
        }
        decl
    }

    fn item_content(&self, item: &Item) -> Value {
        // A model function-call item.
        if item.role == Role::Model && item.name.is_some() && item.args.is_some() {
            return json!({"role": "model", "parts": [{"functionCall": {
                "id": item.tool_call_id, "name": item.name, "args": item.args
            }}]});
        }
        // A tool result item.
        if item.role == Role::Tool {
            return json!({"role": "tool", "parts": [{"functionResponse": {
                "id": item.tool_call_id, "name": item.name, "response": {"result": item.text}
            }}]});
        }
        // SYSTEM content turns are forbidden on Gemini → fold into a user turn.
        if item.role == Role::System {
            return json!({"role": "user", "parts": [{"text": format!("[system] {}", item.text)}]});
        }
        let role = if item.role == Role::Model {
            "model"
        } else {
            "user"
        };
        json!({"role": role, "parts": [{"text": item.text}]})
    }

    /// Extract agent audio (PCM bytes) from a server message's model turn, if any.
    pub fn extract_output_audio(&self, msg: &Value) -> Option<Vec<u8>> {
        let parts = msg
            .get("serverContent")?
            .get("modelTurn")?
            .get("parts")?
            .as_array()?;
        let mut out = Vec::new();
        for p in parts {
            if let Some(data) = p
                .get("inlineData")
                .and_then(|d| d.get("data"))
                .and_then(Value::as_str)
            {
                if let Ok(bytes) = B64.decode(data) {
                    out.extend_from_slice(&bytes);
                }
            }
        }
        if out.is_empty() {
            None
        } else {
            Some(out)
        }
    }
}

impl VendorAdapter for GeminiAdapter {
    fn name(&self) -> &str {
        "gemini"
    }
    fn capabilities(&self) -> &VendorCapabilities {
        &self.caps
    }

    fn build_setup(&self, setup: &SetupParam) -> Value {
        self.build_setup_with_resumption(setup, None)
    }

    fn serialize_item(&self, item: &Item) -> Value {
        self.item_content(item)
    }

    fn serialize_realtime(&self, chunk: &MediaChunk) -> Value {
        match chunk.kind {
            MediaKind::Audio => {
                let rate = chunk.sample_rate.unwrap_or(self.caps.input_sample_rate);
                let data = chunk.data.as_deref().unwrap_or(&[]);
                json!({"audio": {"data": B64.encode(data), "mimeType": format!("audio/pcm;rate={rate}")}})
            }
            MediaKind::Image => {
                let data = chunk.data.as_deref().unwrap_or(&[]);
                let mime = chunk.mime_type.as_deref().unwrap_or("image/jpeg");
                json!({"media": {"data": B64.encode(data), "mimeType": mime}})
            }
            MediaKind::Text => json!({"text": chunk.text}),
        }
    }

    fn serialize_realtime_control(&self, control: RealtimeControl) -> Value {
        match control {
            RealtimeControl::ActivityStart => json!({"activityStart": {}}),
            RealtimeControl::ActivityEnd => json!({"activityEnd": {}}),
            RealtimeControl::AudioStreamEnd => json!({"audioStreamEnd": true}),
        }
    }

    fn serialize_turns(&self, items: &[Item], complete: bool) -> Value {
        let turns: Vec<Value> = items.iter().map(|i| self.item_content(i)).collect();
        json!({"turns": turns, "turnComplete": complete})
    }

    fn serialize_tool_result(
        &self,
        call_id: &str,
        name: &str,
        content: &str,
        _meta: Option<&Value>,
    ) -> Value {
        json!({"id": call_id, "name": name, "response": {"result": content}})
    }

    fn parse_event(&self, msg: &Value) -> Vec<ParsedEvent> {
        let mut out = Vec::new();
        if let Some(sc) = msg.get("serverContent") {
            if let Some(it) = sc.get("inputTranscription") {
                if let Some(text) = it.get("text").and_then(Value::as_str) {
                    if !text.is_empty() {
                        out.push(ParsedEvent::UserTranscript {
                            text: text.to_string(),
                            is_final: it.get("finished").and_then(Value::as_bool).unwrap_or(false),
                        });
                    }
                }
            }
            if let Some(ot) = sc.get("outputTranscription") {
                if let Some(text) = ot.get("text").and_then(Value::as_str) {
                    if !text.is_empty() {
                        out.push(ParsedEvent::AgentTranscript {
                            text: text.to_string(),
                            is_final: ot.get("finished").and_then(Value::as_bool).unwrap_or(false),
                        });
                    }
                }
            }
            if sc
                .get("interrupted")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                out.push(ParsedEvent::Interrupted);
            }
            if sc
                .get("turnComplete")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                out.push(ParsedEvent::TurnComplete);
            }
        }
        if let Some(tc) = msg.get("toolCall") {
            if let Some(calls) = tc.get("functionCalls").and_then(Value::as_array) {
                for fc in calls {
                    out.push(ParsedEvent::ToolCallRequest {
                        call_id: fc
                            .get("id")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                        name: fc
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                        args: fc.get("args").cloned().unwrap_or_else(|| json!({})),
                    });
                }
            }
        }
        if let Some(ga) = msg.get("goAway") {
            let time_left = ga
                .get("timeLeft")
                .and_then(Value::as_str)
                .and_then(parse_duration_ms);
            out.push(ParsedEvent::GoAway {
                time_left_ms: time_left,
            });
        }
        if let Some(sru) = msg.get("sessionResumptionUpdate") {
            let resumable = sru
                .get("resumable")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if let Some(handle) = sru.get("newHandle").and_then(Value::as_str) {
                if resumable && !handle.is_empty() {
                    out.push(ParsedEvent::ResumptionUpdate {
                        handle: handle.to_string(),
                    });
                }
            }
        }
        out
    }

    fn extract_output_audio(&self, raw: &Value) -> Option<Vec<u8>> {
        GeminiAdapter::extract_output_audio(self, raw)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vendor::params::ToolSpec;

    fn adapter() -> GeminiAdapter {
        GeminiAdapter::new(Backend::GeminiDev, "gemini-2.5-flash-live", false)
    }

    #[test]
    fn build_setup_wire_shape() {
        let mut setup = SetupParam::new("gemini-2.5-flash-live");
        setup.system_instruction = "be nice".into();
        setup.voice = Some("Puck".into());
        setup.tools = vec![ToolSpec {
            name: "lookup".into(),
            description: "look it up".into(),
            parameters: Some(json!({"type": "object"})),
            non_blocking: true,
        }];
        let s = adapter().build_setup(&setup);
        let inner = &s["setup"];
        assert_eq!(inner["model"], json!("models/gemini-2.5-flash-live"));
        assert_eq!(
            inner["generationConfig"]["responseModalities"],
            json!(["AUDIO"])
        );
        assert_eq!(
            inner["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
                ["voiceName"],
            json!("Puck")
        );
        assert_eq!(
            inner["systemInstruction"]["parts"][0]["text"],
            json!("be nice")
        );
        let decl = &inner["tools"][0]["functionDeclarations"][0];
        assert_eq!(decl["name"], json!("lookup"));
        assert_eq!(decl["parametersJsonSchema"], json!({"type": "object"}));
        assert_eq!(decl["behavior"], json!("NON_BLOCKING")); // Dev API → NON_BLOCKING
    }

    #[test]
    fn vertex_leaves_tools_blocking() {
        let a = GeminiAdapter::new(Backend::GeminiVertex, "gemini-2.5-flash-live", false);
        let mut setup = SetupParam::new("m");
        setup.tools = vec![ToolSpec {
            name: "t".into(),
            description: String::new(),
            parameters: None,
            non_blocking: true,
        }];
        let s = a.build_setup(&setup);
        assert!(s["setup"]["tools"][0]["functionDeclarations"][0]
            .get("behavior")
            .is_none());
    }

    #[test]
    fn realtime_audio_is_base64_pcm() {
        let v = adapter().serialize_realtime(&MediaChunk::audio(vec![1, 2, 3, 4], 16000));
        assert_eq!(v["audio"]["mimeType"], json!("audio/pcm;rate=16000"));
        assert_eq!(v["audio"]["data"], json!(B64.encode([1, 2, 3, 4])));
    }

    #[test]
    fn realtime_controls() {
        let a = adapter();
        assert_eq!(
            a.serialize_realtime_control(RealtimeControl::ActivityEnd),
            json!({"activityEnd": {}})
        );
        assert_eq!(
            a.serialize_realtime_control(RealtimeControl::AudioStreamEnd),
            json!({"audioStreamEnd": true})
        );
    }

    #[test]
    fn parse_server_content_and_tool_call() {
        let msg = json!({
            "serverContent": {
                "inputTranscription": {"text": "hello", "finished": true},
                "turnComplete": true
            }
        });
        let evs = adapter().parse_event(&msg);
        assert!(evs.contains(&ParsedEvent::UserTranscript {
            text: "hello".into(),
            is_final: true
        }));
        assert!(evs.contains(&ParsedEvent::TurnComplete));

        let tc = json!({"toolCall": {"functionCalls": [{"id": "c1", "name": "lookup", "args": {"q": 1}}]}});
        assert_eq!(
            adapter().parse_event(&tc),
            vec![ParsedEvent::ToolCallRequest {
                call_id: "c1".into(),
                name: "lookup".into(),
                args: json!({"q": 1})
            }]
        );
    }

    #[test]
    fn parse_goaway_and_resumption() {
        let ga = json!({"goAway": {"timeLeft": "9.5s"}});
        assert_eq!(
            adapter().parse_event(&ga),
            vec![ParsedEvent::GoAway {
                time_left_ms: Some(9500)
            }]
        );
        let sru = json!({"sessionResumptionUpdate": {"resumable": true, "newHandle": "h9"}});
        assert_eq!(
            adapter().parse_event(&sru),
            vec![ParsedEvent::ResumptionUpdate {
                handle: "h9".into()
            }]
        );
    }

    #[test]
    fn extract_output_audio_decodes_inline_data() {
        let pcm = [10u8, 20, 30, 40];
        let msg = json!({"serverContent": {"modelTurn": {"parts": [
            {"inlineData": {"mimeType": "audio/pcm", "data": B64.encode(pcm)}}
        ]}}});
        assert_eq!(adapter().extract_output_audio(&msg), Some(pcm.to_vec()));
        assert_eq!(
            adapter().extract_output_audio(&json!({"serverContent": {}})),
            None
        );
    }
}
