# Server-side VAD endpointing for lower per-turn TTFB

**Date:** 2026-07-17
**Status:** Approved design → implementation
**Goal:** Halve per-turn *end-of-speech → first-byte* latency in the multi-agent voice
example, without the mid-sentence barge-in regression that a naive `silence_duration_ms`
cut causes, and without swapping the model. Two workstreams:
1. **Endpointing** — cut the dominant ~800ms VAD wait via server-side energy VAD +
   hangover (bulk of the win).
2. **Hot-path efficiency + scalability** — cut per-frame CPU and remove event-loop
   blocking so TTFB stays low *and stays flat as concurrent users grow*. Under load,
   observed TTFB = intrinsic latency + queuing delay on the shared asyncio loop; a
   saturated loop inflates every session's `await`s. This workstream attacks that term.

## Problem

Per-turn TTFB (user stops speaking → first agent audio byte at the client) is dominated
by Gemini's automatic-VAD **end-of-speech silence wait**, currently
`silence_duration_ms=800` (`examples/multi-agent/backend/adapter.py`). Budget:

| Component | Where | ~ms | Movable under constraints |
|---|---|---|---|
| VAD end-of-speech silence | `silence_duration_ms=800` | ~800 | **This is the lever** |
| Model first-token inference | Gemini | ~300–600 | No (no model swap) |
| Network | — | ~50–100 | No |
| Egress jitter prefill | `JitterBuffer` | 30→10 (done) | Shipped |
| Client downlink lead | `downlink.js` | 50→25 (done) | Shipped |

Lowering `silence_duration_ms` directly makes the model **barge in** — it treats natural
mid-utterance pauses as end-of-turn and talks over the user. A flat trailing-silence
timer cannot tell "brief pause" from "done." An **energy VAD with a hangover window**
can: it holds the turn open through short pauses and ends it only after sustained
silence, so we can end turns at ~300ms of real silence without cutting the user off.

## Approach

Switch the **main-pool** (host + echo) adapter from Gemini *automatic* activity
detection to **manual activity mode**, and let the bridge decide turn boundaries:

```
mic ─opus─▶ bridge.on_client_audio ─decode 48k─▶ EnergyVad(frame480)
    │ START  ─▶ conn.send_realtime_control(ACTIVITY_START); flush pre-roll ring
    │ SPEECH ─▶ conn.send_realtime(audio)               (mic forwarded only in-speech)
    └ END    ─▶ conn.send_realtime_control(ACTIVITY_END)  ← turn ends here, ~500ms sooner
```

In manual mode Gemini ends the turn the instant it receives `ACTIVITY_END`, so
end-of-speech→turn-end drops from ~800ms to the hangover (~300ms): ~500ms off the
dominant term — the only change that makes a 50% cut reachable under the accepted
constraints (silence-lever only, no model swap).

**Scope:** host + echo only (the `main` connection pool). The **translate** agent keeps
its current adapter/auto-VAD — the Live-Translate model is Dev-only and separate, and
manual markers are out of scope for it.

## Components

### `src/snail/audio/vad.py` — `EnergyVad` (new core primitive)

Pure, I/O-free state machine, unit-testable with synthetic frames like `JitterBuffer`.

- **Input:** one 48k int16 mono frame at a time (480 samples / 10ms, the interior frame).
- **Output:** a transition per frame — `START`, `END`, or `NONE`.
- **State:** `SILENCE ⇄ SPEECH`.
  - `SILENCE → SPEECH` (`START`) after `start_frames` consecutive above-threshold frames
    (debounce, default 3 = 30ms) — rejects clicks/blips.
  - `SPEECH → SILENCE` (`END`) after `hangover_frames` consecutive below-threshold frames
    (default 30 = 300ms) — the anti-barge-in property: brief pauses < hangover keep the
    turn open.
- **Threshold — adaptive noise-floor:** maintain a running noise-floor estimate as an
  EMA of frame RMS, updated **only while in `SILENCE`** (`floor = (1-α)*floor + α*rms`,
  α default ~0.05) — so it tracks room tone but never ramps up to the speaker's own
  voice during `SPEECH`. A frame is "speech" when `rms > floor * margin` (margin default
  ~3.0). A short warm-up (first ~10 frames) seeds the floor and suppresses `START`.
  Env-overridable: `margin`, `hangover_frames`, `start_frames`, `alpha`. Adaptive chosen
  over a fixed RMS cutoff for robustness across mics and rooms.
