//! PyO3 wrapper for the endpointing VAD — the TTFB-critical piece.
//!
//! Exposes [`snail_core::EnergyVad`] to Python as a drop-in for the Python `EnergyVad`: feed one
//! interior frame (480 int16 samples, as PCM16LE bytes) per `push`, get back the transition as a
//! string (`"none"` / `"start"` / `"end"`). The bridge maps `"start"`→`activity_start`,
//! `"end"`→`activity_end`. The confident short-hangover model VAD lands here once the Silero ONNX
//! backend is wired.

use pyo3::prelude::*;
use snail_core::vad::energy::{EnergyVad as CoreVad, EnergyVadConfig, VadEvent};

fn decode_le(bytes: &[u8]) -> Vec<i16> {
    bytes
        .chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect()
}

fn event_str(e: VadEvent) -> &'static str {
    match e {
        VadEvent::None => "none",
        VadEvent::Start => "start",
        VadEvent::End => "end",
    }
}

/// Adaptive-threshold energy VAD with start-debounce + hangover endpointing.
#[pyclass]
pub struct EnergyVad {
    inner: CoreVad,
}

#[pymethods]
impl EnergyVad {
    #[new]
    #[pyo3(signature = (
        frame_size = 480,
        start_frames = 3,
        hangover_frames = 30,
        margin = 3.0,
        alpha = 0.05,
        warmup_frames = 10,
    ))]
    fn new(
        frame_size: usize,
        start_frames: u32,
        hangover_frames: u32,
        margin: f64,
        alpha: f64,
        warmup_frames: u64,
    ) -> Self {
        Self {
            inner: CoreVad::new(EnergyVadConfig {
                frame_size,
                start_frames,
                hangover_frames,
                margin,
                alpha,
                warmup_frames,
            }),
        }
    }

    /// Classify one interior frame (PCM16LE bytes) → `"none"` / `"start"` / `"end"`.
    fn push(&mut self, frame: &[u8]) -> &'static str {
        event_str(self.inner.push(&decode_le(frame)))
    }

    /// Classify one interior frame given int16 samples directly (no byte decode).
    fn push_samples(&mut self, samples: Vec<i16>) -> &'static str {
        event_str(self.inner.push(&samples))
    }

    fn reset(&mut self) {
        self.inner.reset();
    }

    #[getter]
    fn state(&self) -> &'static str {
        self.inner.state().as_str()
    }

    #[getter]
    fn floor(&self) -> f64 {
        self.inner.floor()
    }

    #[getter]
    fn starts(&self) -> u64 {
        self.inner.starts()
    }

    #[getter]
    fn ends(&self) -> u64 {
        self.inner.ends()
    }
}
