//! JitterBuffer — smooth bursty vendor output to the speaker clock (port of `snail.audio.jitter`).
//!
//! Vendor audio arrives in bursts; the speaker consumes at a steady clock. The buffer
//! **prebuffers** to `prefill` before releasing anything, then hands out fixed-size frames on
//! each paced drain. State machine: PREBUFFERING until fill ≥ prefill, then PLAYING; on
//! underrun (a drain with < one frame buffered) it counts the event and re-arms prebuffering.
//!
//! Internally a deque of int16 chunks + a head offset — no per-push concatenation; a frame is
//! stitched across chunk boundaries only when one actually straddles them.

use std::collections::VecDeque;

/// 10ms @ 48kHz mono.
pub const FRAME_LEN: usize = 480;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JitterState {
    Prebuffering,
    Playing,
}

impl JitterState {
    pub fn as_str(self) -> &'static str {
        match self {
            JitterState::Prebuffering => "prebuffering",
            JitterState::Playing => "playing",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JitterStats {
    pub buffered: usize,
    pub underruns_total: u64,
    pub state: JitterState,
}

pub struct JitterBuffer {
    frame: usize,
    prefill: usize,
    chunks: VecDeque<Vec<i16>>,
    head: usize,
    total: usize,
    state: JitterState,
    underruns: u64,
}

impl JitterBuffer {
    pub fn new(frame_size: usize, prefill_frames: usize) -> Self {
        assert!(frame_size >= 1, "frame_size must be >= 1");
        Self {
            frame: frame_size,
            prefill: frame_size * prefill_frames.max(1),
            chunks: VecDeque::new(),
            head: 0,
            total: 0,
            state: JitterState::Prebuffering,
            underruns: 0,
        }
    }

    pub fn with_defaults() -> Self {
        Self::new(FRAME_LEN, 3)
    }

    pub fn state(&self) -> JitterState {
        self.state
    }
    pub fn buffered(&self) -> usize {
        self.total
    }

    /// Add a vendor burst. Once fill reaches `prefill`, playout arms.
    pub fn push(&mut self, samples: &[i16]) {
        if samples.is_empty() {
            return;
        }
        self.chunks.push_back(samples.to_vec());
        self.total += samples.len();
        if self.state == JitterState::Prebuffering && self.total >= self.prefill {
            self.state = JitterState::Playing;
        }
    }

    /// Paced drain: one `frame_size` frame, or `None` if not ready (prebuffering/underrun).
    pub fn pop(&mut self) -> Option<Vec<i16>> {
        if self.state != JitterState::Playing {
            return None;
        }
        if self.total < self.frame {
            self.underruns += 1;
            self.state = JitterState::Prebuffering;
            return None;
        }
        Some(self.take(self.frame))
    }

    /// Flush the tail (< one frame) at end-of-turn, zero-padded, or `None`.
    pub fn drain_partial(&mut self) -> Option<Vec<i16>> {
        if self.total == 0 {
            return None;
        }
        let n = self.frame.min(self.total);
        let mut frame = self.take(n);
        if n < self.frame {
            frame.resize(self.frame, 0);
        }
        Some(frame)
    }

    /// Discard everything (barge-in / cut) and re-arm prebuffering.
    pub fn flush(&mut self) {
        self.chunks.clear();
        self.head = 0;
        self.total = 0;
        self.state = JitterState::Prebuffering;
    }

    pub fn stats(&self) -> JitterStats {
        JitterStats {
            buffered: self.total,
            underruns_total: self.underruns,
            state: self.state,
        }
    }

    /// Pull exactly `n` buffered samples, stitching across chunk boundaries.
    fn take(&mut self, n: usize) -> Vec<i16> {
        let head = self.chunks.front().unwrap();
        // Fast path: the current head chunk alone satisfies the request.
        if head.len() - self.head >= n {
            let out = head[self.head..self.head + n].to_vec();
            self.head += n;
            if self.head == head.len() {
                self.chunks.pop_front();
                self.head = 0;
            }
            self.total -= n;
            return out;
        }
        // Slow path: stitch pieces across boundaries.
        let mut out = Vec::with_capacity(n);
        let mut need = n;
        while need > 0 {
            let chunk = self.chunks.front().unwrap();
            let avail = chunk.len() - self.head;
            let take = avail.min(need);
            out.extend_from_slice(&chunk[self.head..self.head + take]);
            self.head += take;
            need -= take;
            if self.head == chunk.len() {
                self.chunks.pop_front();
                self.head = 0;
            }
        }
        self.total -= n;
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prebuffers_then_plays() {
        let mut j = JitterBuffer::new(4, 2); // prefill = 8
        j.push(&[1, 2, 3, 4]);
        assert_eq!(j.state(), JitterState::Prebuffering);
        assert!(j.pop().is_none());
        j.push(&[5, 6, 7, 8]);
        assert_eq!(j.state(), JitterState::Playing);
        assert_eq!(j.pop().unwrap(), vec![1, 2, 3, 4]);
        assert_eq!(j.pop().unwrap(), vec![5, 6, 7, 8]);
    }

    #[test]
    fn underrun_rearms_prebuffering() {
        let mut j = JitterBuffer::new(4, 1); // prefill = 4
        j.push(&[1, 2, 3, 4]);
        assert_eq!(j.pop().unwrap(), vec![1, 2, 3, 4]);
        assert!(j.pop().is_none()); // underrun
        assert_eq!(j.state(), JitterState::Prebuffering);
        assert_eq!(j.stats().underruns_total, 1);
    }

    #[test]
    fn stitches_across_chunk_boundaries() {
        let mut j = JitterBuffer::new(4, 1);
        j.push(&[1, 2]);
        j.push(&[3, 4, 5, 6]);
        assert_eq!(j.pop().unwrap(), vec![1, 2, 3, 4]);
        j.push(&[7, 8]); // total now 5,6,7,8
        assert_eq!(j.pop().unwrap(), vec![5, 6, 7, 8]);
    }

    #[test]
    fn drain_partial_zero_pads() {
        let mut j = JitterBuffer::new(4, 1);
        j.push(&[1, 2, 3, 4, 5]);
        j.pop().unwrap();
        assert_eq!(j.drain_partial().unwrap(), vec![5, 0, 0, 0]);
        assert!(j.drain_partial().is_none());
    }

    #[test]
    fn flush_resets() {
        let mut j = JitterBuffer::new(4, 1);
        j.push(&[1, 2, 3, 4]);
        j.flush();
        assert_eq!(j.buffered(), 0);
        assert_eq!(j.state(), JitterState::Prebuffering);
    }
}
