"""Tests for the DSP trio: resample (lazy/memoized), codec (PCM), jitter buffer.

Dependency-free: a fake ``ResampleBackend`` stands in for ``soxr``; the framework value
under test is the lazy/memoized/no-op resample policy, PCM round-trip, and the jitter
buffer's prebuffer/underrun state machine + boundary-stitching drain.
"""

from __future__ import annotations

import numpy as np

from snail.audio import (
    JitterBuffer,
    JitterState,
    LazyResampler,
    PcmCodec,
)


# --- resample ------------------------------------------------------------


class RatioResampler:
    """Fake per-pair converter: linear-ish rescale by rate ratio; counts calls."""

    def __init__(self, from_rate, to_rate):
        self.pair = (from_rate, to_rate)
        self.calls = 0

    def process(self, samples):
        self.calls += 1
        n_out = max(1, round(len(samples) * self.pair[1] / self.pair[0]))
        return np.resize(samples, n_out).astype(np.int16)


class RatioBackend:
    def __init__(self):
        self.made: list[tuple[int, int]] = []

    def stream(self, from_rate, to_rate):
        self.made.append((from_rate, to_rate))
        return RatioResampler(from_rate, to_rate)


def _pcm(n):
    return (np.arange(n) % 100).astype(np.int16)


def test_equal_rate_is_noop_no_backend_call() -> None:
    be = RatioBackend()
    r = LazyResampler(be)
    x = _pcm(480)
    out = r.resample(x, from_rate=48000, to_rate=48000)
    assert out is x  # untouched, same object
    assert be.made == []  # no converter created


def test_downsample_creates_converter() -> None:
    be = RatioBackend()
    r = LazyResampler(be)
    out = r.resample(_pcm(480), from_rate=48000, to_rate=16000)
    assert len(out) == 160  # 48k→16k = 1/3
    assert be.made == [(48000, 16000)]


def test_same_pair_reuses_one_converter() -> None:
    be = RatioBackend()
    r = LazyResampler(be)
    r.resample(_pcm(480), from_rate=48000, to_rate=16000)
    r.resample(_pcm(480), from_rate=48000, to_rate=16000)
    assert be.made == [(48000, 16000)]  # created once, memoized
    assert r.rate_pairs == [(48000, 16000)]


def test_distinct_pairs_each_get_a_converter() -> None:
    be = RatioBackend()
    r = LazyResampler(be)
    r.resample(_pcm(480), from_rate=48000, to_rate=16000)  # Gemini
    r.resample(_pcm(480), from_rate=48000, to_rate=24000)  # OpenAI / agent-out
    assert set(be.made) == {(48000, 16000), (48000, 24000)}


def test_reset_drops_converters() -> None:
    be = RatioBackend()
    r = LazyResampler(be)
    r.resample(_pcm(480), from_rate=48000, to_rate=16000)
    r.reset()
    assert r.rate_pairs == []


# --- codec (PCM) ---------------------------------------------------------


def test_pcm_roundtrip() -> None:
    c = PcmCodec()
    x = np.array([0, 1, -1, 32767, -32768], dtype=np.int16)
    assert np.array_equal(c.decode(c.encode(x)), x)


def test_pcm_encode_is_little_endian_bytes() -> None:
    c = PcmCodec()
    assert c.encode(np.array([1], dtype=np.int16)) == b"\x01\x00"  # LE


def test_pcm_encode_coerces_dtype() -> None:
    c = PcmCodec()
    out = c.encode(np.array([1, 2], dtype=np.float32))
    assert out == np.array([1, 2], dtype=np.int16).tobytes()


# --- jitter buffer -------------------------------------------------------


def _jb(prefill_frames=2, frame_size=480):
    return JitterBuffer(frame_size=frame_size, prefill_frames=prefill_frames)


def test_prebuffers_before_playing() -> None:
    jb = _jb(prefill_frames=2, frame_size=100)
    jb.push(_pcm(150))  # < 200 prefill
    assert jb.state is JitterState.PREBUFFERING
    assert jb.pop() is None
    jb.push(_pcm(60))  # 210 ≥ 200 → arms
    assert jb.state is JitterState.PLAYING


def test_pop_returns_fixed_frames() -> None:
    jb = _jb(prefill_frames=1, frame_size=100)
    jb.push(_pcm(250))
    a, b = jb.pop(), jb.pop()
    assert len(a) == 100 and len(b) == 100
    assert jb.buffered == 50


