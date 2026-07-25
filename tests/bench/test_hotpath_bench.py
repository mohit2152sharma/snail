"""Hot-path micro-benchmark + N-session load: does TTFB stay flat as sessions grow?

- micro: per-frame cost of the ingress→drain path (ns/frame regression guard).
- load: N concurrent in-process sessions each pumping the pipeline at a 20ms cadence, a
  LoopLagProbe measuring loop saturation. Success = loop p99 stays bounded as N grows —
  the flat-under-load criterion (a saturated loop inflates every session's TTFB).
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from snail.audio import (
    AudioPipeline,
    AudioSource,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
    PcmCodec,
)
from snail.audio.soxr_backend import SoxrResampleBackend
from snail.router import OutputGate
from snail.util.looplag import LoopLagProbe


def _pipeline(target_rate: int):
    pool = FramePool(capacity=64, slab_samples=480)
    p = AudioPipeline(
        pool=pool,
        bus=FanoutBus(pool),
        resampler=LazyResampler(SoxrResampleBackend()),
        gate=OutputGate(depth=64),
        jitter=JitterBuffer(prefill_frames=1),
        codec=PcmCodec(),
        client_rate=48000,
    )
    p.attach_consumer("a", source=AudioSource.USER_RAW, target_rate=target_rate, depth=64)
    return p


@pytest.mark.bench
def test_ingress_drain_per_frame_cost(capsys):
    p = _pipeline(target_rate=16000)
    pcm = np.random.randint(-1000, 1000, 480, dtype=np.int16).tobytes()
    N = 3000
    t0 = time.perf_counter()
    for _ in range(N):
        p.on_client_audio(pcm)
        p.drain()
    per_frame_us = (time.perf_counter() - t0) / N * 1e6
    with capsys.disabled():
        print(f"\n  ingress→drain: {per_frame_us:.1f} us/frame (10ms of audio)")
    assert per_frame_us < 500.0  # generous guard; a 10ms frame must cost << 10ms


@pytest.mark.bench
@pytest.mark.parametrize("n_sessions", [1, 10, 50])
async def test_loop_lag_bounded_under_n_sessions(n_sessions, capsys):
    probe = LoopLagProbe(interval_s=0.010)
    probe.start()

    pcm = np.random.randint(-1000, 1000, 480, dtype=np.int16).tobytes()

    async def session():
        p = _pipeline(target_rate=16000)
        # ~1s of 20ms ticks, two 10ms frames per tick
        for _ in range(50):
            p.on_client_audio(pcm)
            p.on_client_audio(pcm)
            p.drain()
            await asyncio.sleep(0.02)

    await asyncio.gather(*(session() for _ in range(n_sessions)))
    await probe.stop()
    stats = probe.stats
    with capsys.disabled():
        print(f"\n  N={n_sessions:>2}: loop p50={stats['p50_ms']:.1f}ms "
              f"p99={stats['p99_ms']:.1f}ms max={stats['max_ms']:.1f}ms")
    assert stats["p99_ms"] < 25.0  # bounded → TTFB stays flat under load
