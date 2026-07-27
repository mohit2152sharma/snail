//! AudioFrame — the interior audio unit (port of `snail.audio.frame`).
//!
//! Deliberately lean: a fixed-field header plus a handle into a [`crate::audio::pool::FramePool`]
//! slab. The samples are **not owned** by the frame — they live in the pool backing buffer
//! and are valid only until the frame's last owner releases it (the pool refcount protocol).
//!
//! Canonical interior invariant (docs 11): pool samples are always PCM int16, mono, 48kHz.
//! Codec / encoding / vendor rates live only at the edges.

/// Provenance of a frame's audio.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum AudioSource {
    /// post-decode, pre-clean
    UserRaw = 0,
    /// post-RNNoise
    UserClean = 1,
    /// from a vendor
    Agent = 2,
}

/// Bitfield flags carried on a frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FrameFlags(pub u8);

impl FrameFlags {
    pub const NONE: FrameFlags = FrameFlags(0);
    /// VAD marked this frame as speech.
    pub const IS_SPEECH: FrameFlags = FrameFlags(1);
    /// last frame of an utterance/turn.
    pub const IS_FINAL: FrameFlags = FrameFlags(2);

    #[inline]
    pub fn contains(self, other: FrameFlags) -> bool {
        self.0 & other.0 == other.0
    }
}

impl std::ops::BitOr for FrameFlags {
    type Output = FrameFlags;
    #[inline]
    fn bitor(self, rhs: FrameFlags) -> FrameFlags {
        FrameFlags(self.0 | rhs.0)
    }
}

/// One chunk of interior audio: a fixed header + a handle into a pool slab.
///
/// `slab_id` is the owning pool slab index; `-1` = not pool-backed / released. Reading the
/// samples goes through the pool (`pool.samples(&frame)`), matching the Python numpy-view model.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioFrame {
    pub sample_rate: u32,
    pub n_samples: usize,
    pub source: AudioSource,
    pub seq: u64,
    /// presentation timestamp, sample-clock
    pub t_start: i64,
    pub flags: FrameFlags,
    /// owning pool slab index; -1 = not pool-backed / released
    pub slab_id: i64,
}
