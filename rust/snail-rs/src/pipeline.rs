//! PyO3 wrapper for the full audio [`snail_core::AudioPipeline`].
//!
//! Exposes the ingress/egress runners to the Python bridge behind the same method surface the
//! Python `AudioPipeline` had (`on_client_audio` / `drain` / `on_vendor_audio` / `playout` /
//! `cut`) plus consumer management.
//!
//! ## Resample backend
//! Ships a dependency-free **stateful linear-interpolation** resampler as a functional v0. It
//! carries the last input sample + fractional phase across chunks (no per-chunk boundary clicks).
//! A libsoxr-quality backend (bit-parity vs the Python `soxr` leg) is a marked follow-up
//! (P1.5) — swap the `ResampleBackend` with no pipeline change.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use snail_core::audio::codec::PcmCodec;
use snail_core::audio::fanout::FanoutBus;
use snail_core::audio::frame::AudioSource;
use snail_core::audio::jitter::JitterBuffer;
use snail_core::audio::pool::FramePool;
use snail_core::audio::resample::{LazyResampler, ResampleBackend, Resampler};
use snail_core::router::OutputGate;
use snail_core::AudioPipeline;

// --- stateful linear-interpolation resample backend (v0) --------------------

struct LinearStream {
    step: f64, // source samples advanced per output sample = from/to
    phase: f64,
    prev: f32,
    primed: bool,
}

impl Resampler for LinearStream {
    fn process(&mut self, samples: &[i16]) -> Vec<i16> {
        if samples.is_empty() {
            return Vec::new();
        }
        // Virtual input buffer: [prev?] ++ samples (prev gives cross-chunk continuity).
        let mut buf: Vec<f32> = Vec::with_capacity(samples.len() + 1);
        if self.primed {
            buf.push(self.prev);
        }
        buf.extend(samples.iter().map(|&s| s as f32));

        let last = (buf.len() - 1) as f64;
        let mut out = Vec::new();
        let mut t = self.phase;
        while t <= last {
            let i = t.floor() as usize;
            let frac = t - i as f64;
            let a = buf[i];
            let b = if i + 1 < buf.len() { buf[i + 1] } else { a };
            let v = a as f64 * (1.0 - frac) + b as f64 * frac;
            out.push(v.round().clamp(i16::MIN as f64, i16::MAX as f64) as i16);
            t += self.step;
        }
        // Carry: next chunk prepends this chunk's last sample, so the new index 0 aligns to the
        // old last index; remaining phase is measured from there.
        self.phase = t - last;
        self.prev = *samples.last().unwrap() as f32;
        self.primed = true;
        out
    }
}

struct LinearBackend;

impl ResampleBackend for LinearBackend {
    fn stream(&self, from_rate: u32, to_rate: u32) -> Box<dyn Resampler> {
        Box::new(LinearStream {
            step: from_rate as f64 / to_rate as f64,
            phase: 0.0,
            prev: 0.0,
            primed: false,
        })
    }
}

// --- the pipeline pyclass ---------------------------------------------------

fn parse_source(s: &str) -> PyResult<AudioSource> {
    match s {
        "raw" | "user_raw" => Ok(AudioSource::UserRaw),
        "clean" | "user_clean" => Ok(AudioSource::UserClean),
        other => Err(PyValueError::new_err(format!(
            "source must be 'raw' or 'clean', got {other:?}"
        ))),
    }
}

/// The audio-plane runner for one session (ingress + egress), backed by Rust.
///
/// `unsendable`: one pipeline is bound to one session loop (docs 06) and holds injected trait
/// objects (codec/resampler) that aren't `Send`. Accessing it from another thread panics — which
/// is exactly the single-loop invariant we want enforced.
#[pyclass(unsendable)]
pub struct Pipeline {
    inner: AudioPipeline,
}

