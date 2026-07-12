"""Tests for AudioPipeline — the audio-plane runner (docs 11), dependency-free.

Rates are held at 48k end-to-end so the lazy resampler is a proven no-op and bytes stay
exact (rate conversion itself is covered in test_audio_dsp). A guard backend asserts the
resampler is never invoked at equal rates; an identity denoise backend exercises the
clean path.
"""

from __future__ import annotations

import numpy as np

from snail.audio import (
    AudioPipeline,
    AudioSource,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
    PcmCodec,
    RNNoiseCleaner,
)
from snail.router import OutputGate


class NoResampleBackend:
    """Asserts it is never used — all rates are 48k, so resample must be a no-op."""

    def stream(self, from_rate, to_rate):  # pragma: no cover
        raise AssertionError(f"unexpected resample {from_rate}->{to_rate}")


class IdentityDenoise:
    def process_480(self, frame):
        return frame


def _samples(n):
    return (np.arange(n) % 200 - 100).astype(np.int16)


def _pipeline(*, clean=False, jitter_prefill=1):
    pool = FramePool(capacity=64, slab_samples=480)
    bus = FanoutBus(pool)
    return (
        AudioPipeline(
            pool=pool,
            bus=bus,
            resampler=LazyResampler(NoResampleBackend()),
            gate=OutputGate(depth=8),
            jitter=JitterBuffer(frame_size=480, prefill_frames=jitter_prefill),
            cleaner=RNNoiseCleaner(IdentityDenoise()) if clean else None,
            codec=PcmCodec(),
            client_rate=48000,
        ),
        bus,
    )


# --- ingress -------------------------------------------------------------


def test_ingress_raw_fanout_and_drain() -> None:
    pipe, bus = _pipeline()
    bus.subscribe("agent", source=AudioSource.USER_RAW, target_rate=48000)
    x = _samples(960)  # exactly two 480 frames
    pipe.on_client_audio(PcmCodec().encode(x))
    out = pipe.drain()
    assert list(out) == ["agent"]
    joined = b"".join(out["agent"])
    assert np.array_equal(np.frombuffer(joined, dtype=np.int16), x)


def test_ingress_carries_partial_frame() -> None:
    pipe, bus = _pipeline()
    bus.subscribe("agent", source=AudioSource.USER_RAW, target_rate=48000)
    pipe.on_client_audio(PcmCodec().encode(_samples(300)))  # < 480 → buffered
    assert pipe.drain() == {}  # nothing full yet
    pipe.on_client_audio(PcmCodec().encode(_samples(180)))  # 480 total → one frame
    assert len(pipe.drain()["agent"]) == 1


def test_clean_path_runs_only_with_clean_subscriber() -> None:
    pipe, bus = _pipeline(clean=True)
    bus.subscribe("active", source=AudioSource.USER_RAW, target_rate=48000)
    bus.subscribe("listener", source=AudioSource.USER_CLEAN, target_rate=48000)
    pipe.on_client_audio(PcmCodec().encode(_samples(480)))
    out = pipe.drain()
    assert set(out) == {"active", "listener"}  # both raw and clean delivered


def test_no_clean_subscriber_skips_cleaner() -> None:
    pipe, bus = _pipeline(clean=True)
    bus.subscribe("active", source=AudioSource.USER_RAW, target_rate=48000)
    pipe.on_client_audio(PcmCodec().encode(_samples(480)))
    out = pipe.drain()
    assert set(out) == {"active"}  # cleaner present but not exercised


def test_ingress_releases_frames_back_to_pool() -> None:
    pipe, bus = _pipeline()
    pool = FramePool(capacity=4, slab_samples=480)
    bus = FanoutBus(pool)
    pipe = AudioPipeline(
        pool=pool, bus=bus, resampler=LazyResampler(NoResampleBackend()),
        gate=OutputGate(), jitter=JitterBuffer(), client_rate=48000,
    )
    bus.subscribe("agent", source=AudioSource.USER_RAW, target_rate=48000)
    # push + drain many times; if drain didn't release, the 4-slab pool would exhaust.
    for _ in range(50):
        pipe.on_client_audio(PcmCodec().encode(_samples(480)))
        pipe.drain()
    assert pipe.stats["ingress_dropped"] == 0


# --- egress --------------------------------------------------------------


def test_egress_token_holder_hears_audio() -> None:
    pipe, _ = _pipeline(jitter_prefill=1)
    pipe._gate.grant("a")
    pipe.on_vendor_audio(_samples(960).tobytes(), vendor_rate=48000)  # arms jitter
    frame_bytes = pipe.playout("a")
    assert frame_bytes is not None
    assert len(frame_bytes) == 480 * 2  # one 480-sample PCM16 frame


def test_egress_non_holder_suppressed() -> None:
    pipe, _ = _pipeline(jitter_prefill=1)
    pipe._gate.grant("a")
    pipe.on_vendor_audio(_samples(960).tobytes(), vendor_rate=48000)
    assert pipe.playout("b") is None  # not the token holder → dropped


def test_egress_none_when_prebuffering() -> None:
    pipe, _ = _pipeline(jitter_prefill=5)
    pipe._gate.grant("a")
    pipe.on_vendor_audio(_samples(480).tobytes(), vendor_rate=48000)  # < prefill
    assert pipe.playout("a") is None


def test_cut_flushes_jitter_and_gate() -> None:
    pipe, _ = _pipeline(jitter_prefill=1)
    pipe._gate.grant("a")
    pipe.on_vendor_audio(_samples(2400).tobytes(), vendor_rate=48000)
    pipe.cut()
    assert pipe.playout("a") is None  # everything dropped
    assert pipe.stats["jitter"]["buffered"] == 0