def test_underrun_re_arms_prebuffering() -> None:
    jb = _jb(prefill_frames=1, frame_size=100)
    jb.push(_pcm(120))  # arms; one full frame + 20
    assert jb.pop() is not None  # 100 out, 20 left
    assert jb.pop() is None  # underrun: 20 < 100
    assert jb.state is JitterState.PREBUFFERING
    assert jb.stats["underruns_total"] == 1


def test_drain_stitches_across_chunks() -> None:
    jb = _jb(prefill_frames=1, frame_size=100)
    jb.push(_pcm(60))
    jb.push(_pcm(60))  # 120 total across two chunks
    frame = jb.pop()
    assert len(frame) == 100  # stitched from both chunks


def test_drain_partial_zero_pads_tail() -> None:
    jb = _jb(prefill_frames=1, frame_size=100)
    jb.push(_pcm(30))
    jb.push(_pcm(70))  # arms at 100
    jb.pop()  # drains the 100
    jb.push(_pcm(40))
    tail = jb.drain_partial()
    assert len(tail) == 100 and np.all(tail[40:] == 0)


def test_flush_clears_and_rearms() -> None:
    jb = _jb(prefill_frames=1, frame_size=100)
    jb.push(_pcm(300))
    jb.flush()
    assert jb.buffered == 0 and jb.state is JitterState.PREBUFFERING
    assert jb.pop() is None


# --- adaptive (AIMD) target ------------------------------------------------


def test_target_starts_at_ceiling_no_evidence_yet() -> None:
    jb = JitterBuffer(frame_size=100, prefill_frames=3, min_prefill_frames=1)
    assert jb.target_frames == 3
    jb.push(_pcm(250))  # < 300 ceiling → still prebuffering
    assert jb.state is JitterState.PREBUFFERING


def test_underrun_grows_target_capped_at_ceiling() -> None:
    jb = JitterBuffer(frame_size=100, prefill_frames=2, min_prefill_frames=1)
    jb.push(_pcm(220))  # arms at 200 (ceiling=2)
    jb.pop()  # 100 out, 120 left
    jb.pop()  # 100 out, 20 left
    assert jb.target_frames == 2  # unchanged: no underrun yet
    jb.pop()  # 20 < 100 → underrun → grows target
    assert jb.state is JitterState.PREBUFFERING
    assert jb.target_frames == 2  # already at the ceiling, can't grow further

    # a ceiling of 1 still caps growth even after a real underrun
    jb2 = JitterBuffer(frame_size=100, prefill_frames=1, min_prefill_frames=1)
    jb2.push(_pcm(150))  # arms at 100 (ceiling=1)
    assert jb2.pop() is not None  # 100 out, 50 left
    assert jb2.pop() is None  # 50 < 100 → underrun → tries to grow, capped at 1
    assert jb2.target_frames == 1


def test_target_decays_after_stability_streak() -> None:
    jb = JitterBuffer(
        frame_size=100, prefill_frames=3, min_prefill_frames=1, decay_after=4
    )
    assert jb.target_frames == 3
    jb.push(_pcm(300))  # arms at the ceiling (300)
    for _ in range(4):  # 4 clean pops == decay_after → target drops by one frame
        assert jb.pop() is not None
        jb.push(_pcm(100))  # keep it fed so pops stay clean
    assert jb.target_frames == 2


def test_target_never_decays_below_floor() -> None:
    jb = JitterBuffer(
        frame_size=100, prefill_frames=2, min_prefill_frames=1, decay_after=1
    )
    jb.push(_pcm(200))  # arms at the ceiling (200)
    for _ in range(10):
        assert jb.pop() is not None
        jb.push(_pcm(100))
    assert jb.target_frames == 1  # floored, never lower


def test_second_arm_uses_the_decayed_target() -> None:
    """The real per-turn TTFB payoff: turn 2 re-arms faster than turn 1 on a clean link."""
    jb = JitterBuffer(
        frame_size=100, prefill_frames=3, min_prefill_frames=1, decay_after=2
    )
    jb.push(_pcm(300))  # turn 1: arms at the conservative ceiling (300)
    for _ in range(2):  # a clean streak decays the target — no underrun anywhere
        assert jb.pop() is not None
        jb.push(_pcm(100))
    assert jb.target_frames == 2  # decayed once, from evidence, not guesswork
    jb.flush()  # turn 1 ends cleanly (barge-in/turn-complete) — not an underrun
    assert jb.target_frames == 2  # the decayed target survives the flush
    jb.push(_pcm(199))  # turn 2: would have needed 300 before decay; 200 arms it now
    assert jb.state is JitterState.PREBUFFERING
    jb.push(_pcm(1))
    assert jb.state is JitterState.PLAYING  # armed at the decayed 200, not the original 300
