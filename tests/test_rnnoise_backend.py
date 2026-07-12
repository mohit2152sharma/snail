"""Tests for the real librnnoise DenoiseBackend, wired through RNNoiseCleaner.

RNNoise is a trained denoiser — assertions are on shape/dtype/statefulness and that it
attenuates broadband noise (not bit-exact output).
"""

from __future__ import annotations

import numpy as np

from snail.audio import FRAME_LEN, RNNoiseCleaner
from snail.audio.rnnoise_backend import RNNoiseDenoiseBackend


def _noisy(n, seed=0):
    rng = np.random.default_rng(seed)
    tone = np.sin(np.arange(n) * 0.02) * 2000
    noise = rng.standard_normal(n) * 3000
    return (tone + noise).astype(np.int16)


def test_process_480_shape_and_dtype() -> None:
    be = RNNoiseDenoiseBackend()
    out = be.process_480(_noisy(FRAME_LEN))
    assert out.shape == (FRAME_LEN,) and out.dtype == np.int16
    be.close()


def test_attenuates_broadband_noise() -> None:
    be = RNNoiseDenoiseBackend()
    # pure noise → RNNoise should reduce energy once its filter warms over a few frames.
    rng = np.random.default_rng(1)
    last_in = last_out = None
    for _ in range(20):
        frame = (rng.standard_normal(FRAME_LEN) * 4000).astype(np.int16)
        last_out = be.process_480(frame)
        last_in = frame
    e_in = np.mean(last_in.astype(float) ** 2)
    e_out = np.mean(last_out.astype(float) ** 2)
    assert e_out < e_in  # noise suppressed
    be.close()


def test_close_is_idempotent() -> None:
    be = RNNoiseDenoiseBackend()
    be.close()
    be.close()  # no raise


def test_cleaner_with_real_backend() -> None:
    cleaner = RNNoiseCleaner(RNNoiseDenoiseBackend())
    out = cleaner.process(_noisy(960))  # two frames
    assert len(out) == 2
    assert all(f.shape == (FRAME_LEN,) and f.dtype == np.int16 for f in out)
    # tail flush drains the rechunker
    cleaner.process(_noisy(200))
    assert len(cleaner.flush()) == 1
