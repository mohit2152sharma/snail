//! Neutral connection params: SetupParam vs JoinContext (port of `snail.vendor.params`).
//!
//! `SetupParam` (bound at connect, both vendors): model, voice, system_instruction, tools,
//! response_modality — the agent's STATIC identity. `JoinContext` (injected on join): history +
//! per-client facts — genuinely dynamic per-client data.

use serde::Serialize;
use serde_json::Value;

use crate::context::Item;

/// Which user-audio source an agent consumes. `Clean` is the default (RNNoise-denoised); `Raw`
/// skips cleaning (for a self-denoising model). If no agent wants Clean, RNNoise never runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InputSource {
    Clean,
    Raw,
}

/// Per-agent output modality. The active agent is `Audio`; a listener is `Text` (cheapest, needs a
/// flip to promote) or `Audio` (promotes with no flip).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseModality {
    Audio,
    Text,
}

/// Vendor-neutral tool declaration bound at setup. `parameters` is a common-denominator JSON-schema
/// value; the adapter serializes it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: Option<Value>,
    /// async behavior hint; maps to Gemini `Behavior.NON_BLOCKING` where supported.
    pub non_blocking: bool,
}

/// The agent's static identity — bound at connect (the pool key, docs 02).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SetupParam {
    pub model: String,
    pub voice: Option<String>,
    pub system_instruction: String,
    pub tools: Vec<ToolSpec>,
    pub response_modality: ResponseModality,
    pub input_source: InputSource,
}

impl SetupParam {
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            voice: None,
            system_instruction: String::new(),
            tools: Vec::new(),
            response_modality: ResponseModality::Audio,
            input_source: InputSource::Clean,
        }
    }
}

/// Per-client dynamic data injected on join. `history` is projected `Vec<Item>` injected as
/// user/model turns before the first model turn; `facts` are extra neutral items.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct JoinContext {
    pub history: Vec<Item>,
    pub facts: Vec<Item>,
}
