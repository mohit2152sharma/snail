//! AudioPipeline — assembles the audio plane into the two directional runners
//! (port of `snail.audio.pipeline`).
//!
//! **Ingress (client → vendors):** [`AudioPipeline::on_client_audio`] decodes client bytes,
//! resamples to the 48k interior, rechunks to 480-sample frames, publishes each as `USER_RAW`
//! (and `USER_CLEAN` via the cleaner when a subscriber wants it). [`AudioPipeline::drain`] pulls
//! each subscriber's ring, lazily resamples 48k → that subscriber's vendor rate, and hands back
//! vendor-ready PCM per subscriber.
//!
//! **Egress (active vendor → client):** [`AudioPipeline::on_vendor_audio`] decodes/upsamples the
//! vendor output to 48k into the jitter buffer; [`AudioPipeline::playout`] is the paced drain:
//! one jittered frame → the [`OutputGate`] token check → codec-encoded client bytes.
//!
//! The pool is used on ingress only (fan-out to N consumers → refcounted slabs); egress is a
//! single active stream and stays plain-vector.

use crate::router::OutputGate;

use super::clean::AudioCleaner;
use super::codec::AudioCodec;
use super::fanout::{FanoutBus, OverflowPolicy};
use super::frame::{AudioFrame, AudioSource, FrameFlags};
use super::jitter::JitterBuffer;
use super::pool::FramePool;
use super::resample::LazyResampler;

/// The canonical interior sample rate (docs 11).
pub const INTERIOR_RATE: u32 = 48000;
const FRAME_LEN: usize = 480;

