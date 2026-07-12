"""FramePool — refcounted free-list of fixed int16 slabs (see docs 11-audio).

This resolves ``09§E TODO(framepool-ownership)``: with N subscriber rings draining
at different rates, drop-oldest eviction, and sinks that copy-then-release, a slab has
several decrement paths. A missed decrement means a slab is reused while a listener
still reads the view = audio corruption. So ownership is an explicit refcount.

Ownership protocol (the contract every audio-plane consumer MUST follow)
------------------------------------------------------------------------
1. ``acquire()`` returns a frame with **refcount = 1**, owned by the caller (ingress).
2. Handing a frame to another consumer = **exactly one** :meth:`incref` per hand-off.
   Fan-out to N subscriber rings: ``incref(frame, N)`` then the producer ``release``\\ s
   its own ref → net refcount = N (one per ring).
3. Every consumer calls :meth:`release` **exactly once** when done with the view.
   This includes non-obvious paths:
     * a ring dropping a frame (drop-oldest overflow) MUST release it;
     * a sink tap increfs on enqueue and releases after it has **copied out** of the
       view (docs 11: sinks copy then release).
4. The slab returns to the free-list only when refcount hits 0. **Do not read
   ``frame.samples`` after you release it** — the slab may already be reused.

Concurrency: one asyncio loop per worker process (docs 06), so the pool is used from
a single thread and needs no lock. It is deliberately lock-free. Cross-thread sharing
is out of the model; a threaded consumer must copy under its own synchronization.
"""

from __future__ import annotations

import numpy as np

from .frame import AudioFrame, AudioSource, FrameFlags


class FramePoolExhausted(RuntimeError):
    """Raised when :meth:`FramePool.acquire` is called with no free slab."""


class FramePool:
    """Free-list of ``capacity`` preallocated int16 slabs of ``slab_samples`` each.

    ``acquire`` hands out a zero-copy view into a slab; ``release`` returns it. No
    per-chunk malloc, no GC churn — the density win (docs 11).
    """

    __slots__ = (
        "_backing",
        "_refcount",
        "_free",
        "_capacity",
        "_slab_samples",
        "_stat_acquired",
        "_stat_released",
        "_stat_exhausted",
    )

    def __init__(self, capacity: int, slab_samples: int) -> None:
        if capacity <= 0 or slab_samples <= 0:
            raise ValueError("capacity and slab_samples must be positive")
        # One contiguous backing buffer; each row is a slab.
        self._backing: np.ndarray = np.zeros((capacity, slab_samples), dtype=np.int16)
        self._refcount: np.ndarray = np.zeros(capacity, dtype=np.int32)
        self._free: list[int] = list(range(capacity - 1, -1, -1))  # pop() = low index
        self._capacity = capacity
        self._slab_samples = slab_samples
        self._stat_acquired = 0
        self._stat_released = 0
        self._stat_exhausted = 0  # times try_acquire found no free slab (drops)

    def try_acquire(
        self,
        n_samples: int,
        *,
        sample_rate: int,
        source: AudioSource,
        seq: int,
        t_start: int = 0,
        flags: FrameFlags = FrameFlags.NONE,
    ) -> AudioFrame | None:
        """Like :meth:`acquire` but returns ``None`` when the pool is exhausted.

        This is the **ingress** primitive: the mic can't block, so when every slab is
        in use the ingress loop drops the newest chunk (counts it + emits a
        discontinuity marker) instead of crashing. Exhaustion should not occur under a
        correctly sized pool — see :meth:`recommend_capacity` — so a persistent stream
        of ``None`` means the pool is under-sized or a consumer leaked a ref.
        """
        if not (0 < n_samples <= self._slab_samples):
            raise ValueError(
                f"n_samples={n_samples} out of range (1..{self._slab_samples})"
            )
        if not self._free:
            self._stat_exhausted += 1
            return None
        idx = self._free.pop()
        self._refcount[idx] = 1
        self._stat_acquired += 1
        return AudioFrame(
            samples=self._backing[idx, :n_samples],
            sample_rate=sample_rate,
            n_samples=n_samples,
            source=source,
            seq=seq,
            t_start=t_start,
            flags=flags,
            slab_id=idx,
        )

    def acquire(
        self,
        n_samples: int,
        *,
        sample_rate: int,
        source: AudioSource,
        seq: int,
        t_start: int = 0,
        flags: FrameFlags = FrameFlags.NONE,
    ) -> AudioFrame:
        """Take a slab and return an :class:`AudioFrame` with refcount 1.

        Raises :class:`FramePoolExhausted` if no slab is free (a tripwire for callers
        that treat exhaustion as a bug), :class:`ValueError` if ``n_samples`` exceeds
        the slab size. Ingress should use :meth:`try_acquire` and drop gracefully.
        """
        frame = self.try_acquire(
            n_samples,
            sample_rate=sample_rate,
            source=source,
            seq=seq,
            t_start=t_start,
            flags=flags,
        )
        if frame is None:
            raise FramePoolExhausted(f"all {self._capacity} slabs in use")
        return frame

    @staticmethod
    def recommend_capacity(
        ring_depths: list[int],
        *,
        sink_ring_depths: list[int] | None = None,
        margin: int = 8,
    ) -> int:
        """Suggested ``capacity`` so :meth:`acquire` never fails in steady state.

        ``Σ ring_depths + one-in-processing per consumer + Σ sink depths + margin``.
        """
        n_consumers = len(ring_depths)
        sinks = sum(sink_ring_depths) if sink_ring_depths else 0
        return sum(ring_depths) + n_consumers + sinks + margin

    def incref(self, frame: AudioFrame, n: int = 1) -> None:
        """Add ``n`` owners to ``frame``'s slab (one per additional hand-off)."""
        if n < 1:
            raise ValueError("incref n must be >= 1")
        idx = frame.slab_id
        self._check_live(idx)
        self._refcount[idx] += n

    def release(self, frame: AudioFrame) -> None:
        """Drop one owner. At refcount 0 the slab returns to the free-list.

        After the call that drops the last ref, ``frame.slab_id`` is set to -1 to trip
        an assertion on any accidental reuse. Raises on an over-release (a decrement
        below zero = a broken ownership contract, i.e. the corruption this guards).
        """
        idx = frame.slab_id
        self._check_live(idx)
        rc = int(self._refcount[idx]) - 1
        self._refcount[idx] = rc
        if rc == 0:
            self._free.append(idx)
            self._stat_released += 1
            frame.slab_id = -1  # poison: further incref/release on this frame will trip

    def _check_live(self, idx: int) -> None:
        if idx < 0 or idx >= self._capacity:
            raise RuntimeError(
                "operation on a non-pool-backed or already-released frame"
            )
        if self._refcount[idx] <= 0:
            raise RuntimeError(
                f"ownership violation: slab {idx} refcount already 0 (double release "
                "or use-after-free)"
            )

    # --- introspection (cheap; for tests, admission, observability) ---
    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def slab_samples(self) -> int:
        return self._slab_samples

    @property
    def available(self) -> int:
        """Slabs currently free."""
        return len(self._free)

    @property
    def in_use(self) -> int:
        """Slabs currently held by at least one owner."""
        return self._capacity - len(self._free)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "capacity": self._capacity,
            "available": self.available,
            "in_use": self.in_use,
            "acquired_total": self._stat_acquired,
            "released_total": self._stat_released,
            "exhausted_total": self._stat_exhausted,
        }
