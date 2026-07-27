//! Resample — lazy, per-target-rate rate conversion (port of `snail.audio.resample`).
//!
//! Resampling is the CPU lever the pipeline is built around: the 48k interior is copy-free, and
//! a leg converts **only when its vendor rate differs from 48k**, memoized per distinct target
//! rate so N same-rate legs share one stateful converter. The DSP kernel is an injected
//! [`ResampleBackend`] (e.g. rubato), so this module stays dependency-free and testable.

use std::collections::HashMap;

/// A stateful streaming converter for one fixed `(from_rate, to_rate)` pair.
pub trait Resampler {
    fn process(&mut self, samples: &[i16]) -> Vec<i16>;
}

/// Factory for per-rate-pair converters.
pub trait ResampleBackend {
    fn stream(&self, from_rate: u32, to_rate: u32) -> Box<dyn Resampler>;
}

/// Per-distinct-rate memoized resampler with an equal-rate fast path.
///
/// Holds at most one converter per distinct `(from_rate, to_rate)` seen. `resample` at equal
/// rates returns the input untouched.
pub struct LazyResampler {
    backend: Box<dyn ResampleBackend>,
    streams: HashMap<(u32, u32), Box<dyn Resampler>>,
}

impl LazyResampler {
    pub fn new(backend: Box<dyn ResampleBackend>) -> Self {
        Self {
            backend,
            streams: HashMap::new(),
        }
    }

    /// Convert `samples` from `from_rate` to `to_rate`. Equal rates are a no-op copy; the caller
    /// on the hot path skips this call entirely when it can (see the pipeline drain).
    pub fn resample(&mut self, samples: &[i16], from_rate: u32, to_rate: u32) -> Vec<i16> {
        if from_rate == to_rate {
            return samples.to_vec();
        }
        let key = (from_rate, to_rate);
        let stream = self
            .streams
            .entry(key)
            .or_insert_with(|| self.backend.stream(from_rate, to_rate));
        stream.process(samples)
    }

    /// Distinct converter rate-pairs currently memoized (for stats/tests).
    pub fn rate_pairs(&self) -> Vec<(u32, u32)> {
        self.streams.keys().copied().collect()
    }

    pub fn reset(&mut self) {
        self.streams.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test backend: scales length by to/from with nearest-neighbour (deterministic, dep-free).
    struct NearestBackend;
    struct NearestStream {
        from: u32,
        to: u32,
    }
    impl Resampler for NearestStream {
        fn process(&mut self, samples: &[i16]) -> Vec<i16> {
            let out_len = samples.len() * self.to as usize / self.from as usize;
            (0..out_len)
                .map(|i| samples[i * self.from as usize / self.to as usize])
                .collect()
        }
    }
    impl ResampleBackend for NearestBackend {
        fn stream(&self, from_rate: u32, to_rate: u32) -> Box<dyn Resampler> {
            Box::new(NearestStream {
                from: from_rate,
                to: to_rate,
            })
        }
    }

    #[test]
    fn equal_rate_is_noop() {
        let mut r = LazyResampler::new(Box::new(NearestBackend));
        assert_eq!(r.resample(&[1, 2, 3], 48000, 48000), vec![1, 2, 3]);
        assert!(r.rate_pairs().is_empty()); // no converter created
    }

    #[test]
    fn differing_rate_memoizes_one_converter() {
        let mut r = LazyResampler::new(Box::new(NearestBackend));
        let down = r.resample(&[10, 20, 30, 40, 50, 60], 48000, 16000);
        assert_eq!(down, vec![10, 40]); // 3:1 decimate
        r.resample(&[1, 2, 3], 48000, 16000);
        assert_eq!(r.rate_pairs(), vec![(48000, 16000)]); // memoized once
    }
}
