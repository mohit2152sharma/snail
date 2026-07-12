"""Tests for the audio cleaner stage (docs 11 §Cleaner) — dependency-free.

A fake ``DenoiseBackend`` stands in for ``librnnoise``; the framework value under test is
the 480-sample rechunker (alignment, remainder carry, flush padding) and the swappable
cleaner interface.
"""

from __future__ import annotations

import numpy as np

from snail.audio import (
    FRAME_LEN,
    NullCleaner,
    Rechunker,
    RNNoiseCleaner,
)


class GainBackend:
    """Fake denoise kernel: records frames, returns each halved (proves per-480 apply)."""

    def __init__(self) -> None:
        self.seen: list[np.ndarray] = []

    def process_480(self, frame: np.ndarray) -> np.ndarray:
        assert frame.shape == (FRAME_LEN,)
        assert frame.dtype == np.int16
        self.seen.append(frame)
        return (frame // 2).astype(np.int16)


def _ramp(n: int) -> np.ndarray:
    return (np.arange(n) % 1000).astype(np.int16)


# --- Rechunker -----------------------------------------------------------


def test_exact_480_is_one_in_one_out() -> None:
    rc = Rechunker()
    out = rc.push(_ramp(FRAME_LEN))
    assert len(out) == 1 and len(out[0]) == FRAME_LEN


def test_carries_remainder_across_pushes() -> None:
    rc = Rechunker()
    assert rc.push(_ramp(300)) == []  # buffered, nothing emitted
    out = rc.push(_ramp(300))  # 600 total → one 480-frame, 120 carried
    assert len(out) == 1
    assert rc.push(_ramp(360)) and rc._fill == 0  # 120+360 = 480 → emits, clean


def test_large_push_emits_multiple_frames() -> None:
    rc = Rechunker()
    out = rc.push(_ramp(FRAME_LEN * 3 + 17))
    assert len(out) == 3  # 3 full frames, 17 remainder held


def test_emitted_frames_are_independent_copies() -> None:
    rc = Rechunker()
    a = rc.push(_ramp(FRAME_LEN))[0]
    rc.push(_ramp(FRAME_LEN))  # reuses the internal buffer
    assert a[0] == 0  # first frame not clobbered by the second


def test_flush_zero_pads_partial() -> None:
    rc = Rechunker()
    rc.push(_ramp(100))
    tail = rc.flush()
    assert tail is not None and len(tail) == FRAME_LEN
    assert np.all(tail[100:] == 0)
    assert rc.flush() is None  # drained


def test_push_coerces_non_int16() -> None:
    rc = Rechunker()
    out = rc.push(np.arange(FRAME_LEN, dtype=np.float32))
    assert out[0].dtype == np.int16


# --- RNNoiseCleaner ------------------------------------------------------


def test_rnnoise_applies_backend_per_frame() -> None:
    be = GainBackend()
    cleaner = RNNoiseCleaner(be)
    frame = np.full(FRAME_LEN, 100, dtype=np.int16)
    out = cleaner.process(frame)
    assert len(out) == 1
    assert np.all(out[0] == 50)  # halved by backend
    assert len(be.seen) == 1


def test_rnnoise_buffers_until_aligned() -> None:
    be = GainBackend()
    cleaner = RNNoiseCleaner(be)
    assert cleaner.process(_ramp(200)) == []  # not enough for a frame yet
    out = cleaner.process(_ramp(400))  # 600 → one frame
    assert len(out) == 1 and len(be.seen) == 1


def test_rnnoise_flush_drains_tail() -> None:
    be = GainBackend()
    cleaner = RNNoiseCleaner(be)
    cleaner.process(_ramp(100))
    out = cleaner.flush()
    assert len(out) == 1  # padded tail denoised
    assert cleaner.flush() == []


def test_reset_clears_carry() -> None:
    cleaner = RNNoiseCleaner(GainBackend())
    cleaner.process(_ramp(100))
    cleaner.reset()
    assert cleaner.flush() == []  # carry gone


# --- NullCleaner ---------------------------------------------------------


def test_null_passthrough() -> None:
    n = NullCleaner()
    x = _ramp(333)
    out = n.process(x)
    assert len(out) == 1 and out[0] is x  # untouched, no rechunk
    assert n.process(np.array([], dtype=np.int16)) == []
    assert n.flush() == []
