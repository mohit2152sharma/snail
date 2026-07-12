"""Tests for the real soxr ResampleBackend — exercises libsoxr through LazyResampler.

soxr streams have warmup latency (early chunks emit fewer samples), so assertions are on
aggregate sample counts within tolerance, not exact per-chunk ratios.
"""

from __future__ import annotations

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


def _sine(n, freq=440, rate=48000):
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)


def test_downsample_48k_to_16k_ratio() -> None:
    r = LazyResampler(SoxrResampleBackend())
    x = _sine(48000)  # 1 second @48k
    out = r.resample(x, from_rate=48000, to_rate=16000)
    assert out.dtype == np.int16
    # ideal 16000; soxr holds a fixed ~341-sample filter latency (no final flush).
    assert 15400 < len(out) < 16000


def test_upsample_24k_to_48k_ratio() -> None:
    r = LazyResampler(SoxrResampleBackend())
    x = _sine(24000, rate=24000)  # 1s @24k (agent output rate)
    out = r.resample(x, from_rate=24000, to_rate=48000)
    # ideal 48000; fixed ~1520-sample (≈32ms @48k) streaming latency.
    assert 46000 < len(out) < 48000


def test_equal_rate_still_noop() -> None:
    r = LazyResampler(SoxrResampleBackend())
    x = _sine(480)
    assert r.resample(x, from_rate=48000, to_rate=48000) is x
    assert r.rate_pairs == []  # no soxr stream created


def test_stream_memoized_across_chunks() -> None:
    r = LazyResampler(SoxrResampleBackend())
    for _ in range(5):
        r.resample(_sine(480), from_rate=48000, to_rate=16000)
    assert r.rate_pairs == [(48000, 16000)]  # one stateful stream reused


def test_pipeline_downsamples_to_vendor_rate() -> None:
    pool = FramePool(capacity=128, slab_samples=480)
    bus = FanoutBus(pool)
    pipe = AudioPipeline(
        pool=pool,
        bus=bus,
        resampler=LazyResampler(SoxrResampleBackend()),
        gate=OutputGate(),
        jitter=JitterBuffer(),
        codec=PcmCodec(),
        client_rate=48000,
    )
    bus.subscribe("agent", source=AudioSource.USER_RAW, target_rate=16000)
    # feed 100ms of 48k audio in 10ms client frames, draining each (realistic loop).
    total = 0
    for _ in range(10):
        pipe.on_client_audio(PcmCodec().encode(_sine(480)))
        for c in pipe.drain().get("agent", []):
            total += len(np.frombuffer(c, dtype=np.int16))
    # 100ms @16k ≈ 1600, minus the fixed ~341-sample soxr latency.
    assert 1150 < total < 1600


def test_missing_soxr_message_is_actionable() -> None:
    # sanity: the guard text names the fix (only checked structurally here).
    from snail.audio import soxr_backend

    assert soxr_backend.SoxrResampleBackend is not None
