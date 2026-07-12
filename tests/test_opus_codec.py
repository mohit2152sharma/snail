"""Tests for OpusCodec — the client-leg opus (de)compressor (libopus).

Opus is lossy, so assertions are on frame sizes, packet compression, and coarse signal
fidelity (correlation / energy), never bit-exact equality.
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
)
from snail.audio.opus_codec import OpusCodec
from snail.router import OutputGate


def _sine(n, freq=440, rate=48000):
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)


class NoResample:
    def stream(self, f, t):  # pragma: no cover
        raise AssertionError("no resample at 48k")


def test_roundtrip_length_and_compression() -> None:
    c = OpusCodec()
    x = _sine(480)
    pkt = c.encode(x)
    assert isinstance(pkt, bytes)
    assert len(pkt) < 480 * 2  # compressed vs raw PCM16 (960 bytes)
    out = c.decode(pkt)
    assert out.dtype == np.int16 and len(out) == 480


def test_roundtrip_preserves_signal_coarsely() -> None:
    enc = OpusCodec()
    dec = OpusCodec()
    # opus has ~6.5ms algorithmic delay, so samples aren't phase-aligned; compare RMS
    # energy (delay-invariant) after a few frames let the codec state settle.
    x = _sine(480)
    for _ in range(5):
        out = dec.decode(enc.encode(x))
    rms_in = np.sqrt(np.mean(x.astype(float) ** 2))
    rms_out = np.sqrt(np.mean(out.astype(float) ** 2))
    assert 0.5 < rms_out / rms_in < 1.5  # energy preserved within a lossy margin


def test_encode_rejects_wrong_frame_size() -> None:
    c = OpusCodec()
    with pytest.raises(ValueError):
        c.encode(_sine(300))  # not the 480 frame size


def test_decode_handles_20ms_packet() -> None:
    enc = OpusCodec(frame_size=960)  # 20ms encoder
    dec = OpusCodec()  # default 10ms — decode is duration-agnostic
    out = dec.decode(enc.encode(_sine(960)))
    assert len(out) == 960  # actual packet duration returned


def _pipeline(codec):
    pool = FramePool(capacity=128, slab_samples=480)
    bus = FanoutBus(pool)
    return AudioPipeline(
        pool=pool,
        bus=bus,
        resampler=LazyResampler(NoResample()),
        gate=OutputGate(depth=32),
        jitter=JitterBuffer(frame_size=480, prefill_frames=1),
        codec=codec,
        client_rate=48000,  # opus is native 48k → no resample
    ), bus


def test_pipeline_ingress_decodes_opus() -> None:
    client = OpusCodec()  # stands in for the browser encoder
    pipe, bus = _pipeline(OpusCodec())
    bus.subscribe("agent", source=AudioSource.USER_RAW, target_rate=48000)
    # browser sends two 10ms opus packets.
    pipe.on_client_audio(client.encode(_sine(480)))
    pipe.on_client_audio(client.encode(_sine(480)))
    chunks = pipe.drain().get("agent", [])
    total = sum(len(np.frombuffer(c, dtype=np.int16)) for c in chunks)
    assert total == 960  # decoded to two 480 interior frames


def test_pipeline_egress_encodes_opus() -> None:
    pipe, _ = _pipeline(OpusCodec())
    pipe.hold_token("a")
    pipe.on_vendor_audio(_sine(960).tobytes(), vendor_rate=48000)  # arms jitter
    frame = pipe.playout("a")
    assert frame is not None
    assert len(frame) < 480 * 2  # opus packet, smaller than raw PCM frame