#[inline]
fn to_le_bytes(samples: &[i16]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for &s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

#[inline]
fn from_le_bytes(data: &[u8]) -> Vec<i16> {
    data.chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect()
}

pub struct PipelineStats {
    pub ingress_dropped: u64,
    pub jitter_underruns: u64,
    pub gate_suppressed: u64,
    pub resample_pairs: usize,
}

pub struct AudioPipeline {
    pool: FramePool,
    bus: FanoutBus,
    resampler: LazyResampler,
    gate: OutputGate,
    jitter: JitterBuffer,
    cleaner: Option<Box<dyn AudioCleaner>>,
    codec: Box<dyn AudioCodec>,
    client_rate: u32,
    frame: usize,
    raw_carry: Vec<i16>,
    in_seq: u64,
    dropped: u64,
}

impl AudioPipeline {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        pool: FramePool,
        bus: FanoutBus,
        resampler: LazyResampler,
        gate: OutputGate,
        jitter: JitterBuffer,
        cleaner: Option<Box<dyn AudioCleaner>>,
        codec: Box<dyn AudioCodec>,
        client_rate: u32,
    ) -> Self {
        Self {
            pool,
            bus,
            resampler,
            gate,
            jitter,
            cleaner,
            codec,
            client_rate,
            frame: FRAME_LEN,
            raw_carry: Vec::new(),
            in_seq: 0,
            dropped: 0,
        }
    }

    // --- consumer (GATE 1) management ------------------------------------

    pub fn attach_consumer(
        &mut self,
        consumer_id: &str,
        source: AudioSource,
        target_rate: u32,
        depth: usize,
    ) {
        self.bus.subscribe(
            consumer_id,
            source,
            target_rate,
            depth,
            OverflowPolicy::DropOldest,
        );
    }

    pub fn detach_consumer(&mut self, consumer_id: &str) -> usize {
        self.bus.unsubscribe(&mut self.pool, consumer_id)
    }

    /// Give the output token to `agent_id` — only its audio reaches the user (GATE 2).
    pub fn hold_token(&mut self, agent_id: &str) {
        self.gate.transfer(agent_id);
    }

    // --- ingress: client → interior → fan-out ----------------------------

    /// Decode one client media frame and publish it to the fan-out bus. Returns the RAW 48k
    /// frames published this call (so the caller can feed them to endpointing without re-decode).
    pub fn on_client_audio(&mut self, data: &[u8]) -> Vec<Vec<i16>> {
        let samples = self.codec.decode(data);
        let at48 = self
            .resampler
            .resample(&samples, self.client_rate, INTERIOR_RATE);
        let frames = self.rechunk_raw(&at48);
        for frame480 in &frames {
            self.publish(frame480, AudioSource::UserRaw);
        }
        if self.wants_clean() {
            if let Some(cleaner) = self.cleaner.as_mut() {
                let cleaned = cleaner.process(&at48);
                for c in &cleaned {
                    self.publish(c, AudioSource::UserClean);
                }
            }
        }
        frames
    }

    /// Pull every subscriber's ring → vendor-ready PCM bytes, per subscriber id. Each frame is
    /// lazily resampled 48k → the subscriber's target rate (no-op at 48k), then released.
    pub fn drain(&mut self) -> Vec<(String, Vec<Vec<u8>>)> {
        let mut out = Vec::new();
        for sub in self.bus.subscribers_mut() {
            let mut chunks: Vec<Vec<u8>> = Vec::new();
            while let Some(mut frame) = sub.ring.pop() {
                // Copy out of the slab (frame stays live until release below).
                let samples: Vec<i16> = self.pool.samples(&frame).to_vec();
                if sub.target_rate == INTERIOR_RATE {
                    chunks.push(to_le_bytes(&samples));
                } else {
                    let resampled =
                        self.resampler
                            .resample(&samples, INTERIOR_RATE, sub.target_rate);
                    chunks.push(to_le_bytes(&resampled));
                }
                self.pool.release(&mut frame);
            }
            if !chunks.is_empty() {
                out.push((sub.id.clone(), chunks));
            }
        }
        out
    }

    // --- egress: vendor → jitter → gate → client -------------------------

    /// Push one vendor output burst (PCM16 mono LE) into the jitter buffer at 48k.
    pub fn on_vendor_audio(&mut self, pcm: &[u8], vendor_rate: u32) {
        let samples = from_le_bytes(pcm);
        let at48 = self
            .resampler
            .resample(&samples, vendor_rate, INTERIOR_RATE);
        self.jitter.push(&at48);
    }

    /// Paced drain → client bytes, or `None` if no frame is due. One jittered 48k frame passes
    /// the [`OutputGate`] token check (suppressed if `agent_id` isn't the holder) and is
    /// codec-encoded for the client leg.
    pub fn playout(&mut self, agent_id: &str) -> Option<Vec<u8>> {
        let frame = self.jitter.pop()?;
        if !self.gate.write(agent_id, frame) {
            return None;
        }
        let out = self.gate.pop()?;
        Some(self.codec.encode(&out))
    }

    /// Barge-in / CUT_NOW on the output path: flush jitter + gate rings.
    pub fn cut(&mut self) {
        self.jitter.flush();
        self.gate.flush();
    }

    pub fn stats(&self) -> PipelineStats {
        PipelineStats {
            ingress_dropped: self.dropped,
            jitter_underruns: self.jitter.stats().underruns_total,
            gate_suppressed: self.gate.stats().suppressed_total,
            resample_pairs: self.resampler.rate_pairs().len(),
        }
    }

    // --- internals --------------------------------------------------------

    fn wants_clean(&self) -> bool {
        self.bus
            .subscribers()
            .iter()
            .any(|s| s.source == AudioSource::UserClean)
    }

    /// Align the RAW 48k stream to fixed 480-sample frames, carrying a remainder across calls.
    fn rechunk_raw(&mut self, at48: &[i16]) -> Vec<Vec<i16>> {
        let mut buf: Vec<i16> = Vec::with_capacity(self.raw_carry.len() + at48.len());
        buf.extend_from_slice(&self.raw_carry);
        buf.extend_from_slice(at48);
        let n_full = buf.len() / self.frame;
        let mut frames = Vec::with_capacity(n_full);
        for i in 0..n_full {
            frames.push(buf[i * self.frame..(i + 1) * self.frame].to_vec());
        }
        self.raw_carry = buf[n_full * self.frame..].to_vec();
        frames
    }

    /// Copy a 48k frame into a pooled slab and fan it out (drop on exhaustion).
    fn publish(&mut self, samples: &[i16], source: AudioSource) {
        match self.acquire(samples.len(), source) {
            Some(frame) => {
                self.pool.samples_mut(&frame).copy_from_slice(samples);
                self.bus.publish(&mut self.pool, frame);
            }
            None => self.dropped += 1, // discontinuity — never crash ingress (docs 11)
        }
    }

    /// Acquire a pooled frame; on exhaustion drop the globally-oldest and retry once.
    fn acquire(&mut self, n: usize, source: AudioSource) -> Option<AudioFrame> {
        self.in_seq += 1;
        if let Some(f) =
            self.pool
                .try_acquire(n, INTERIOR_RATE, source, self.in_seq, 0, FrameFlags::NONE)
        {
            return Some(f);
        }
        if self.bus.reclaim_oldest(&mut self.pool) {
            return self.pool.try_acquire(
                n,
                INTERIOR_RATE,
                source,
                self.in_seq,
                0,
                FrameFlags::NONE,
            );
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::codec::PcmCodec;
    use crate::audio::resample::{ResampleBackend, Resampler};

    struct IdentityBackend;
    struct IdentityStream;
    impl Resampler for IdentityStream {
        fn process(&mut self, samples: &[i16]) -> Vec<i16> {
            samples.to_vec()
        }
    }
    impl ResampleBackend for IdentityBackend {
        fn stream(&self, _f: u32, _t: u32) -> Box<dyn Resampler> {
            Box::new(IdentityStream)
        }
    }

    fn pipeline(client_rate: u32) -> AudioPipeline {
        AudioPipeline::new(
            FramePool::new(64, FRAME_LEN),
            FanoutBus::new(),
            LazyResampler::new(Box::new(IdentityBackend)),
            OutputGate::new(32),
            JitterBuffer::new(FRAME_LEN, 1),
            None,
            Box::new(PcmCodec),
            client_rate,
        )
    }

    #[test]
    fn ingress_publishes_full_480_frames_and_drains_per_consumer() {
        let mut p = pipeline(INTERIOR_RATE); // client already at 48k → no resample
        p.attach_consumer("agent", AudioSource::UserRaw, INTERIOR_RATE, 8);
        p.hold_token("agent");
        // one 10ms client frame = 480 samples = 960 bytes
        let bytes = to_le_bytes(&vec![100i16; FRAME_LEN]);
        let raw = p.on_client_audio(&bytes);
        assert_eq!(raw.len(), 1);
        assert_eq!(raw[0].len(), FRAME_LEN);
        let drained = p.drain();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].0, "agent");
        assert_eq!(drained[0].1[0], bytes); // 48k target → byte-identical passthrough
                                            // ring drained → slabs all released back
        assert_eq!(p.pool.available(), p.pool.capacity());
    }

    #[test]
    fn ingress_carries_partial_frames_across_calls() {
        let mut p = pipeline(INTERIOR_RATE);
        p.attach_consumer("a", AudioSource::UserRaw, INTERIOR_RATE, 8);
        // 300 samples < 480 → nothing published yet, carried
        assert!(p.on_client_audio(&to_le_bytes(&vec![1i16; 300])).is_empty());
        // +300 = 600 → one full 480 frame
        let raw = p.on_client_audio(&to_le_bytes(&vec![2i16; 300]));
        assert_eq!(raw.len(), 1);
    }

    #[test]
    fn egress_gate_only_passes_token_holder() {
        let mut p = pipeline(INTERIOR_RATE);
        p.hold_token("agent");
        let burst = to_le_bytes(&vec![7i16; FRAME_LEN]); // ≥ prefill (1 frame)
        p.on_vendor_audio(&burst, INTERIOR_RATE);
        // holder gets bytes
        let out = p.playout("agent");
        assert!(out.is_some());
        // re-push, non-holder suppressed
        p.on_vendor_audio(&burst, INTERIOR_RATE);
        assert!(p.playout("intruder").is_none());
    }

    #[test]
    fn cut_flushes_egress() {
        let mut p = pipeline(INTERIOR_RATE);
        p.hold_token("agent");
        p.on_vendor_audio(&to_le_bytes(&vec![7i16; FRAME_LEN * 3]), INTERIOR_RATE);
        p.cut();
        assert!(p.playout("agent").is_none()); // jitter + gate flushed
    }
}
