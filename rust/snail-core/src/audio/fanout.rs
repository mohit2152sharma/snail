//! Fan-out bus + per-subscriber rings (port of `snail.audio.fanout`, GATE 1).
//!
//! One producer publishes user-audio frames; N bounded subscriber rings each buffer them
//! for one consumer. The [`crate::audio::pool::FramePool`] refcount protocol is honoured:
//! `publish` increfs once per delivered ring then releases the producer's own ref;
//! drop-oldest eviction releases the evicted frame; `pop` transfers ownership to the caller;
//! detach/close drain rings and release every buffered slab.
//!
//! The pool is threaded through each method (single owner in the pipeline) rather than held
//! by reference, so there is no aliasing between rings and the pool.

use std::collections::VecDeque;

use super::frame::{AudioFrame, AudioSource};
use super::pool::FramePool;

/// What a full ring does with a newly-pushed frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OverflowPolicy {
    /// evict the stalest buffered frame — newest audio matters most for realtime.
    DropOldest,
    /// refuse the incoming frame, keep what's buffered.
    DropNewest,
}

pub struct SubscriberRing {
    depth: usize,
    policy: OverflowPolicy,
    buf: VecDeque<AudioFrame>,
    drops: u64,
}

impl SubscriberRing {
    pub fn new(depth: usize, policy: OverflowPolicy) -> Self {
        assert!(depth >= 1, "ring depth must be >= 1");
        Self {
            depth,
            policy,
            buf: VecDeque::new(),
            drops: 0,
        }
    }

    /// Buffer a frame per the overflow policy. Returns whether it was stored.
    pub fn push(&mut self, pool: &mut FramePool, frame: AudioFrame) -> bool {
        if self.buf.len() >= self.depth {
            if self.policy == OverflowPolicy::DropNewest {
                self.drops += 1;
                return false;
            }
            let mut old = self.buf.pop_front().unwrap();
            pool.release(&mut old);
            self.drops += 1;
        }
        pool.incref(&frame, 1);
        self.buf.push_back(frame);
        true
    }

    /// Take the oldest buffered frame. Ownership transfers to the caller (must release).
    pub fn pop(&mut self) -> Option<AudioFrame> {
        self.buf.pop_front()
    }

    pub fn peek_oldest_seq(&self) -> Option<u64> {
        self.buf.front().map(|f| f.seq)
    }

    pub fn drop_oldest(&mut self, pool: &mut FramePool) -> bool {
        match self.buf.pop_front() {
            Some(mut f) => {
                pool.release(&mut f);
                self.drops += 1;
                true
            }
            None => false,
        }
    }

    pub fn release_all(&mut self, pool: &mut FramePool) -> usize {
        let mut n = 0;
        while let Some(mut f) = self.buf.pop_front() {
            pool.release(&mut f);
            n += 1;
        }
        n
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }
    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }
    pub fn drops(&self) -> u64 {
        self.drops
    }
}

/// One GATE-1 subscription: id + chosen source + target rate + its ring.
pub struct Subscriber {
    pub id: String,
    pub source: AudioSource,
    pub target_rate: u32,
    pub ring: SubscriberRing,
}

/// Distributes user-audio frames to matching subscriber rings (GATE 1).
///
/// Insertion order is preserved (Vec) so publish/drain iteration is deterministic, matching
/// the Python dict-ordered behaviour.
pub struct FanoutBus {
    subs: Vec<Subscriber>,
}

impl Default for FanoutBus {
    fn default() -> Self {
        Self::new()
    }
}

impl FanoutBus {
    pub fn new() -> Self {
        Self { subs: Vec::new() }
    }

    pub fn subscribe(
        &mut self,
        sub_id: &str,
        source: AudioSource,
        target_rate: u32,
        depth: usize,
        overflow: OverflowPolicy,
    ) {
        assert!(
            self.subs.iter().all(|s| s.id != sub_id),
            "subscriber {sub_id:?} already attached"
        );
        assert!(
            matches!(source, AudioSource::UserRaw | AudioSource::UserClean),
            "fan-out source must be USER_RAW or USER_CLEAN"
        );
        self.subs.push(Subscriber {
            id: sub_id.to_string(),
            source,
            target_rate,
            ring: SubscriberRing::new(depth, overflow),
        });
    }

    /// Detach a consumer and release its buffered slabs. Returns frames released.
    pub fn unsubscribe(&mut self, pool: &mut FramePool, sub_id: &str) -> usize {
        if let Some(pos) = self.subs.iter().position(|s| s.id == sub_id) {
            let mut sub = self.subs.remove(pos);
            sub.ring.release_all(pool)
        } else {
            0
        }
    }

    /// Deliver `frame` to every matching subscriber, then release the producer's own ref.
    /// Returns how many rings received it.
    pub fn publish(&mut self, pool: &mut FramePool, mut frame: AudioFrame) -> usize {
        let mut delivered = 0;
        for sub in self.subs.iter_mut() {
            if sub.source == frame.source && sub.ring.push(pool, frame) {
                delivered += 1;
            }
        }
        pool.release(&mut frame); // producer's own ref
        delivered
    }

