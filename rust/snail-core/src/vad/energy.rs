//! EnergyVad — energy-based speech endpointing with hangover (port of `snail.audio.vad`).
//!
//! A pure, I/O-free state machine that classifies one interior frame at a time (480 samples,
//! int16, mono, 48kHz) as speech or silence and emits turn boundaries. The **hangover** holds a
//! turn open through brief mid-utterance pauses, so end-of-speech can be declared quickly
//! without cutting the user off. Threshold is an adaptive noise floor: an EMA of frame RMS
//! updated only while in SILENCE. State: SILENCE ⇄ SPEECH; one transition per push.

/// 10ms @ 48kHz mono (matches audio interior).
pub const FRAME_LEN: usize = 480;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VadState {
    Silence,
    Speech,
}

impl VadState {
    pub fn as_str(self) -> &'static str {
        match self {
            VadState::Silence => "silence",
            VadState::Speech => "speech",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VadEvent {
    None,
    Start,
    End,
}

/// Tunables for [`EnergyVad`]. Defaults match the Python `EnergyVad` constructor.
#[derive(Debug, Clone, Copy)]
pub struct EnergyVadConfig {
    pub frame_size: usize,
    pub start_frames: u32,
    pub hangover_frames: u32,
    pub margin: f64,
    pub alpha: f64,
    pub warmup_frames: u64,
}

impl Default for EnergyVadConfig {
    fn default() -> Self {
        Self {
            frame_size: FRAME_LEN,
            start_frames: 3,
            hangover_frames: 30,
            margin: 3.0,
            alpha: 0.05,
            warmup_frames: 10,
        }
    }
}

pub struct EnergyVad {
    start_frames: u32,
    hangover: u32,
    margin: f64,
    alpha: f64,
    warmup: u64,
    floor: f64,
    seen: u64,
    state: VadState,
    above: u32,
    below: u32,
    starts: u64,
    ends: u64,
}

impl EnergyVad {
    pub fn new(cfg: EnergyVadConfig) -> Self {
        assert!(cfg.frame_size >= 1, "frame_size must be >= 1");
        Self {
            start_frames: cfg.start_frames.max(1),
            hangover: cfg.hangover_frames.max(1),
            margin: cfg.margin,
            alpha: cfg.alpha,
            warmup: cfg.warmup_frames,
            floor: 0.0,
            seen: 0,
            state: VadState::Silence,
            above: 0,
            below: 0,
            starts: 0,
            ends: 0,
        }
    }

    /// Classify one frame → transition. Call once per interior frame in order.
    pub fn push(&mut self, frame: &[i16]) -> VadEvent {
        let rms = Self::rms(frame);
        self.seen += 1;
        let warming = self.seen <= self.warmup;
        if self.floor == 0.0 {
            self.floor = rms;
        } else if self.state == VadState::Silence {
            self.floor = (1.0 - self.alpha) * self.floor + self.alpha * rms;
        }
        let voiced = !warming && rms > self.floor * self.margin;

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

    /// Drop any in-flight speech back to SILENCE (keeps the floor estimate).
    pub fn reset(&mut self) {
        self.state = VadState::Silence;
        self.above = 0;
        self.below = 0;
    }

    pub fn state(&self) -> VadState {
        self.state
    }
    pub fn floor(&self) -> f64 {
        self.floor
    }
    pub fn starts(&self) -> u64 {
        self.starts
    }
    pub fn ends(&self) -> u64 {
        self.ends
    }

    /// RMS matching numpy `sqrt(mean((f32)^2, dtype=float64))`: square in f32, accumulate f64.
    fn rms(frame: &[i16]) -> f64 {
        if frame.is_empty() {
            return 0.0;
        }
        let mut acc = 0.0f64;
        for &s in frame {
            let x = s as f32;
            acc += (x * x) as f64;
        }
        (acc / frame.len() as f64).sqrt()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(n: usize, amp: i16) -> Vec<i16> {
        (0..n)
            .map(|i| if i % 2 == 0 { amp } else { -amp })
            .collect()
    }

    #[test]
    fn silence_stays_silent() {
        let mut v = EnergyVad::new(EnergyVadConfig::default());
        for _ in 0..50 {
            assert_eq!(v.push(&[0i16; 480]), VadEvent::None);
        }
        assert_eq!(v.state(), VadState::Silence);
    }

    #[test]
    fn speech_starts_after_debounce_and_ends_after_hangover() {
        let cfg = EnergyVadConfig {
            start_frames: 3,
            hangover_frames: 5,
            warmup_frames: 5,
            ..Default::default()
        };
        let mut v = EnergyVad::new(cfg);
        // warm up floor on quiet room tone
        for _ in 0..6 {
            v.push(&tone(480, 5));
        }
        let loud = tone(480, 4000);
        let mut started = false;
        for _ in 0..3 {
            if v.push(&loud) == VadEvent::Start {
                started = true;
            }
        }
        assert!(started);
        assert_eq!(v.state(), VadState::Speech);
        // now silence for hangover frames → END on the 5th
        let mut ended = VadEvent::None;
        for _ in 0..5 {
            ended = v.push(&[0i16; 480]);
        }
        assert_eq!(ended, VadEvent::End);
        assert_eq!(v.state(), VadState::Silence);
    }

    #[test]
    fn rms_matches_expected() {
        // constant-amplitude square wave: RMS == amplitude
        let f = tone(480, 1000);
        assert!((EnergyVad::rms(&f) - 1000.0).abs() < 1e-6);
    }
}