- **API sketch:**
  ```python
  class VadEvent(enum.Enum): NONE; START; END
  class EnergyVad:
      def __init__(self, *, sample_rate=48000, frame_size=480,
                   start_frames=3, hangover_frames=30, margin=3.0,
                   alpha=0.05, warmup_frames=10): ...
      def push(self, frame: np.ndarray) -> VadEvent: ...  # one frame → transition
      def reset(self) -> None: ...                          # back to SILENCE (on promote)
      @property
      def state(self) -> VadState: ...
      @property
      def stats(self) -> dict: ...
  ```

### `ManualVadGeminiAdapter` (example `adapter.py`)

`build_setup` calls `super().build_setup(...)` then sets
`realtime_input_config = RealtimeInputConfig(automatic_activity_detection=
AutomaticActivityDetection(disabled=True))` — automatic VAD **off**; the bridge now owns
endpointing via markers. Replaces `VadGeminiAdapter` on the main pool in `app.py`. The
existing `VadGeminiAdapter` (auto) is kept for reference / fallback.

### Bridge wiring (`bridge.py`)

- Instantiate one `EnergyVad` per session.
- Drive it from the decoded 48k mic frames. **Tap point (decided):**
  `AudioPipeline.on_client_audio` returns the list of 480-sample 48k RAW frames it just
  published; the bridge feeds those same frames to the `EnergyVad` — no re-decode, one
  decode path. The VAD runs **once on the user stream**, independent of routing.
- **Pre-roll ring:** buffer the most recent ~200ms of pre-START frames; on `START`, send
  `ACTIVITY_START` then flush the ring as audio so the word onset isn't clipped (replaces
  the old `prefix_padding_ms`).
- **Marker sending:** on `START` → `conn.send_realtime_control(RealtimeControl.
  ACTIVITY_START)` to the active connection, then forward audio; on `END` → forward any
  remaining audio, then `ACTIVITY_END`. Mic audio is forwarded **only between START and
  END** (saves bandwidth + prevents noise-driven activity).
- **Promote reset:** on `_on_promote`, call `vad.reset()` alongside the existing
  `_pipeline.cut()` so the newly-active agent starts from `SILENCE`.
- **Barge-in:** a user `START` during agent speech naturally interrupts (Gemini treats a
  new activity as interruption); the existing `barge_in` control path is retained.

### TTFB instrumentation update (`bridge.py`)

Because the bridge now *decides* end-of-speech, move the TTFB `t0` from the
`UserTranscript(final)` proxy to the **last speech frame** (the frame before the hangover
begins). This makes the full **end-of-speech → first-byte** window — the chosen goal
metric — measurable server-side for the first time:
`ttfb = first_audio_byte_ts − last_speech_frame_ts` (= hangover + inference + pipeline).
Log per turn so before/after is provable from logs, not estimated.

## Data flow (per turn)

1. User silent → `EnergyVad` in `SILENCE`, adaptive floor tracking room tone. Mic not
   forwarded; last ~200ms kept in the pre-roll ring.
2. User speaks → after 30ms above-floor, `START` → `ACTIVITY_START` + pre-roll flush;
   mic frames forwarded to the active connection as `send_realtime` audio.
3. Brief mid-sentence pause (< 300ms) → stays `SPEECH` (turn held open — no barge-in).
4. User stops → after 300ms below-floor, `END` → `ACTIVITY_END`. `t0` recorded at the
   last speech frame. Model ends turn immediately and begins generating.
5. First agent audio byte arrives → TTFB logged; audio flows through the existing
   jitter → gate → codec egress to the client.

## Error handling / edge cases

- **Warm-up:** until the floor is seeded (first N frames), bias toward `SILENCE` (don't
  emit spurious `START`); acceptable since sessions open before the user speaks.
- **Never-ending speech / stuck SPEECH:** none needed — Gemini caps turn length; hangover
  guarantees `END` on real silence.
- **Marker ordering:** guarantee exactly one `ACTIVITY_START` per `ACTIVITY_END`; the
  state machine's single-transition-per-frame contract enforces this. Bridge asserts no
  double-START / double-END.
- **Handoff mid-turn:** `vad.reset()` on promote drops any in-flight SPEECH so the new
  agent isn't handed a dangling `ACTIVITY_END`.
- **Translate agent:** unaffected (keeps auto VAD); markers only sent to main-pool conns.

## Testing

- **`EnergyVad` unit tests** (`tests/`, pure, no network):
  - Silence-only stream → no transitions.
  - Speech burst → `START` fires after exactly `start_frames`; `END` after exactly
    `hangover_frames` of silence.
  - **Anti-barge-in:** speech, a 200ms pause (< hangover), more speech → **no** `END`
    across the pause (one continuous turn).
  - Debounce: a 1–2 frame blip → no `START`.
  - Adaptive floor: rising background noise → threshold tracks, no false `START`.
  - Pre-roll: frames before `START` are retained/flushed.