    /// Free one slab by dropping the globally-oldest buffered frame (across all rings).
    /// Returns false if nothing is buffered anywhere.
    pub fn reclaim_oldest(&mut self, pool: &mut FramePool) -> bool {
        let mut best: Option<u64> = None;
        for sub in &self.subs {
            if let Some(s) = sub.ring.peek_oldest_seq() {
                if best.map_or(true, |b| s < b) {
                    best = Some(s);
                }
            }
        }
        let best = match best {
            Some(b) => b,
            None => return false,
        };
        for sub in self.subs.iter_mut() {
            if sub.ring.peek_oldest_seq() == Some(best) {
                sub.ring.drop_oldest(pool);
            }
        }
        true
    }

    pub fn close(&mut self, pool: &mut FramePool) -> usize {
        let mut n = 0;
        for sub in self.subs.iter_mut() {
            n += sub.ring.release_all(pool);
        }
        self.subs.clear();
        n
    }

    pub fn get(&self, sub_id: &str) -> Option<&Subscriber> {
        self.subs.iter().find(|s| s.id == sub_id)
    }
    pub fn get_mut(&mut self, sub_id: &str) -> Option<&mut Subscriber> {
        self.subs.iter_mut().find(|s| s.id == sub_id)
    }

    pub fn subscribers(&self) -> &[Subscriber] {
        &self.subs
    }
    pub fn subscribers_mut(&mut self) -> &mut [Subscriber] {
        &mut self.subs
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::frame::FrameFlags;

    fn publish_raw(pool: &mut FramePool, bus: &mut FanoutBus, seq: u64) -> usize {
        let f = pool.acquire(480, 48000, AudioSource::UserRaw, seq, 0, FrameFlags::NONE);
        bus.publish(pool, f)
    }

    #[test]
    fn publish_delivers_to_matching_source_only() {
        let mut pool = FramePool::new(16, 480);
        let mut bus = FanoutBus::new();
        bus.subscribe(
            "a",
            AudioSource::UserRaw,
            16000,
            8,
            OverflowPolicy::DropOldest,
        );
        bus.subscribe(
            "b",
            AudioSource::UserClean,
            16000,
            8,
            OverflowPolicy::DropOldest,
        );
        assert_eq!(publish_raw(&mut pool, &mut bus, 1), 1); // only "a" matches raw
        assert_eq!(bus.get("a").unwrap().ring.len(), 1);
        assert_eq!(bus.get("b").unwrap().ring.len(), 0);
    }

    #[test]
    fn no_match_frees_the_slab() {
        let mut pool = FramePool::new(1, 480);
        let mut bus = FanoutBus::new();
        assert_eq!(publish_raw(&mut pool, &mut bus, 1), 0);
        assert_eq!(pool.available(), 1); // producer ref released → slab back
    }

    #[test]
    fn drop_oldest_evicts_and_releases() {
        let mut pool = FramePool::new(16, 480);
        let mut bus = FanoutBus::new();
        bus.subscribe(
            "a",
            AudioSource::UserRaw,
            16000,
            2,
            OverflowPolicy::DropOldest,
        );
        for seq in 1..=3 {
            publish_raw(&mut pool, &mut bus, seq);
        }
        let ring = &bus.get("a").unwrap().ring;
        assert_eq!(ring.len(), 2);
        assert_eq!(ring.peek_oldest_seq(), Some(2)); // seq 1 evicted
        assert_eq!(ring.drops(), 1);
    }

    #[test]
    fn reclaim_oldest_frees_shared_slab_across_rings() {
        let mut pool = FramePool::new(16, 480);
        let mut bus = FanoutBus::new();
        bus.subscribe(
            "a",
            AudioSource::UserRaw,
            16000,
            8,
            OverflowPolicy::DropOldest,
        );
        bus.subscribe(
            "b",
            AudioSource::UserRaw,
            16000,
            8,
            OverflowPolicy::DropOldest,
        );
        publish_raw(&mut pool, &mut bus, 1); // one slab, two refs
        let before = pool.available();
        assert!(bus.reclaim_oldest(&mut pool));
        assert_eq!(pool.available(), before + 1); // both heads dropped → slab freed
    }

    #[test]
    fn unsubscribe_releases_buffered() {
        let mut pool = FramePool::new(16, 480);
        let mut bus = FanoutBus::new();
        bus.subscribe(
            "a",
            AudioSource::UserRaw,
            16000,
            8,
            OverflowPolicy::DropOldest,
        );
        publish_raw(&mut pool, &mut bus, 1);
        assert_eq!(bus.unsubscribe(&mut pool, "a"), 1);
        assert_eq!(pool.available(), pool.capacity());
    }
}
