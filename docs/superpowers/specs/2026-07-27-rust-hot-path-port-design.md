# Rust hot-path port + deeper TTFB cut — design

**Date:** 2026-07-27
**Goal:** Port the current code (all functionality) to Rust, and reduce TTFB further.

## Honest framing (read first)

TTFB (`end-of-speech → first-audio-byte`) is dominated by the **vendor round-trip**
(Gemini STT+LLM+TTS ≈ 500–800ms), per `docs/claude/00-vision-and-goals.md:36`. The
51% cut already shipped came entirely from **VAD endpointing** (10ms hangover → signal
end-of-turn sooner), not language speed. A Rust rewrite does **not** shrink TTFB by
itself — framework per-frame CPU is already microseconds.

What a Rust port *does* buy:
1. **Density/cost** — memory per session (the defensible win in the vision doc).
2. **Tail-latency determinism** — no GIL/GC pauses → we can run endpointing at a
   **confident near-0ms hangover** without risking clipped speech. *This* is the lever
   that reduces TTFB further, and it is only safe in a deterministic runtime.

## Strategy (decided)

**Hot-path Rust core first, via PyO3, then migrate outward.** Chosen over full rewrite
(too big, behavior-drift risk vs the live-tested Python) and TTFB-only (doesn't satisfy
"all functionality"). Terminal state is a full Rust port; first shippable value lands at
P2.

**TTFB lever (decided):** replace energy-VAD with a real speech model
(Silero via ONNX `ort`, or pure-Rust WebRTC VAD) in Rust → high end-of-speech
confidence → hangover ~0–5ms. Target **≥60% cut vs 800ms auto-VAD, zero speech-clip
regressions**.

## Architecture — phase 1 seam

PyO3 extension `snail_rs` (built with maturin) replaces the Python `AudioPipeline`
behind its **exact current interface** — `on_client_audio` / `drain` /
`on_vendor_audio` / `playout` / `cut` — plus a new `vad_feed() -> endpointing signal`.
Python keeps uvicorn websocket, `google-genai`, session/router/tools **unchanged**. The
bridge just calls into Rust. Everything except audio+VAD is behavior-unchanged → low
risk, incrementally testable, pytest stays green.

## Rust workspace

```
rust/
  snail-audio/   frame, pool, fanout, jitter, gate, resample, clean, opus, codec, pipeline
  snail-vad/     energy VAD + Silero (ONNX via ort) + endpointing state machine (hangover)
  snail-rs/      pyo3 cdylib — thin bindings exposing the two crates to Python
```

Dep crates (C-backed or pure-Rust — no reinvention):
`nnnoiseless` (pure-Rust RNNoise), `rubato`/`libsoxr` (resample), `opus`/`audiopus`
(codec), `voice_activity_detector` (Silero via `ort`).

## Phasing (terminal state = full port)

- **P0** — workspace + maturin + CI + golden-vector parity harness (Python emits
  reference PCM/signals; Rust must match).
- **P1** — audio primitives → Rust, byte-parity tests, swap into pipeline. *Win: density.*
- **P2** — VAD + endpointing → Rust + Silero + new TTFB bench. *Win: the further TTFB cut.*
- **P3+** — migrate Gemini Live ws + session loop into Rust (tokio), collapse PyO3 →
  full port / single binary. *Win: completes "all functionality," kills google-genai churn.*

## Testing

- **Golden-vector parity:** Python emits reference outputs for known inputs; Rust must
  match — bit-exact for pool/fanout/gate/jitter logic, tolerance for resample/codec
  float paths.
- Existing pytest suite stays green through P1/P2 (Rust is a drop-in).
- Live TTFB A/B bench extended with Silero (`examples/multi-agent/bench_live_ttfb.py`).

## Open (defaulted to keep momentum)

- **VAD dep:** start with energy/WebRTC (pure-Rust) for P0/P1; choose Silero-vs-WebRTC
  at P2 when the model actually lands.
- **Resample crate:** `rubato` (pure-Rust) first; fall back to `libsoxr` bindings only if
  parity tolerance vs Python `soxr` fails.