- **Bridge integration test** (fake transport / `MockVendorAdapter`): feed mic bytes for
  a silence→speech→silence arc → assert the outbound sequence is
  `ACTIVITY_START … audio … ACTIVITY_END` in order, and that a mid-turn pause does not
  emit a marker. Assert TTFB `t0` is taken at the last speech frame.
- Existing suites (210 py / 17 fe) stay green.

## Performance & scalability (workstream 2)

All audio work runs on the session's single asyncio loop (docs 06), so any CPU-bound or
blocking step adds **queuing delay** to every concurrent session. At N users,
`TTFB(N) = intrinsic + queue(loop_saturation)`. Goal: cut per-frame CPU and remove
loop-blocking so `TTFB(N)` stays ~flat, not super-linear. **Discipline: measure first —**
profile the real hot spots; optimize only what a profile/benchmark proves, then re-measure.

### Known hot-path targets (from code read — to confirm by profiling)

1. **Double parse per message.** `bridge._make_on_msg` calls `adapter.parse_event(raw)`,
   then `session.on_vendor_raw(raw)` parses the *same* raw again. Parse once; add
   `Session.on_events(list[ParsedEvent])` and have the bridge pass the already-parsed
   list. Halves inbound parse CPU per message.
2. **Per-frame allocations (ingress + egress).** `drain()` does
   `np.ascontiguousarray(...).tobytes()` per frame; `_publish` copies into a slab;
   `_rechunk_raw` slices per frame; jitter `_take` copies. Cut with per-session
   preallocated scratch arrays, `memoryview`/`ndarray.tobytes` avoidance, and in-place
   ops — kills per-frame heap churn and GC tail-latency under load.
3. **Per-frame await/syscall fan-out.** Egress does one `socket.send_bytes` per 10ms
   frame; ingress one `send_realtime` per drained chunk. **Coalesce** a drain's frames
   into one payload → one `await`/syscall per drain instead of per frame. Fewer loop
   wakeups = less saturation.
4. **CPU-bound codecs/resamplers on the loop.** soxr resample, opus encode/decode,
   rnnoise run synchronously on the loop. Verify each **releases the GIL** in its C
   extension. For any that don't — and only where profiling shows loop stalls — offload
   to a bounded worker via `run_in_executor` (thread if GIL-released; else a process
   pool), preserving the frame ownership-transfer contract.
5. **Vectorize residual Python loops.** Prefer whole-burst numpy ops over frame-by-frame
   Python where the interface allows (e.g. resample the full burst once, then slice).

### Data-structure choices

Keep the strong ones (deque-with-head jitter — no per-push concat; refcounted slab pool;
bounded rings). Add: **per-session preallocated scratch buffers** for resample/encode
outputs (no per-frame allocation), and a **`bytearray` coalescing buffer** for batched
egress. Choose structures for O(1) amortized hot-path ops and zero steady-state
allocation.

### Event-loop health

- Add a **loop-lag probe** (schedule a callback every 10ms; measure actual−scheduled
  delay) surfaced in stats — the direct saturation signal.
- Audit the receive loop for any hidden `await` on CPU work; keep tool execution off the
  hot path (already `create_task`).

### Benchmarks (new)

- **Micro** (`tests/`, timed asserts w/ generous bounds as regression guards): per-frame
  cost of `decode→resample→publish` and `jitter→gate→encode` (ns/frame), before/after.
- **Load** (fake transport, no network): N concurrent sessions pumping audio; report
  **loop-lag p50/p99** and **TTFB p50/p99 vs N**. Success = TTFB stays ~flat to the
  target N; loop-lag bounded.

## Success criteria

- `EnergyVad` holds turns through pauses shorter than the hangover (no barge-in) and ends
  them ~300ms after real end-of-speech.
- Measured `end-of-speech → first-byte` (from the new instrumentation) drops by ~500ms vs
  the 800ms-silence baseline — on the order of a 50% cut of the per-turn TTFB window.
- **Scale:** TTFB p50/p99 stays ~flat as concurrent sessions grow to the target N (no
  super-linear blow-up); loop-lag stays bounded. Per-frame hot-path CPU and steady-state
  allocations measurably reduced vs baseline.
- No regression in the existing test suites.

## Out of scope

- Model swap / inference-latency changes.
- Browser-side VAD (rejected: adds a client protocol + browser test surface; the bridge
  already has the decoded mic).
- Translate-agent endpointing.
- Semantic / ML endpointing (energy + hangover is sufficient for this target).
