"""Tests for the fan-out bus + subscriber rings (docs 11, GATE 1)."""

from __future__ import annotations

import pytest

from snail.audio import AudioSource, FanoutBus, FramePool, OverflowPolicy


def _pool(capacity: int = 16, slab: int = 4) -> FramePool:
    return FramePool(capacity=capacity, slab_samples=slab)


def _frame(pool: FramePool, source: AudioSource, seq: int = 0):
    return pool.acquire(4, sample_rate=48000, source=source, seq=seq)


def test_publish_delivers_to_matching_source_only() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    bus.subscribe("b", source=AudioSource.USER_CLEAN, target_rate=24000)

    f = _frame(pool, AudioSource.USER_RAW)
    assert bus.publish(f) == 1  # only the RAW subscriber
    assert len(bus.get("a").ring) == 1
    assert len(bus.get("b").ring) == 0
    assert pool.in_use == 1  # held by a's ring


def test_source_filtering_counts() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    bus.subscribe("b", source=AudioSource.USER_RAW, target_rate=16000)
    bus.subscribe("c", source=AudioSource.USER_CLEAN, target_rate=24000)
    assert bus.publish(_frame(pool, AudioSource.USER_RAW)) == 2
    assert bus.publish(_frame(pool, AudioSource.USER_CLEAN)) == 1


def test_publish_with_no_match_frees_frame() -> None:
    pool = _pool()
    bus = FanoutBus(pool)  # no subscribers
    f = _frame(pool, AudioSource.USER_RAW)
    assert pool.in_use == 1
    assert bus.publish(f) == 0
    assert pool.in_use == 0  # producer ref released → slab freed


def test_pop_transfers_ownership_then_release_frees() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    f = _frame(pool, AudioSource.USER_RAW)
    bus.publish(f)
    ring = bus.get("a").ring

    popped = ring.pop()
    assert popped is not None
    assert len(ring) == 0
    assert pool.in_use == 1  # caller now owns it
    pool.release(popped)
    assert pool.in_use == 0


def test_drop_oldest_releases_and_counts() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000, depth=2)
    for i in range(3):  # slow consumer never pops
        bus.publish(_frame(pool, AudioSource.USER_RAW, seq=i))
    ring = bus.get("a").ring
    assert len(ring) == 2
    assert ring.drops == 1
    assert pool.in_use == 2  # only the 2 buffered slabs remain


def test_unsubscribe_releases_buffered_slabs() -> None:
    # the detach-release rule (docs 11).
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    for i in range(3):
        bus.publish(_frame(pool, AudioSource.USER_RAW, seq=i))
    assert pool.in_use == 3
    assert bus.unsubscribe("a") == 3
    assert pool.in_use == 0
    assert bus.get("a") is None


def test_close_releases_everything() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    bus.subscribe("b", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    for i in range(2):
        bus.publish(_frame(pool, AudioSource.USER_RAW, seq=i))
    assert bus.close() == 4  # 2 frames × 2 rings
    assert pool.in_use == 0
    assert bus.subscribers == ()


def test_continuous_arrival_uses_distinct_slabs() -> None:
    # frame N held by a slow consumer never blocks frame N+1 — different slabs.
    pool = _pool(capacity=16, slab=4)
    bus = FanoutBus(pool)
    bus.subscribe("slow", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    for i in range(5):
        bus.publish(_frame(pool, AudioSource.USER_RAW, seq=i))
    ring = bus.get("slow").ring
    assert len(ring) == 5
    assert pool.in_use == 5  # 5 distinct slabs coexist, bounded by depth


def test_drop_newest_policy_keeps_buffered_run() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe(
        "a",
        source=AudioSource.USER_RAW,
        target_rate=16000,
        depth=2,
        overflow=OverflowPolicy.DROP_NEWEST,
    )
    delivered = [bus.publish(_frame(pool, AudioSource.USER_RAW, seq=i)) for i in range(3)]
    # first two stored (delivered=1 each); third refused (delivered=0), freed.
    assert delivered == [1, 1, 0]
    ring = bus.get("a").ring
    assert len(ring) == 2
    assert [ring.pop().seq for _ in range(2)] == [0, 1]  # kept the OLD run
    assert ring.drops == 1


def test_reclaim_oldest_frees_globally_stalest() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    bus.subscribe("b", source=AudioSource.USER_RAW, target_rate=16000, depth=8)
    # seq 0 → both rings; seq 1 → both rings. Oldest overall is seq 0.
    bus.publish(_frame(pool, AudioSource.USER_RAW, seq=0))
    bus.publish(_frame(pool, AudioSource.USER_RAW, seq=1))
    in_use_before = pool.in_use
    assert bus.reclaim_oldest() is True
    # seq-0 frame was in both rings (shared slab) → freeing it drops in_use by 1.
    assert pool.in_use == in_use_before - 1
    # 'a' now heads at seq 1 (its seq-0 copy dropped)
    assert bus.get("a").ring.peek_oldest_seq() == 1


def test_reclaim_oldest_empty_returns_false() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    assert bus.reclaim_oldest() is False


def test_subscribe_dupe_and_bad_source_rejected() -> None:
    pool = _pool()
    bus = FanoutBus(pool)
    bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    with pytest.raises(ValueError):
        bus.subscribe("a", source=AudioSource.USER_RAW, target_rate=16000)
    with pytest.raises(ValueError):
        bus.subscribe("bad", source=AudioSource.AGENT, target_rate=16000)
