//! snail-core — the Snail audio-plane hot path + endpointing VAD, in pure Rust.
//!
//! A faithful port of the Python `snail.audio` / `snail.router.gate` / `snail.audio.vad`
//! primitives, kept dependency-light and single-threaded (one loop per worker, docs 06). The
//! PyO3 bindings in the sibling `snail-rs` crate expose these to the existing Python bridge; a
//! later phase migrates the orchestration layer up here too.

pub mod audio;
pub mod router;
pub mod vad;

pub use audio::{
    AudioCleaner, AudioCodec, AudioFrame, AudioPipeline, AudioSource, FanoutBus, FrameFlags,
    FramePool, JitterBuffer, LazyResampler, NullCleaner, OverflowPolicy, PcmCodec,
};
pub use router::OutputGate;
pub use vad::{EnergyVad, EnergyVadConfig, VadEvent, VadState};
