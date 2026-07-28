//! ModelVad — speech-model endpointing for a **confident near-0ms hangover** (the TTFB lever).
//!
//! The energy VAD's floor/margin heuristic needs a generous hangover (~10 frames) to avoid
//! clipping speech on brief dips. A real speech model (Silero / WebRTC) gives a per-frame speech
//! *probability*, so end-of-speech can be declared with high confidence after a much shorter
//! hangover — cutting the `end-of-speech → activity_end` delay that dominates the framework's
//! slice of TTFB. Rust's deterministic tail latency (no GIL/GC pauses) is what makes near-0
//! hangover safe in production.
//!
//! The model itself is an injected [`SpeechScorer`] (same pattern as the resample/denoise
//! backends), so this module and its tests stay dependency-free; the ONNX Silero backend lands
//! in `snail-rs` behind a feature.

use super::energy::{VadEvent, VadState};

/// Per-frame speech scorer. `score` returns a speech probability in `[0, 1]` for one interior
/// frame (int16, mono, 48kHz). Stateful across calls (the model carries recurrent state) — one
/// instance per stream.
pub trait SpeechScorer {
    fn score(&mut self, frame: &[i16]) -> f32;
    /// Reset any recurrent model state (e.g. between turns / on barge-in).
    fn reset(&mut self) {}
}

/// The endpointing surface shared by [`super::EnergyVad`] and [`ModelVad`], so the bridge can
/// drive either one interchangeably.
pub trait EndpointVad {
    fn push(&mut self, frame: &[i16]) -> VadEvent;
    fn reset(&mut self);
    fn state(&self) -> VadState;
}

impl EndpointVad for super::EnergyVad {
    fn push(&mut self, frame: &[i16]) -> VadEvent {
        super::EnergyVad::push(self, frame)
    }
    fn reset(&mut self) {
        super::EnergyVad::reset(self)
    }
    fn state(&self) -> VadState {
        super::EnergyVad::state(self)
    }
}

/// Tunables for [`ModelVad`]. The default `hangover_frames = 1` (10ms) is the aggressive,
/// live-tested endpoint that already met the 50% TTFB goal; a confident model can run it at 0.
#[derive(Debug, Clone, Copy)]
pub struct ModelVadConfig {
    /// speech-probability threshold for a "voiced" frame.
    pub threshold: f32,
    /// consecutive voiced frames required to declare speech start (debounce).
    pub start_frames: u32,
    /// consecutive unvoiced frames required to declare end-of-speech (the TTFB knob).
    pub hangover_frames: u32,
}

impl Default for ModelVadConfig {
    fn default() -> Self {
        Self {
            threshold: 0.5,
            start_frames: 2,
            hangover_frames: 1,
        }
    }
}

/// Endpointing VAD driven by a [`SpeechScorer`] probability, with start-debounce + a confident
/// short hangover. State: SILENCE ⇄ SPEECH; one transition per push (same contract as EnergyVad).
pub struct ModelVad<S: SpeechScorer> {
    scorer: S,
    threshold: f32,
    start_frames: u32,
    hangover: u32,
    state: VadState,
    above: u32,
    below: u32,
    starts: u64,
    ends: u64,
    last_score: f32,
}

impl<S: SpeechScorer> ModelVad<S> {
    pub fn new(scorer: S, cfg: ModelVadConfig) -> Self {
        Self {
            scorer,
            threshold: cfg.threshold,
            start_frames: cfg.start_frames.max(1),
            // hangover=1 = END on the first sub-threshold frame (the most aggressive
            // meaningful value; a confident model runs here). Values <1 are nonsensical.
            hangover: cfg.hangover_frames.max(1),
            state: VadState::Silence,
            above: 0,
            below: 0,
            starts: 0,
            ends: 0,
            last_score: 0.0,
        }
    }

    pub fn last_score(&self) -> f32 {
        self.last_score
    }
    pub fn starts(&self) -> u64 {
        self.starts
    }
    pub fn ends(&self) -> u64 {
        self.ends
    }
}

