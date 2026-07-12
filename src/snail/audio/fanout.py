"""Fan-out bus + per-subscriber rings (see docs 11-audio, GATE 1).

The input plane: one producer publishes user-audio frames; N bounded subscriber rings
each buffer them for one consumer (the active agent + chosen listeners). This is
GATE 1 — the Router attaches/detaches subscribers, and each subscription picks its
**source** (raw vs clean) and **target rate** (for the consumer's lazy resample).

Slab ownership (the FramePool refcount protocol, docs 11):

* the producer ``acquire``\\ s a frame (refcount 1);
* :meth:`FanoutBus.publish` ``incref``\\ s once per matching subscriber ring, then
  ``release``\\ s the producer's own ref → net refcount = number of delivered rings;
* a ring holds one ref per buffered frame; **drop-oldest eviction releases** the
  evicted frame;
* :meth:`SubscriberRing.pop` **transfers ownership to the caller** (no incref) — the
  consumer's drain task releases after it has resampled/sent the frame;
* **detach-release:** :meth:`FanoutBus.unsubscribe` / :meth:`close` drain the ring and
  release every slab still in it (else a dropped consumer's buffered frames leak).

Single loop per worker (docs 06) → no locks.
"""

from __future__ import annotations

import enum
from collections import deque

from .frame import AudioFrame, AudioSource
from .pool import FramePool


class OverflowPolicy(enum.Enum):
    """What a full ring does with a newly-pushed frame (docs 11).

    ``DROP_OLDEST`` (default) evicts the stalest buffered frame to make room — newest
    audio matters most for realtime. ``DROP_NEWEST`` refuses the incoming frame and
    keeps what's buffered (for a consumer that would rather have a contiguous older
    run than the latest chunk).
    """

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"


