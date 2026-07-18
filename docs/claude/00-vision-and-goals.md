# Vision & Goals

## Product definition

**Snail** = a framework for **multi-voice-agents** — the voice equivalent of
chat-based multi-agent systems.

- Leverages **Gemini Live** and **OpenAI Realtime** to build voice agents.
- Provides a **common, vendor-neutral interface** so the end user can switch
  between vendors/models without switching cost.
- **Multi-agent, single-face:** a single session can host multiple agents (e.g.
  one Gemini agent + one OpenAI agent). The end user perceives **one** agent, not
  many. The framework manages routing audio to/from those agents and avoids audio
  overlap.

## Performance framing (important)

Performance is a **design principle**, not a headline number. The original "30x
faster / 30x less memory" framing was dropped in favor of "squeeze memory, CPU,
and latency as far as pure Python + C-backed libraries allow."

Where performance actually matters, honestly assessed:

- **Density / cost** (sessions per box): the real, defensible win. Python voice
  frameworks (pipecat, livekit) waste memory via pydantic, fat frame-object
  hierarchies, and per-frame object churn. Snail avoids these → big memory wins
  are believable.
- **Interruption latency (barge-in)** and **handoff latency**: user-perceived and
  **framework-controlled**. This is where Snail can genuinely beat competitors on
  *felt* quality.
  > **TODO(v1-vad / listener-economics — see 09§E):** this claim is not yet earned.
  > (a) v1 defers local VAD (11) → barge-in is a vendor round-trip, no edge. (b) The
  > handoff-latency win rides on the listener model, which has unresolved
  > context/cost/Gemini-modality holes (09§E). Either fix those or soften this to
  > "density/cost is the defensible win; latency is aspirational until VAD + listener
  > model land."
- **Steady-state audio-shuttling latency**: mostly bounded by network + vendor
  (STT+LLM+TTS ≈ 500–800ms loop). Framework overhead is a tiny slice — cutting it
  is invisible to the user. Do not oversell this.

**Honest positioning:** "much cheaper to run + near-zero barge-in/handoff latency,"
verified by a benchmark harness — not "30x faster real-time."

### Per-turn TTFB target vs pipecat/livekit (sourced)

Published numbers found for the two named competitors (see PR discussion for search
sources — pipecat's own docs/benchmark repo, LiveKit's own blog + third-party
multi-stack benchmarks):

```
pipecat   worked-example component TTFB (STT 200ms + LLM TTFT 268ms + TTS TTFB 44ms)
              ≈ 512ms      (their own P95 component target: <300ms per stage)
          production median, cross-platform benchmark (Cekura)   ≈ 3.15s (default config)
livekit   own stated bar: "Time-To-First-Audio must stay under 500ms"
          3rd-party 30-stack benchmark: default AgentSession  ≈ 1.2-1.4s p95
                                          fully latency-tuned  ≈ 500-650ms p95
```

Target: **under 250ms** — half the ~500ms bar both frameworks publish as their own
quality line. Honest budget against that target (docs 11 for the mechanism):

```
silence-dwell (vendor VAD, our floor)     ~200ms   (relaxed from a 500ms floor —
                                                     Google's own Gemini Live guidance
                                                     recommends 500-800ms and warns
                                                     100-200ms risks clipping natural
                                                     pauses; this is a deliberate
                                                     quality-for-latency trade, not a
                                                     free win)
vendor generation (STT-finalize+LLM+TTS,   unmeasured — bundled inside one vendor
  bundled, opaque)                          call (Gemini Live), not framework code;
                                             public estimates put comparable bundled
                                             models at 300-600ms first-audio-chunk
vendor round trip + framework overhead     ~10ms    (adaptive jitter floor, docs 11)
```

**Reality check:** the 200ms dwell + ~10ms framework overhead is the only part this
codebase's algorithms/data-structures can move, and it's already near its floor. The
remaining, larger piece (vendor generation) is Google's compute, not ours — so a
confident "<250ms, guaranteed" claim would be exactly the overselling this doc warns
against. What's real: `ClientBridge.ttfb_stats` (docs 11) measures the actual end-to-end
number (last mic chunk in → first agent byte out) against live traffic, so the claim is
checked, not asserted. Until measured against a real Gemini connection, treat 250ms as
the *design target* the framework's own knobs are tuned for, not a verified result.

## Implementation constraints

- **Pure Python. No custom Rust/C layer** (at least for now).
- Lean on **existing C-backed libraries**: `numpy` (audio buffers, zero-copy
  views), `msgspec` (vendor event parsing, flat structs — replaces pydantic).
- Performance wins come from **not repeating the waste** of pipecat/livekit:
  - kill pydantic → use `msgspec` structs
  - kill per-frame object churn → `__slots__`/structs, buffer reuse, object pools
  - zero-copy audio via `memoryview` / numpy views into ring buffers
- `msgspec`'s real job = parsing vendor WebSocket **control events** (JSON), not
  internal audio. Audio = raw PCM → numpy.
- **Benchmark against pipecat early** — do not trust our own performance claims
  without a repro harness.

## Vendor scope

- Vendor-neutrality **fully stays**; OpenAI Realtime is an active co-target.
- Gemini side targets **`gemini-2.5-flash-live` only** (both **Gemini Developer
  API** and **Vertex AI** backends). We ignore Gemini 3.1 / 2.0 quirks.
