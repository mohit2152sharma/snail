"""Tests for AudioFrame + FramePool ownership protocol (docs 11, 09§E)."""

from __future__ import annotations

import numpy as np
import pytest

from snail.audio import AudioSource, FramePool, FramePoolExhausted


def _acq(pool: FramePool, n: int = 480, seq: int = 0):
    return pool.acquire(n, sample_rate=48000, source=AudioSource.USER_CLEAN, seq=seq)


def test_acquire_gives_view_and_refcount_one() -> None:
    pool = FramePool(capacity=4, slab_samples=480)
    f = _acq(pool)
    assert f.n_samples == 480
    assert f.samples.shape == (480,)
    assert f.samples.dtype == np.int16
    assert pool.in_use == 1
    assert pool.available == 3


def test_samples_is_zero_copy_view() -> None:
    pool = FramePool(capacity=2, slab_samples=8)
    f = _acq(pool, n=8)
    f.samples[:] = 7
    # A fresh acquire of the *other* slab is independent.
    g = _acq(pool, n=8, seq=1)
    g.samples[:] = 3
    assert f.samples[0] == 7
    assert g.samples[0] == 3
    assert not np.shares_memory(f.samples, g.samples)


def test_release_returns_slab_at_zero() -> None:
    pool = FramePool(capacity=1, slab_samples=480)
    f = _acq(pool)
    assert pool.available == 0
    pool.release(f)
    assert pool.available == 1
    assert f.slab_id == -1  # poisoned


def test_fanout_incref_release_lifecycle() -> None:
    pool = FramePool(capacity=2, slab_samples=480)
    f = _acq(pool)
    # fan-out to 3 subscribers: +3 then producer drops its own ref → net 3.
    pool.incref(f, 3)
    slab = f.slab_id
    pool.release(f)  # producer done
    assert pool.available == 1  # still held by 3 subscribers
    # each subscriber releases once
    pool.release(f)
    pool.release(f)
    assert pool.available == 1
    pool.release(f)  # last subscriber
    assert pool.available == 2
    assert f.slab_id == -1
    # stats: one slab fully cycled
    assert pool.stats["acquired_total"] == 1
    assert pool.stats["released_total"] == 1
    _ = slab


def test_drop_path_must_release() -> None:
    # A ring dropping a frame (overflow) is just another release path.
    pool = FramePool(capacity=1, slab_samples=480)
    f = _acq(pool)
    pool.incref(f)  # enqueued into one ring (net 2)
    pool.release(f)  # producer done (net 1)
    assert pool.available == 0
    pool.release(f)  # ring drops it before draining → releases
    assert pool.available == 1


def test_exhaustion_raises() -> None:
    pool = FramePool(capacity=2, slab_samples=480)
    _acq(pool)
    _acq(pool, seq=1)
    with pytest.raises(FramePoolExhausted):
        _acq(pool, seq=2)


def test_try_acquire_returns_none_when_exhausted() -> None:
    # ingress uses try_acquire → drops the newest chunk instead of crashing.
    pool = FramePool(capacity=1, slab_samples=480)
    f = _acq(pool)
    assert (
        pool.try_acquire(480, sample_rate=48000, source=AudioSource.USER_RAW, seq=1)
        is None
    )
    assert pool.stats["exhausted_total"] == 1
    # freeing a slab makes it available again
    pool.release(f)
    assert pool.try_acquire(
        480, sample_rate=48000, source=AudioSource.USER_RAW, seq=2
    ) is not None


def test_recommend_capacity_formula() -> None:
    # 1 active + 4 listeners at depth 8, 2 sinks at depth 16, margin 8
    cap = FramePool.recommend_capacity([8, 8, 8, 8, 8], sink_ring_depths=[16, 16])
    assert cap == 40 + 5 + 32 + 8  # Σdepths + N_consumers + Σsinks + margin


def test_double_release_is_ownership_violation() -> None:
    pool = FramePool(capacity=1, slab_samples=480)
    f = _acq(pool)
    pool.release(f)
    with pytest.raises(RuntimeError, match="already 0|non-pool-backed|released"):
        pool.release(f)


def test_incref_after_free_trips() -> None:
    pool = FramePool(capacity=1, slab_samples=480)
    f = _acq(pool)
    pool.release(f)
    with pytest.raises(RuntimeError):
        pool.incref(f)


def test_acquire_out_of_range_rejected() -> None:
    pool = FramePool(capacity=1, slab_samples=480)
    with pytest.raises(ValueError):
        _acq(pool, n=481)
    with pytest.raises(ValueError):
        _acq(pool, n=0)


def test_slab_reuse_after_full_release() -> None:
    pool = FramePool(capacity=1, slab_samples=4)
    f = _acq(pool, n=4)
    f.samples[:] = 9
    pool.release(f)
    # reacquire the same physical slab; it must be usable and independent of the old
    g = _acq(pool, n=4, seq=1)
    g.samples[:] = 1
    assert list(g.samples) == [1, 1, 1, 1]
    assert pool.in_use == 1