class SubscriberRing:
    """A bounded ring of frame handles for one consumer, with an overflow policy."""

    __slots__ = ("_pool", "_depth", "_buf", "_drops", "_policy")

    def __init__(
        self,
        pool: FramePool,
        depth: int,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        if depth < 1:
            raise ValueError("ring depth must be >= 1")
        self._pool = pool
        self._depth = depth
        self._policy = policy
        self._buf: deque[AudioFrame] = deque()
        self._drops = 0

    def push(self, frame: AudioFrame) -> bool:
        """Buffer a frame per the overflow policy. Returns whether it was stored.

        Full + ``DROP_OLDEST`` → evict+release the oldest, store the new one (True).
        Full + ``DROP_NEWEST`` → refuse the new one, keep the buffer (False); the
        caller never transferred a ref, so nothing leaks.
        """
        if len(self._buf) >= self._depth:
            if self._policy is OverflowPolicy.DROP_NEWEST:
                self._drops += 1
                return False
            # DROP_OLDEST: evict the head, then take the new frame.
            self._pool.release(self._buf.popleft())
            self._drops += 1
        self._pool.incref(frame)
        self._buf.append(frame)
        return True

    def pop(self) -> AudioFrame | None:
        """Take the oldest buffered frame. **Ownership transfers to the caller** —
        the caller must ``pool.release`` it after use. ``None`` if empty.
        """
        if not self._buf:
            return None
        return self._buf.popleft()

    def peek_oldest_seq(self) -> int | None:
        """Seq of the oldest buffered frame (for cross-ring age comparison)."""
        return self._buf[0].seq if self._buf else None

    def drop_oldest(self) -> bool:
        """Evict + release the oldest buffered frame. Returns False if empty."""
        if not self._buf:
            return False
        self._pool.release(self._buf.popleft())
        self._drops += 1
        return True

    def release_all(self) -> int:
        """Release every buffered frame (detach/close). Returns the count released."""
        n = 0
        while self._buf:
            self._pool.release(self._buf.popleft())
            n += 1
        return n

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def drops(self) -> int:
        """Frames dropped so far (backpressure signal, docs 11/09§E)."""
        return self._drops


class Subscriber:
    """One GATE-1 subscription: id + chosen source + target rate + its ring."""

    __slots__ = ("id", "source", "target_rate", "ring")

    def __init__(
        self, id: str, source: AudioSource, target_rate: int, ring: SubscriberRing
    ) -> None:
        self.id = id
        #: which producer feeds this subscriber (USER_RAW vs USER_CLEAN).
        self.source = source
        #: vendor rate (Hz); the consumer's leg resamples only if it differs from 48k.
        self.target_rate = target_rate
        self.ring = ring


class FanoutBus:
    """Distributes user-audio frames to matching subscriber rings (GATE 1)."""

    __slots__ = ("_pool", "_subs")

    def __init__(self, pool: FramePool) -> None:
        self._pool = pool
        self._subs: dict[str, Subscriber] = {}

    def subscribe(
        self,
        sub_id: str,
        *,
        source: AudioSource,
        target_rate: int,
        depth: int = 8,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> Subscriber:
        """Attach a consumer. ``source`` = USER_RAW or USER_CLEAN (docs 11).

        ``overflow`` picks this ring's full-ring behavior (default drop-oldest).
        """
        if sub_id in self._subs:
            raise ValueError(f"subscriber {sub_id!r} already attached")
        if source not in (AudioSource.USER_RAW, AudioSource.USER_CLEAN):
            raise ValueError("fan-out source must be USER_RAW or USER_CLEAN")
        ring = SubscriberRing(self._pool, depth, overflow)
        sub = Subscriber(sub_id, source, target_rate, ring)
        self._subs[sub_id] = sub
        return sub

    def unsubscribe(self, sub_id: str) -> int:
        """Detach a consumer and **release its buffered slabs** (detach-release rule).

        Returns the number of frames released. No-op (0) if not attached.
        """
        sub = self._subs.pop(sub_id, None)
        if sub is None:
            return 0
        return sub.ring.release_all()

    def publish(self, frame: AudioFrame) -> int:
        """Deliver ``frame`` to every subscriber whose source matches, then release the
        producer's own ref. Returns how many rings received it.

        A frame with no matching subscriber is simply freed (producer ref released →
        refcount 0). ``frame`` must be a USER_RAW/USER_CLEAN frame the producer owns.
        """
        delivered = 0
        for sub in self._subs.values():
            if sub.source == frame.source and sub.ring.push(frame):
                delivered += 1
        self._pool.release(frame)  # producer's own ref
        return delivered

    def reclaim_oldest(self) -> bool:
        """Free one slab by dropping the **globally-oldest** buffered frame.

        The default recovery a caller uses on :class:`FramePoolExhausted`: rather than
        drop the newest mic chunk, evict the stalest frame across all rings (found by
        ``seq``) — freeing its slab so ingress can retry ``acquire``. Returns ``False``
        if nothing is buffered anywhere (then the caller must drop-newest).

        A shared frame lives in several rings (one slab, many refs). Because ``seq`` is
        monotonic and rings are FIFO, the global-oldest seq is at the **head** of every
        ring that still holds it — so dropping that head from **all** of them releases
        the last ref and actually frees the slab (not just one ref).
        """
        best: int | None = None
        for sub in self._subs.values():
            s = sub.ring.peek_oldest_seq()
            if s is not None and (best is None or s < best):
                best = s
        if best is None:
            return False
        for sub in self._subs.values():
            if sub.ring.peek_oldest_seq() == best:
                sub.ring.drop_oldest()
        return True

    def close(self) -> int:
        """Detach all subscribers, releasing every buffered slab. Returns the total."""
        n = 0
        for sub in self._subs.values():
            n += sub.ring.release_all()
        self._subs.clear()
        return n

    def get(self, sub_id: str) -> Subscriber | None:
        return self._subs.get(sub_id)

    @property
    def subscribers(self) -> tuple[Subscriber, ...]:
        return tuple(self._subs.values())
