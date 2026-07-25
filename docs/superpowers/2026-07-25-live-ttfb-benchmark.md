# Live Gemini TTFB benchmark — server-VAD endpointing

**Date:** 2026-07-25
**Harness:** `examples/multi-agent/bench_live_ttfb.py` (real Gemini Live, Dev API).
**Metric:** per-turn *end-of-speech → first-audio-byte*, median of 4 trials, identical
utterance (macOS `say`, "Hello, what is two plus two?", 1872ms), realtime-paced 20ms
chunks. A/B differs only in VAD mode.

- **auto** — Gemini automatic VAD, `silence_duration_ms=800` (host/echo baseline).
- **manual** — automatic VAD off; we drive `activity_start`/`activity_end` exactly as
  `ManualVadGeminiAdapter` + the bridge `EnergyVad` do, ending the turn `HANGOVER_MS`
  after the last speech sample.

## Results

### `gemini-2.5-flash-native-audio-latest`
| mode | median | reduction |
|---|---|---|
| auto (800ms) | 4816 ms | — |
| manual (300ms hangover) | 3592 ms | **25%** (1224 ms saved) |

Native-audio inference floor is large (~3s), so it dominates the window.

### `gemini-3.1-flash-live-preview` (lower-latency; representative)
Auto baseline median = **1517 ms**. Inference+network floor ≈ **760 ms** (fixed).

| manual hangover | median | reduction vs auto |
|---|---|---|
| 300 ms | 1147 ms | **24%** |
| 150 ms | 1026 ms | **32%** |
| 80 ms | 841 ms | **45%** |
| ~0 ms (extrapolated) | ~760 ms | **~50%** |

## Interpretation

`end-of-speech → first-byte  =  hangover  +  inference/network floor (~760ms, fixed)`.

The endpointing change removes the **500ms** gap between Gemini's flat 800ms silence
timer and a 300ms hangover, and measurably cuts per-turn TTFB **live** (24–45% depending
on hangover). Because the model's inference floor is fixed and not code-controllable, the
**50% target on this metric is reached only as the hangover approaches ~0ms** — which
reintroduces the mid-sentence barge-in the flat-timer reduction was rejected for.

The energy VAD's advantage over a flat `silence_duration_ms` cut is that its adaptive
floor + hangover distinguishes a brief pause from a real stop, so a given hangover is
*safer* than the same flat silence value — but it does not remove the fundamental
hangover ↔ barge-in ↔ latency tradeoff.

## Recommendation

Operating point is a product call (`SNAIL_VAD_HANGOVER_FRAMES`, 10ms/frame):
- **150 ms (15 frames)** — ~32% live cut, good pause tolerance. Suggested default.
- **300 ms (30 frames)** — ~24%, most conservative (current default).
- **80 ms (8 frames)** — ~45%, aggressive; only with a well-tuned floor/margin.

Reproduce:
```
set -a && . examples/multi-agent/.env && set +a
SNAIL_BENCH_MODEL=gemini-3.1-flash-live-preview SNAIL_BENCH_HANGOVER_MS=150 \
  .venv/bin/python examples/multi-agent/bench_live_ttfb.py
```

## Scalability (hot-path cost & event-loop saturation)

`tests/bench/` (`pytest -m bench`), on this dev box:

| op | cost | ceiling (10ms cadence) |
|---|---|---|
| opus **encode** (egress) | 135 us/frame | **~74 sessions/core** |
| opus decode (ingress) | 62 us/frame | ~160 sessions/core |
| soxr 48k→16k resample | 6 us/frame | ~1737 sessions/core |
| ingress→drain (whole path) | 10 us/frame | — |

Whole-pipeline load test (N concurrent sessions pumping 20ms ticks) — event-loop lag
stays **flat**:

| N sessions | loop p50 | loop p99 |
|---|---|---|
| 1 | 1.0 ms | 1.3 ms |
| 10 | 1.0 ms | 1.2 ms |
| 50 | 0.8 ms | 1.5 ms |

**Finding:** the ingress/routing hot path scales flat to 50 sessions; **opus encode is the
single scale ceiling** (~74 sessions/core). Follow-up if higher density is needed: opus
encode does not cheaply offload (thread pool won't help unless the ext releases the GIL) —
options are a process/worker pool for egress encode, or letting the client decode PCM.
The single-parse and drain no-copy wins removed per-message/per-frame overhead on the way.
