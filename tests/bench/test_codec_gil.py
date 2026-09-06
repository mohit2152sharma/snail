"""Codec/resampler per-frame cost audit → concurrent-session capacity.

The audio plane runs codecs/resamplers synchronously on the session loop, so each op's
per-frame CPU cost sets a ceiling on concurrent sessions: at a 10ms frame cadence one core
saturates near ``10000us / per_frame_us`` sessions. This measures the deterministic cost
(robust, unlike loop-lag on a shared box) and prints the derived capacity. Realistic
whole-pipeline scaling is checked in test_hotpath_bench (flat loop-lag to N=50).

Finding: opus encode (~135us/frame) is the heaviest op and the practical scale ceiling;
soxr resample and PCM ingress are ~an order of magnitude cheaper.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from snail.audio.opus_codec import OpusCodec
from snail.audio.soxr_backend import SoxrResampleBackend


def _per_call_us(work, *, iters: int = 2000) -> float:
    for _ in range(100):  # warm up C init
        work()
    t = time.perf_counter()
    for _ in range(iters):
        work()
    return (time.perf_counter() - t) / iters * 1e6


def _report(name, per_us, capsys):
    cap = 10000.0 / per_us  # sessions per core at 10ms/frame cadence
    with capsys.disabled():
        print(f"\n  {name}: {per_us:.0f} us/frame  →  ~{cap:.0f} sessions/core (10ms cadence)")


@pytest.mark.bench
def test_opus_encode_cost(capsys):
    codec = OpusCodec()
    frame = np.zeros(480, dtype=np.int16)
    per_us = _per_call_us(lambda: codec.encode(frame))
    _report("opus encode", per_us, capsys)
    assert per_us < 1000.0  # sub-ms/frame (else < 10 sessions/core — would need offload)


@pytest.mark.bench
def test_opus_decode_cost(capsys):
    codec = OpusCodec()
    enc = codec.encode(np.zeros(480, dtype=np.int16))
    per_us = _per_call_us(lambda: codec.decode(enc))
    _report("opus decode", per_us, capsys)
    assert per_us < 1000.0


@pytest.mark.bench
def test_soxr_resample_cost(capsys):
    stream = SoxrResampleBackend().stream(48000, 16000)
    frame = np.zeros(480, dtype=np.int16)
    per_us = _per_call_us(lambda: stream.process(frame))
    _report("soxr 48k→16k", per_us, capsys)
    assert per_us < 1000.0