#[pymethods]
impl Pipeline {
    /// Build a pipeline. `capacity`/`slab_samples` size the ingress frame pool; `client_rate` is
    /// the client leg's sample rate (interior is 48k); `prefill_frames`/`gate_depth` tune the
    /// egress jitter buffer + output gate.
    #[new]
    #[pyo3(signature = (
        capacity = 128,
        slab_samples = 480,
        client_rate = 48000,
        prefill_frames = 3,
        gate_depth = 32,
    ))]
    fn new(
        capacity: usize,
        slab_samples: usize,
        client_rate: u32,
        prefill_frames: usize,
        gate_depth: usize,
    ) -> Self {
        let inner = AudioPipeline::new(
            FramePool::new(capacity, slab_samples),
            FanoutBus::new(),
            LazyResampler::new(Box::new(LinearBackend)),
            OutputGate::new(gate_depth),
            JitterBuffer::new(480, prefill_frames),
            None, // cleaner (RNNoise) wired in a later phase
            Box::new(PcmCodec),
            client_rate,
        );
        Self { inner }
    }

    /// Attach a consumer to the fan-out bus (GATE 1). `source` = `"raw"` / `"clean"`;
    /// `target_rate` is the consumer's vendor input rate (its leg resamples 48k→that).
    #[pyo3(signature = (consumer_id, source, target_rate, depth = 8))]
    fn attach_consumer(
        &mut self,
        consumer_id: &str,
        source: &str,
        target_rate: u32,
        depth: usize,
    ) -> PyResult<()> {
        self.inner
            .attach_consumer(consumer_id, parse_source(source)?, target_rate, depth);
        Ok(())
    }

    /// Unsubscribe a consumer, releasing its buffered slabs. Returns frames released.
    fn detach_consumer(&mut self, consumer_id: &str) -> usize {
        self.inner.detach_consumer(consumer_id)
    }

    /// Give the output token to `agent_id` — only its audio reaches the user (GATE 2).
    fn hold_token(&mut self, agent_id: &str) {
        self.inner.hold_token(agent_id);
    }

    /// Ingress: decode one client media frame (PCM16LE bytes) and publish to the bus. Returns the
    /// RAW 48k frames published this call, each as PCM16LE bytes (for VAD/testing).
    fn on_client_audio<'py>(&mut self, py: Python<'py>, data: &[u8]) -> Vec<Bound<'py, PyBytes>> {
        self.inner
            .on_client_audio(data)
            .into_iter()
            .map(|frame| {
                let mut bytes = Vec::with_capacity(frame.len() * 2);
                for s in frame {
                    bytes.extend_from_slice(&s.to_le_bytes());
                }
                PyBytes::new(py, &bytes)
            })
            .collect()
    }

    /// Pull every subscriber's ring → vendor-ready PCM bytes, keyed by subscriber id.
    fn drain<'py>(&mut self, py: Python<'py>) -> HashMap<String, Vec<Bound<'py, PyBytes>>> {
        self.inner
            .drain()
            .into_iter()
            .map(|(id, chunks)| {
                let pyc = chunks.into_iter().map(|c| PyBytes::new(py, &c)).collect();
                (id, pyc)
            })
            .collect()
    }

    /// Egress: push one vendor output burst (PCM16 mono LE) into the jitter buffer at 48k.
    fn on_vendor_audio(&mut self, pcm: &[u8], vendor_rate: u32) {
        self.inner.on_vendor_audio(pcm, vendor_rate);
    }

    /// Paced egress drain → client bytes, or `None` if no frame is due / not the token holder.
    fn playout<'py>(&mut self, py: Python<'py>, agent_id: &str) -> Option<Bound<'py, PyBytes>> {
        self.inner.playout(agent_id).map(|b| PyBytes::new(py, &b))
    }

    /// Barge-in / CUT_NOW on the output path: flush jitter + gate rings.
    fn cut(&mut self) {
        self.inner.cut();
    }

    /// Snapshot of pipeline counters.
    fn stats(&self) -> HashMap<String, u64> {
        let s = self.inner.stats();
        HashMap::from([
            ("ingress_dropped".to_string(), s.ingress_dropped),
            ("jitter_underruns".to_string(), s.jitter_underruns),
            ("gate_suppressed".to_string(), s.gate_suppressed),
            ("resample_pairs".to_string(), s.resample_pairs as u64),
        ])
    }
}