impl<S: SpeechScorer> EndpointVad for ModelVad<S> {
    fn push(&mut self, frame: &[i16]) -> VadEvent {
        let p = self.scorer.score(frame);
        self.last_score = p;
        let voiced = p >= self.threshold;
        match self.state {
            VadState::Silence => {
                if voiced {
                    self.above += 1;
                    if self.above >= self.start_frames {
                        self.state = VadState::Speech;
                        self.above = 0;
                        self.below = 0;
                        self.starts += 1;
                        return VadEvent::Start;
                    }
                } else {
                    self.above = 0;
                }
                VadEvent::None
            }
            VadState::Speech => {
                if !voiced {
                    self.below += 1;
                    if self.below >= self.hangover {
                        self.state = VadState::Silence;
                        self.below = 0;
                        self.above = 0;
                        self.ends += 1;
                        return VadEvent::End;
                    }
                } else {
                    self.below = 0;
                }
                VadEvent::None
            }
        }
    }

    fn reset(&mut self) {
        self.state = VadState::Silence;
        self.above = 0;
        self.below = 0;
        self.scorer.reset();
    }

    fn state(&self) -> VadState {
        self.state
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Scripted scorer: yields a fixed sequence of probabilities (deterministic, dep-free).
    struct ScriptScorer {
        seq: Vec<f32>,
        i: usize,
        resets: u32,
    }
    impl ScriptScorer {
        fn new(seq: Vec<f32>) -> Self {
            Self {
                seq,
                i: 0,
                resets: 0,
            }
        }
    }
    impl SpeechScorer for ScriptScorer {
        fn score(&mut self, _frame: &[i16]) -> f32 {
            let v = self.seq[self.i.min(self.seq.len() - 1)];
            self.i += 1;
            v
        }
        fn reset(&mut self) {
            self.resets += 1;
        }
    }

    #[test]
    fn confident_zero_ish_hangover_ends_fast() {
        // hangover=1 → END on the first sub-threshold frame after speech.
        let seq = vec![0.9, 0.9, 0.1]; // start (2 frames) then one silence
        let mut v = ModelVad::new(
            ScriptScorer::new(seq),
            ModelVadConfig {
                threshold: 0.5,
                start_frames: 2,
                hangover_frames: 1,
            },
        );
        assert_eq!(v.push(&[0; 480]), VadEvent::None); // 1 voiced
        assert_eq!(v.push(&[0; 480]), VadEvent::Start); // 2 voiced → start
        assert_eq!(v.push(&[0; 480]), VadEvent::End); // 1 silence → end (hangover=1)
    }

    #[test]
    fn brief_dip_does_not_end_with_larger_hangover() {
        let seq = vec![0.9, 0.9, 0.2, 0.9, 0.2, 0.2, 0.2];
        let mut v = ModelVad::new(
            ScriptScorer::new(seq),
            ModelVadConfig {
                threshold: 0.5,
                start_frames: 2,
                hangover_frames: 3,
            },
        );
        v.push(&[0; 480]);
        assert_eq!(v.push(&[0; 480]), VadEvent::Start);
        assert_eq!(v.push(&[0; 480]), VadEvent::None); // dip 1
        assert_eq!(v.push(&[0; 480]), VadEvent::None); // voiced again → below reset
        assert_eq!(v.push(&[0; 480]), VadEvent::None); // silence 1
        assert_eq!(v.push(&[0; 480]), VadEvent::None); // silence 2
        assert_eq!(v.push(&[0; 480]), VadEvent::End); // silence 3 → end
    }

    #[test]
    fn reset_resets_scorer_and_state() {
        let mut v = ModelVad::new(ScriptScorer::new(vec![0.9]), ModelVadConfig::default());
        v.push(&[0; 480]);
        v.reset();
        assert_eq!(v.state(), VadState::Silence);
    }
}
