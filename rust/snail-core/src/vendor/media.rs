//! Neutral multimodal media for the outbound seams (port of `snail.vendor.media`).
//!
//! Vendors take multimodal input over a realtime channel (continuous audio/image/text) and an
//! ordered turns channel. [`MediaChunk`] is the realtime unit; ordered turns reuse
//! [`crate::context::Item`]. The adapter maps each to the right vendor call.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MediaKind {
    Audio,
    Image,
    Text,
}

/// Out-of-band control markers on the realtime channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RealtimeControl {
    /// manual VAD: user speech begins.
    ActivityStart,
    /// manual VAD: user speech ends.
    ActivityEnd,
    /// no more audio coming.
    AudioStreamEnd,
}

/// One realtime multimodal chunk. Exactly one of audio/image/text is meaningful per `kind`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaChunk {
    pub kind: MediaKind,
    pub data: Option<Vec<u8>>,
    pub text: Option<String>,
    pub mime_type: Option<String>,
    pub sample_rate: Option<u32>,
}

impl MediaChunk {
    pub fn audio(pcm: Vec<u8>, sample_rate: u32) -> Self {
        Self {
            kind: MediaKind::Audio,
            data: Some(pcm),
            text: None,
            mime_type: None,
            sample_rate: Some(sample_rate),
        }
    }

    pub fn image(data: Vec<u8>, mime_type: impl Into<String>) -> Self {
        Self {
            kind: MediaKind::Image,
            data: Some(data),
            text: None,
            mime_type: Some(mime_type.into()),
            sample_rate: None,
        }
    }

    pub fn text(text: impl Into<String>) -> Self {
        Self {
            kind: MediaKind::Text,
            data: None,
            text: Some(text.into()),
            mime_type: None,
            sample_rate: None,
        }
    }
}
