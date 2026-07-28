"""Parity: the Rust `snail_rs` hot path must match the Python reference on identical input.

The audio-plane primitives were ported to Rust (see docs/superpowers/specs/…-rust-hot-path-port).
These tests are the golden-vector guard: same input → same output. They skip cleanly when the
`snail_rs` extension isn't built, so the pure-Python suite stays runnable without a Rust toolchain.

Build the extension with:
    .venv/bin/maturin build -m rust/snail-rs/Cargo.toml -i .venv/bin/python
    uv pip install --python .venv/bin/python --force-reinstall --no-deps \
        rust/target/wheels/snail_rs-*.whl
"""

from __future__ import annotations

import numpy as np
import pytest

from snail.audio.vad import EnergyVad as PyEnergyVad, VadEvent

snail_rs = pytest.importorskip("snail_rs")

_EVENT_STR = {
    VadEvent.NONE: "none",
    VadEvent.START: "start",
    VadEvent.END: "end",
}


def _frames() -> list[np.ndarray]:
    """A deterministic mix of silence, ramps, and tone bursts — 480-sample interior frames."""
    rng = np.random.default_rng(1234)
    out: list[np.ndarray] = []
    # warm-up room tone (quiet)
    for _ in range(12):
        out.append((rng.normal(0, 30, 480)).astype(np.int16))
    # speech-ish loud tone bursts
    t = np.arange(480)
    tone = (8000 * np.sin(2 * np.pi * 220 * t / 48000)).astype(np.int16)
    for _ in range(20):
        out.append(tone.copy())
    # brief pause (should be held open by hangover), then more speech
    for _ in range(3):
        out.append((rng.normal(0, 40, 480)).astype(np.int16))
    for _ in range(10):
        out.append(tone.copy())
    # long trailing silence → end-of-speech
    for _ in range(40):
        out.append((rng.normal(0, 25, 480)).astype(np.int16))
    return out


@pytest.mark.parametrize("hangover", [1, 5, 30])
def test_energy_vad_event_sequence_matches_python(hangover: int) -> None:
    cfg = dict(
        frame_size=480,
        start_frames=3,
        hangover_frames=hangover,
        margin=3.0,
        alpha=0.05,
        warmup_frames=10,
    )
    py = PyEnergyVad(**cfg)
    rs = snail_rs.EnergyVad(**cfg)

    py_events: list[str] = []
    rs_events: list[str] = []
    for f in _frames():
        py_events.append(_EVENT_STR[py.push(f)])
        rs_events.append(rs.push_samples(f.tolist()))

    assert rs_events == py_events, (
        f"VAD event sequences diverge at hangover={hangover}:\n"
        f"  py={py_events}\n  rs={rs_events}"
    )
    assert rs.state == py.state.value


def test_energy_vad_floor_tracks_python() -> None:
    py = PyEnergyVad()
    rs = snail_rs.EnergyVad()
    for f in _frames():
        py.push(f)
        rs.push_samples(f.tolist())
    # adaptive noise floor is a float EMA — allow a tiny tolerance for fp order-of-ops.
    assert rs.floor == pytest.approx(py.stats["floor"], rel=1e-9, abs=1e-6)


def test_pipeline_passthrough_bytes_at_interior_rate() -> None:
    """At 48k in/out (no resample) the Rust pipeline is a byte-exact fan-out + drain."""
    p = snail_rs.Pipeline(client_rate=48000)
    p.attach_consumer("agent", "raw", 48000)
    p.hold_token("agent")

    frame = np.arange(480, dtype=np.int16)
    published = p.on_client_audio(frame.tobytes())
    assert len(published) == 1
    assert published[0] == frame.tobytes()

    drained = p.drain()
    assert list(drained.keys()) == ["agent"]
    assert drained["agent"][0] == frame.tobytes()  # 48k target → no resample, identical


def test_pipeline_egress_gate_single_voice() -> None:
    p = snail_rs.Pipeline(client_rate=48000, prefill_frames=1)
    p.hold_token("agent")
    burst = np.full(480, 500, dtype=np.int16).tobytes()
    p.on_vendor_audio(burst, 48000)
    assert p.playout("agent") is not None  # token holder is heard
    p.on_vendor_audio(burst, 48000)
    assert p.playout("intruder") is None  # non-holder suppressed (single-voice invariant)
