//! snail-core — the Snail audio-plane hot path + endpointing VAD, in pure Rust.
//!
//! A faithful port of the Python `snail.audio` / `snail.router.gate` / `snail.audio.vad`
//! primitives, kept dependency-light and single-threaded (one loop per worker, docs 06). The
//! PyO3 bindings in the sibling `snail-rs` crate expose these to the existing Python bridge; a
//! later phase migrates the orchestration layer up here too.

pub mod audio;
pub mod context;
pub mod registry;
pub mod router;
pub mod tools;
pub mod vad;

pub use audio::{
    AudioCleaner, AudioCodec, AudioFrame, AudioPipeline, AudioSource, FanoutBus, FrameFlags,
    FramePool, JitterBuffer, LazyResampler, NullCleaner, OverflowPolicy, PcmCodec,
};
pub use context::{Event, EventLog, EventType, Item, Projection, Role};
pub use registry::{CallState, ToolCallRegistry};
pub use router::OutputGate;
pub use tools::{validate, ToolResult, ToolStatus};
pub use vad::{EnergyVad, EnergyVadConfig, VadEvent, VadState};
