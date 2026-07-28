//! Endpointing VAD — energy VAD today; a speech-model VAD (Silero/WebRTC) lands here for the
//! confident near-0ms hangover that reduces TTFB further.

pub mod energy;
pub mod model;

pub use energy::{EnergyVad, EnergyVadConfig, VadEvent, VadState};
pub use model::{EndpointVad, ModelVad, ModelVadConfig, SpeechScorer};
