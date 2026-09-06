"""on_client_audio returns the RAW 48k frames it published (the bridge's VAD tap)."""

from __future__ import annotations

import numpy as np

from snail.audio import (
    AudioPipeline,
    FanoutBus,
    FramePool,
    JitterBuffer,
    LazyResampler,
    PcmCodec,
)
from snail.router import OutputGate


class NoResampleBackend:
    def stream(self, from_rate, to_rate):  # pragma: no cover
        raise AssertionError(f"unexpected resample {from_rate}->{to_rate}")


def _pipeline():
    pool = FramePool(capacity=64, slab_samples=480)
    return AudioPipeline(
        pool=pool,
        bus=FanoutBus(pool),
        resampler=LazyResampler(NoResampleBackend()),
        gate=OutputGate(depth=32),
        jitter=JitterBuffer(),
        codec=PcmCodec(),
        client_rate=48000,
    )


def test_on_client_audio_returns_480_frames():
    p = _pipeline()
    pcm = np.zeros(960, dtype=np.int16).tobytes()  # two 480 frames @ 48k
    frames = p.on_client_audio(pcm)
    assert isinstance(frames, list)
    assert len(frames) == 2
    assert all(f.shape == (480,) for f in frames)
    assert all(f.dtype == np.int16 for f in frames)


def test_partial_frame_returns_empty_then_completes():
    p = _pipeline()
    # 240 samples < one 480 frame → nothing published yet
    assert p.on_client_audio(np.zeros(240, dtype=np.int16).tobytes()) == []
    # next 240 completes the first frame
    frames = p.on_client_audio(np.zeros(240, dtype=np.int16).tobytes())
    assert len(frames) == 1
    assert frames[0].shape == (480,)
