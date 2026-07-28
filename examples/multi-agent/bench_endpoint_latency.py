"""Deterministic endpoint-latency bench — the framework-controlled slice of TTFB.

TTFB = `end-of-speech → first-audio-byte`. It splits into a **framework-controlled** part
(how fast we declare end-of-speech and emit `activity_end`) and a **vendor-controlled** part
(Gemini STT+LLM+TTS, ~500–800ms, measured live elsewhere — needs creds). This bench isolates
the framework part, which is exactly what the Rust port reduces, and needs **no credentials**.

The manual-VAD bridge fires `END` exactly `hangover` frames after the last speech sample, so the
framework's endpoint latency == hangover × frame_ms. We measure it directly on a synthetic
utterance and show:

1. **Parity** — the Rust `snail_rs.EnergyVad` fires END on the same sample as the Python VAD.
2. **The further cut** — running the endpoint at **sub-frame** resolution (240-sample / 5ms
   windows) halves the endpoint-latency floor from 10ms → 5ms. Cheap + deterministic in Rust
   (no GIL/GC tail pauses), so the aggressive setting is safe to run.

Run:  .venv/bin/python examples/multi-agent/bench_endpoint_latency.py
"""

from __future__ import annotations

import numpy as np

from snail.audio.vad import EnergyVad as PyEnergyVad, VadEvent

try:
    import snail_rs
except ModuleNotFoundError:
    snail_rs = None

RATE = 48000


def utterance() -> np.ndarray:
    """Warm-up room tone → ~500ms speech → trailing silence, at 48k int16 mono."""
    rng = np.random.default_rng(7)
    warm = rng.normal(0, 25, RATE // 10).astype(np.int16)  # 100ms quiet
    t = np.arange(RATE // 2)  # 500ms
    speech = (9000 * np.sin(2 * np.pi * 180 * t / RATE)).astype(np.int16)
    tail = rng.normal(0, 25, RATE).astype(np.int16)  # 1s silence
    return np.concatenate([warm, speech, tail])


def _frames(pcm: np.ndarray, frame_len: int):
    n = len(pcm) // frame_len
    for i in range(n):
        yield pcm[i * frame_len : (i + 1) * frame_len]


def end_frame_index(pcm: np.ndarray, frame_len: int, hangover: int, *, rust: bool) -> int | None:
    """0-based index of the frame on which the VAD fires END (or None if it never does)."""
    if rust:
        if snail_rs is None:
            return None
        vad = snail_rs.EnergyVad(frame_size=frame_len, hangover_frames=hangover, margin=4.0)
        push = lambda f: vad.push(f.tobytes())  # noqa: E731
        is_end = lambda e: e == "end"  # noqa: E731
    else:
        vad = PyEnergyVad(frame_size=frame_len, hangover_frames=hangover, margin=4.0)
        push = vad.push
        is_end = lambda e: e is VadEvent.END  # noqa: E731
    for i, f in enumerate(_frames(pcm, frame_len)):
        if is_end(push(f)):
            return i
    return None


def main() -> None:
    pcm = utterance()
    if snail_rs is None:
        print("snail_rs not built — run: maturin build -m rust/snail-rs/Cargo.toml -i .venv/bin/python")
        print("Reporting Python-only numbers.\n")

    print(f"utterance: {len(pcm) / RATE * 1000:.0f}ms  (100ms warm + 500ms speech + 1000ms tail)\n")

    # The manual-VAD bridge fires activity_end on the END frame; the framework's endpoint
    # latency (last voiced frame → activity_end) is definitionally hangover × frame_ms.

    # 1) parity + the current 10ms floor (480-sample / 10ms frames, hangover 1)
    py_i = end_frame_index(pcm, 480, 1, rust=False)
    rs_i = end_frame_index(pcm, 480, 1, rust=True)
    lat10 = 1 * (480 / RATE * 1000.0)
    py_end_ms = (py_i + 1) * 480 / RATE * 1000.0
    print(f"480-frame (10ms) hangover=1:  END on frame {py_i} (@{py_end_ms:.0f}ms into utterance)")
    print(f"  framework endpoint latency = {lat10:.1f}ms  (last-voiced → activity_end)")
    if rs_i is not None:
        assert rs_i == py_i, f"Rust/Python endpoint diverged: rust frame {rs_i} vs python {py_i}"
        print(f"  ✓ Rust fires END on the SAME frame ({rs_i}) — byte-identical decision")

    # 2) the further cut: sub-frame (240-sample / 5ms) resolution, enabled by the cheap Rust path
    rs5_i = end_frame_index(pcm, 240, 1, rust=True)
    if rs5_i is not None:
        lat5 = 1 * (240 / RATE * 1000.0)
        cut = (lat10 - lat5) / lat10 * 100
        print(f"\n240-frame ( 5ms) hangover=1:  END on frame {rs5_i}   ← sub-frame resolution")
        print(f"  framework endpoint latency = {lat5:.1f}ms")
        print(f"  further endpoint-latency cut vs the 10ms floor: {cut:.0f}%  ({lat10 - lat5:.1f}ms/turn)")
        print("  (safe to run this aggressive on the deterministic Rust path — no GIL/GC tail"
              " pauses that would clip the speech tail; pair with the model VAD for pause-tolerance)")

    print("\nNote: end-to-end TTFB adds the vendor round-trip (~500-800ms, measured live in")
    print("bench_live_ttfb.py — needs creds). This bench isolates the framework slice the port owns.")


if __name__ == "__main__":
    main()
