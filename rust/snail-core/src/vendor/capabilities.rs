//! Vendor capability descriptor (port of `snail.vendor.capabilities`).
//!
//! Cross-cutting pattern: adapters declare capabilities; the framework branches on them.
//! Capability is keyed per (vendor, model, backend), not per vendor — so two Gemini backends can
//! differ (native async tools on the Developer API, emulated on Vertex). The neutral surface stays
//! the same; the adapter absorbs the difference.

/// A concrete (vendor, model) hosting backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Backend {
    GeminiDev,
    GeminiVertex,
    OpenAiRealtime,
    Mock,
}

impl Backend {
    pub fn as_str(self) -> &'static str {
        match self {
            Backend::GeminiDev => "gemini_dev",
            Backend::GeminiVertex => "gemini_vertex",
            Backend::OpenAiRealtime => "openai_realtime",
            Backend::Mock => "mock",
        }
    }
}

/// What one `(vendor, model, backend)` actually supports. The framework reads these flags instead
/// of hardcoding vendor names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VendorCapabilities {
    pub vendor: String,
    pub model: String,
    pub backend: Backend,
    /// native async / non-blocking tool calls (Gemini Dev API `NON_BLOCKING`).
    pub native_async_tools: bool,
    /// native session resumption handle (Gemini). Else recycle = log-replay.
    pub session_resumption: bool,
    /// accepts `role="system"` *content* turns. Gemini does NOT.
    pub system_content_turn: bool,
    /// config/instruction/tool update allowed mid-session. Gemini does NOT.
    pub mid_session_config_update: bool,
    /// can truncate an already-emitted item to "what the user actually heard".
    pub item_truncate: bool,
    /// the model does its own noise suppression → the framework can feed it RAW audio.
    pub self_denoise: bool,
    /// vendor wire sample rates (Hz); interior is always 48k. Resample is lazy.
    pub input_sample_rate: u32,
    pub output_sample_rate: u32,
}

impl VendorCapabilities {
    /// A descriptor with the neutral defaults (all flags off, 16k in / 24k out).
    pub fn new(vendor: impl Into<String>, model: impl Into<String>, backend: Backend) -> Self {
        Self {
            vendor: vendor.into(),
            model: model.into(),
            backend,
            native_async_tools: false,
            session_resumption: false,
            system_content_turn: false,
            mid_session_config_update: false,
            item_truncate: false,
            self_denoise: false,
            input_sample_rate: 16000,
            output_sample_rate: 24000,
        }
    }
}
