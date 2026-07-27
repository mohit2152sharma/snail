//! FramePool — refcounted free-list of fixed int16 slabs (port of `snail.audio.pool`).
//!
//! Ownership is an explicit refcount so N subscriber rings, drop-oldest eviction, and
//! copy-then-release sinks all stay correct:
//!
//! 1. [`FramePool::try_acquire`] returns a frame with refcount = 1 (owned by the caller).
//! 2. Handing a frame to another consumer = exactly one [`FramePool::incref`] per hand-off.
//! 3. Every consumer calls [`FramePool::release`] exactly once when done with the view.
//! 4. The slab returns to the free-list only when refcount hits 0.
//!
//! Single-threaded (one loop per worker, docs 06) → deliberately lock-free.

use super::frame::{AudioFrame, AudioSource, FrameFlags};

/// Cheap, non-panicking stats snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PoolStats {
    pub capacity: usize,
    pub available: usize,
    pub in_use: usize,
    pub acquired_total: u64,
    pub released_total: u64,
    pub exhausted_total: u64,
}

pub struct FramePool {
    backing: Vec<i16>,
    refcount: Vec<i32>,
    free: Vec<usize>,
    capacity: usize,
    slab_samples: usize,
    stat_acquired: u64,
    stat_released: u64,
    stat_exhausted: u64,
}

impl FramePool {
    /// Free-list of `capacity` preallocated int16 slabs of `slab_samples` each.
    pub fn new(capacity: usize, slab_samples: usize) -> Self {
        assert!(
            capacity > 0 && slab_samples > 0,
            "capacity and slab_samples must be positive"
        );
        // free-list ordered so pop() hands out the lowest index first (matches Python).
        let free: Vec<usize> = (0..capacity).rev().collect();
        Self {
            backing: vec![0i16; capacity * slab_samples],
            refcount: vec![0i32; capacity],
            free,
            capacity,
            slab_samples,
            stat_acquired: 0,
            stat_released: 0,
            stat_exhausted: 0,
        }
    }

    /// The ingress primitive: returns `None` when the pool is exhausted (drop, don't crash).
    #[allow(clippy::too_many_arguments)]
    pub fn try_acquire(
        &mut self,
        n_samples: usize,
        sample_rate: u32,
        source: AudioSource,
        seq: u64,
        t_start: i64,
        flags: FrameFlags,
    ) -> Option<AudioFrame> {
        assert!(
            n_samples > 0 && n_samples <= self.slab_samples,
            "n_samples={n_samples} out of range (1..{})",
            self.slab_samples
        );
        let idx = match self.free.pop() {
            Some(i) => i,
            None => {
                self.stat_exhausted += 1;
                return None;
            }
        };
        self.refcount[idx] = 1;
        self.stat_acquired += 1;
        Some(AudioFrame {
            sample_rate,
            n_samples,
            source,
            seq,
            t_start,
            flags,
            slab_id: idx as i64,
        })
    }

    /// Panicking variant — a tripwire for callers that treat exhaustion as a bug.
    #[allow(clippy::too_many_arguments)]
    pub fn acquire(
        &mut self,
        n_samples: usize,
        sample_rate: u32,
        source: AudioSource,
        seq: u64,
        t_start: i64,
        flags: FrameFlags,
    ) -> AudioFrame {
        self.try_acquire(n_samples, sample_rate, source, seq, t_start, flags)
            .unwrap_or_else(|| panic!("all {} slabs in use", self.capacity))
    }

    /// Suggested capacity so `acquire` never fails in steady state:
    /// `Σ ring_depths + one-in-processing per consumer + Σ sink depths + margin`.
    pub fn recommend_capacity(
        ring_depths: &[usize],
        sink_ring_depths: &[usize],
        margin: usize,
    ) -> usize {
        let n_consumers = ring_depths.len();
        let rings: usize = ring_depths.iter().sum();
        let sinks: usize = sink_ring_depths.iter().sum();
        rings + n_consumers + sinks + margin
    }

    /// Add `n` owners to `frame`'s slab (one per additional hand-off).
    pub fn incref(&mut self, frame: &AudioFrame, n: i32) {
        assert!(n >= 1, "incref n must be >= 1");
        let idx = self.check_live(frame.slab_id);
        self.refcount[idx] += n;
    }

