//! Audio cleaner — per-consumer denoise stage (port of `snail.audio.clean`).
//!
//! Sits on the user-input leg after the RAW fan-out; runs only when a consumer wants CLEAN.
//! Emits cleaned 48kHz mono int16 onto the CLEAN fan-out. Two swappable pieces: an
//! [`AudioCleaner`] (`NullCleaner` bypass / [`RNNoiseCleaner`]) and an injected
//! [`DenoiseBackend`] per-frame kernel. RNNoise wants exactly 480-sample (10ms) frames, so the
//! [`Rechunker`] re-aligns arbitrary input using a preallocated 480-sample accumulator.

/// 10ms @ 48kHz mono — the RNNoise frame size.
pub const FRAME_LEN: usize = 480;

/// The per-frame denoise kernel (e.g. an `librnnoise` binding). `process_480` takes one
/// 480-sample int16 mono frame @48k and returns a cleaned 480-sample frame. Stateful across
/// calls — one backend instance per stream.
pub trait DenoiseBackend {
    fn process_480(&mut self, frame: &[i16]) -> Vec<i16>;
}

/// Swappable denoise stage. `process` may emit 0+ cleaned frames per call.
pub trait AudioCleaner {
    fn process(&mut self, samples: &[i16]) -> Vec<Vec<i16>>;
    fn flush(&mut self) -> Vec<Vec<i16>>;
    fn reset(&mut self);
}

/// Bypass — pass audio through untouched (no rechunk, no denoise).
#[derive(Debug, Default)]
pub struct NullCleaner;

impl AudioCleaner for NullCleaner {
    fn process(&mut self, samples: &[i16]) -> Vec<Vec<i16>> {
        if samples.is_empty() {
            vec![]
        } else {
            vec![samples.to_vec()]
        }
    }
    fn flush(&mut self) -> Vec<Vec<i16>> {
        vec![]
    }
    fn reset(&mut self) {}
}

/// Re-aligns an arbitrary-length int16 stream to fixed 480-sample frames using a preallocated
/// accumulator + fill index (no growing buffers).
pub struct Rechunker {
    buf: [i16; FRAME_LEN],
    fill: usize,
}

impl Default for Rechunker {
    fn default() -> Self {
        Self::new()
    }
}

impl Rechunker {
    pub fn new() -> Self {
        Self {
            buf: [0i16; FRAME_LEN],
            fill: 0,
        }
    }

    /// Feed samples; return every complete 480-frame now available.
    pub fn push(&mut self, samples: &[i16]) -> Vec<Vec<i16>> {
        let mut out = Vec::new();
        let mut pos = 0;
        let n = samples.len();
        while pos < n {
            let take = (FRAME_LEN - self.fill).min(n - pos);
            self.buf[self.fill..self.fill + take].copy_from_slice(&samples[pos..pos + take]);
            self.fill += take;
            pos += take;
            if self.fill == FRAME_LEN {
                out.push(self.buf.to_vec());
                self.fill = 0;
            }
        }
        out
    }

    /// Emit the final partial frame zero-padded to 480, or `None` if empty.
    pub fn flush(&mut self) -> Option<Vec<i16>> {
        if self.fill == 0 {
            return None;
        }
        let mut frame = vec![0i16; FRAME_LEN];
        frame[..self.fill].copy_from_slice(&self.buf[..self.fill]);
        self.fill = 0;
        Some(frame)
    }

    pub fn reset(&mut self) {
        self.fill = 0;
    }
}

/// Default cleaner: rechunk to 480 frames, denoise each via the backend. Input and output are
/// 48k mono int16, so no resample surrounds it.
pub struct RNNoiseCleaner<B: DenoiseBackend> {
    backend: B,
    rechunk: Rechunker,
}

impl<B: DenoiseBackend> RNNoiseCleaner<B> {
    pub fn new(backend: B) -> Self {
        Self {
            backend,
            rechunk: Rechunker::new(),
        }
    }
}

impl<B: DenoiseBackend> AudioCleaner for RNNoiseCleaner<B> {
    fn process(&mut self, samples: &[i16]) -> Vec<Vec<i16>> {
        self.rechunk
            .push(samples)
            .iter()
            .map(|f| self.backend.process_480(f))
            .collect()
    }
    fn flush(&mut self) -> Vec<Vec<i16>> {
        match self.rechunk.flush() {
            Some(tail) => vec![self.backend.process_480(&tail)],
            None => vec![],
        }
    }
    fn reset(&mut self) {
        self.rechunk.reset();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rechunker_aligns_to_480() {
        let mut r = Rechunker::new();
        assert!(r.push(&vec![1i16; 300]).is_empty()); // held
        let out = r.push(&vec![2i16; 300]); // 300+300 = 600 → one full 480
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].len(), FRAME_LEN);
        assert_eq!(out[0][0], 1);
        assert_eq!(out[0][479], 2);
    }

    #[test]
    fn rechunker_flush_zero_pads() {
        let mut r = Rechunker::new();
        r.push(&vec![7i16; 100]);
        let tail = r.flush().unwrap();
        assert_eq!(tail.len(), FRAME_LEN);
        assert_eq!(tail[0], 7);
        assert_eq!(tail[100], 0);
        assert!(r.flush().is_none());
    }

    #[test]
    fn null_cleaner_passthrough() {
        let mut c = NullCleaner;
        assert_eq!(c.process(&[1, 2, 3]), vec![vec![1, 2, 3]]);
        assert!(c.process(&[]).is_empty());
    }

    struct GainBackend;
    impl DenoiseBackend for GainBackend {
        fn process_480(&mut self, frame: &[i16]) -> Vec<i16> {
            frame.iter().map(|&s| s / 2).collect()
        }
    }

    #[test]
    fn rnnoise_cleaner_denoises_full_frames() {
        let mut c = RNNoiseCleaner::new(GainBackend);
        assert!(c.process(&vec![100i16; 200]).is_empty()); // no full frame yet
        let out = c.process(&vec![100i16; 400]); // 600 total → one 480 frame denoised
        assert_eq!(out.len(), 1);
        assert_eq!(out[0][0], 50); // gain applied
    }
}
