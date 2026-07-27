//! The audio plane: interior frames, refcounted pool, fan-out bus, jitter buffer, codec, and
//! lazy resample — ported from `snail.audio`. Everything runs on one loop (docs 06), no locks.

pub mod clean;
pub mod codec;
pub mod fanout;
pub mod frame;
pub mod jitter;
pub mod pipeline;
pub mod pool;
pub mod resample;

pub use clean::{AudioCleaner, DenoiseBackend, NullCleaner, RNNoiseCleaner, Rechunker};
pub use codec::{AudioCodec, PcmCodec};
pub use fanout::{FanoutBus, OverflowPolicy, Subscriber, SubscriberRing};
pub use frame::{AudioFrame, AudioSource, FrameFlags};
pub use jitter::{JitterBuffer, JitterState, FRAME_LEN};
pub use pipeline::{AudioPipeline, PipelineStats, INTERIOR_RATE};
pub use pool::{FramePool, PoolStats};
pub use resample::{LazyResampler, ResampleBackend, Resampler};