    /// Drop one owner. At refcount 0 the slab returns to the free-list; the caller's
    /// `frame.slab_id` is poisoned to -1 to trip any accidental reuse.
    pub fn release(&mut self, frame: &mut AudioFrame) {
        let idx = self.check_live(frame.slab_id);
        let rc = self.refcount[idx] - 1;
        self.refcount[idx] = rc;
        if rc == 0 {
            self.free.push(idx);
            self.stat_released += 1;
            frame.slab_id = -1;
        }
    }

    /// Read-only view of a live frame's samples.
    pub fn samples(&self, frame: &AudioFrame) -> &[i16] {
        let idx = self.assert_idx(frame.slab_id);
        let base = idx * self.slab_samples;
        &self.backing[base..base + frame.n_samples]
    }

    /// Mutable view of a live frame's samples (producer fills it before publish).
    pub fn samples_mut(&mut self, frame: &AudioFrame) -> &mut [i16] {
        let idx = self.assert_idx(frame.slab_id);
        let base = idx * self.slab_samples;
        &mut self.backing[base..base + frame.n_samples]
    }

    fn check_live(&self, slab_id: i64) -> usize {
        let idx = self.assert_idx(slab_id);
        assert!(
            self.refcount[idx] > 0,
            "ownership violation: slab {idx} refcount already 0 (double release or use-after-free)"
        );
        idx
    }

    fn assert_idx(&self, slab_id: i64) -> usize {
        assert!(
            slab_id >= 0 && (slab_id as usize) < self.capacity,
            "operation on a non-pool-backed or already-released frame"
        );
        slab_id as usize
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }
    pub fn slab_samples(&self) -> usize {
        self.slab_samples
    }
    pub fn available(&self) -> usize {
        self.free.len()
    }
    pub fn in_use(&self) -> usize {
        self.capacity - self.free.len()
    }

    pub fn stats(&self) -> PoolStats {
        PoolStats {
            capacity: self.capacity,
            available: self.available(),
            in_use: self.in_use(),
            acquired_total: self.stat_acquired,
            released_total: self.stat_released,
            exhausted_total: self.stat_exhausted,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn acq(p: &mut FramePool, n: usize, seq: u64) -> AudioFrame {
        p.acquire(n, 48000, AudioSource::UserRaw, seq, 0, FrameFlags::NONE)
    }

    #[test]
    fn acquire_hands_out_low_index_first() {
        let mut p = FramePool::new(3, 480);
        let a = acq(&mut p, 480, 1);
        let b = acq(&mut p, 480, 2);
        assert_eq!(a.slab_id, 0);
        assert_eq!(b.slab_id, 1);
        assert_eq!(p.in_use(), 2);
    }

    #[test]
    fn refcount_release_returns_slab() {
        let mut p = FramePool::new(2, 480);
        let mut a = acq(&mut p, 480, 1);
        p.incref(&a, 2); // net refcount 3
        p.release(&mut a);
        p.release(&mut a);
        assert_eq!(p.available(), 1);
        p.release(&mut a); // final ref → slab freed, poisoned
        assert_eq!(a.slab_id, -1);
        assert_eq!(p.available(), 2);
    }

    #[test]
    fn samples_roundtrip() {
        let mut p = FramePool::new(1, 4);
        let f = acq(&mut p, 4, 1);
        p.samples_mut(&f).copy_from_slice(&[1, 2, 3, 4]);
        assert_eq!(p.samples(&f), &[1, 2, 3, 4]);
    }

    #[test]
    fn exhaustion_returns_none_and_counts() {
        let mut p = FramePool::new(1, 4);
        let _a = acq(&mut p, 4, 1);
        assert!(p
            .try_acquire(4, 48000, AudioSource::UserRaw, 2, 0, FrameFlags::NONE)
            .is_none());
        assert_eq!(p.stats().exhausted_total, 1);
    }

    #[test]
    #[should_panic(expected = "ownership violation")]
    fn double_release_panics() {
        let mut p = FramePool::new(1, 4);
        let mut a = acq(&mut p, 4, 1);
        p.release(&mut a);
        // slab_id now -1 → next release panics via assert_idx
        a.slab_id = 0;
        p.release(&mut a);
    }

    #[test]
    fn recommend_capacity_formula() {
        assert_eq!(
            FramePool::recommend_capacity(&[8, 8], &[], 8),
            8 + 8 + 2 + 8
        );
        assert_eq!(FramePool::recommend_capacity(&[8], &[4], 8), 8 + 1 + 4 + 8);
    }
}
