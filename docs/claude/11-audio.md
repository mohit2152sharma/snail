# Audio Plane

The audio subsystem. Where the two real perf wins live: **barge-in latency** and
**overlap avoidance**, plus the **density** win (fixed buffers, no object churn).

## AudioFrame — the unit

```
AudioFrame (msgspec.Struct — fixed fields, NO dict, NO strings):
   samples      : numpy int16 view    ← payload. mono. one rate.
   sample_rate  : uint32
   n_samples    : uint32
   source       : uint8 enum   (USER_RAW | USER_CLEAN | AGENT)
   seq          : uint64       (ordering)
   t_start      : uint64       (pts, sample-clock — sync + recording)
   flags        : uint16       (bitfield: IS_SPEECH, IS_FINAL, ...)
```

Deliberately absent (pipecat's fat): no `name`/`id` strings, no `metadata` dict, no
`num_channels` (mono forced — voice is mono), no per-frame pydantic. Header ≈ 40 B
fixed; samples are the only real memory.

## Canonical-interior invariant

```
codec (opus) + encoding (base64/string) + vendor rates  →  live ONLY at the edges
AudioFrame interior  →  ALWAYS PCM int16, mono, 48kHz
```

Interior code sees **one format, one rate** → no branching, no repeated conversion.

### Why 48kHz — the opus + RNNoise synergy

```
libopus always decodes to 48kHz  (opus IS a 48k codec)
RNNoise wants exactly 48kHz mono, 480-sample (10ms) frames
```

Both defaults are native 48k → the default stack (opus-in + RNNoise-clean) is 48k
end-to-end with **zero resampling until the vendor edge**. No explicit "upscale"
step — opus decode already lands at 48k.

## Defaults (LOCKED)

```
codec:      opus in / opus out       (PCM supported, opt-in)
cleaner:    RNNoise on BY DEFAULT    (swappable; PER-CONSUMER — a consumer may take
                                      RAW audio; skipped entirely if nobody wants clean)
interior:   48kHz PCM int16 mono
resample:   LAZY, per distinct target rate — only for a subscribed consumer that
            needs a different rate; shared across same-rate consumers; skipped when
            target == interior (soxr / libsoxr)
client leg: binary WS frames + opus  (density/CPU win, NOT per-chunk latency)
```

## Codec vs encoding — the latency truth

Two independent levers, very different payoff:

```
CODEC     opus vs PCM       →  the REAL bandwidth/latency lever (~10–20x smaller)
                               PCM 48k=768kbps, 16k=256kbps; opus voice=16–32kbps
ENCODING  bytes vs base64   →  +33% size + CPU. NOT latency (chunks are tiny).
                               bytes wins DENSITY (skip base64 CPU × N sessions),
                               not single-session latency.
```

**Vendor leg forces base64.** Verified: Gemini Live + OpenAI Realtime send audio as
**base64-in-JSON over WebSocket text frames** — no binary-frame path. The SDK's
`types.Blob(data=<bytes>)` accepts bytes but **base64-wraps them on the wire**.
So bytes-forcing is a **client-leg** win only; the vendor leg pays base64 regardless.

```
client ⇄ SNAIL    → WE control: binary frames + opus.  ✓
SNAIL  ⇄ vendor   → base64-in-JSON forced by vendor. int16 → base64 at edge.
```

> **TODO(client-protocol — see 09§E):** the client leg is specified as "binary WS +
> opus" only — **no control channel**. Real barge-in (CUT_NOW) needs a client-bound
> `flush/clear` frame to drop already-buffered playout audio, plus playout-position
> reporting back (required for OpenAI `conversation.item.truncate` and for honest
> "what the user actually heard" logging — see 01 TODO(log-truncation-fidelity)).
> Token revoke only stops **server-side** send. Write the client wire protocol:
> framing, control channel, playout clock.

Verified vendor rates: **Gemini in=16kHz / out=24kHz**; **OpenAI 24kHz both**.
(ai.google.dev/api/live; get-started-websocket.)

## The pipeline + rate journey

```
client opus ──decode──► 48k PCM  (opus native, no resample)
   → AudioFrame @48k, source=USER_RAW [from FramePool]
   ├─► [tap] INGRESS_RAW sink
   ├─────────────────────────► RAW fan-out  (consumers that want raw / self-denoise)
   → RNNoise @48k  (ONLY if ≥1 consumer wants clean; 480-sample re-chunker)
   →   AudioFrame @48k, source=USER_CLEAN
   ├─► [tap] POST_CLEAN sink
   └─────────────────────────► CLEAN fan-out (consumers that want clean)

each subscribed consumer's OWN leg (in its own task):
   → [resample IFF vendor_rate != 48k] downsample 48k→16k (Gemini) / 48k→24k (OpenAI)
        (memoized per distinct target rate — two Gemini legs share one 48k→16k)
   → int16 → base64 → JSON → vendor

agent out:  vendor 24k base64 ──► decode ──► [upsample 24k→48k IFF needed]
   → AudioFrame @48k
   ├─► [tap] VENDOR_RX sink   (agent audio)
   → jitter buffer → OutputGate (token) → encode opus → client
```

Two changes from "always clean, always resample at the edge":

1. **Cleaning is per-consumer.** RNNoise runs **only if at least one consumer wants
   clean**; consumers that self-denoise (some Gemini models — 07) or explicitly want
   raw subscribe to the RAW fan-out and skip it. No consumer wants clean → RNNoise
   never runs (CPU saved).
2. **Resampling is lazy + shared.** A consumer's leg resamples **only when its vendor
   rate differs from the 48k interior**, done in that consumer's own task, and
   **memoized per distinct target rate** so N same-rate legs pay one resample. A
   target already at the interior rate pays nothing.

Everything on the shared path stays copy-free at 48k; the per-consumer resample is the
only copy, paid once per distinct rate, only when needed.

## No memory waste — the FramePool

```
FramePool = free-list of fixed-size preallocated int16 slabs.
   acquire() on ingress            → no malloc
   release() after fan-out drained → no GC
   frame.samples = a VIEW (slab, offset, len), not owned bytes
```

Transforms write **in place** or into a pooled sibling — never stage→stage copy.
Fan-out to same-format targets = numpy **views** (zero copy); only cross-rate targets
pay a resample. This is where the density win is earned — no per-chunk Python object
churn (the pipecat/livekit waste).

### Slab lifecycle — recycle, not accumulate (LOCKED)

The pool is **fixed-size and bounded**; slabs **recycle**, they never accumulate. As
the user speaks, each 10ms frame `acquire()`s a **fresh slab** and cycles through the
same preallocated set — memory stays flat. A slab is **freed (returned to the
free-list) the instant its refcount hits 0**, i.e. when **all** of its holders are
done:

```
slab freed  ⇔  active agent released  AND  every subscribed listener released
                (consumed OR drop-oldest evicted)  AND  every sink copied-out + released
```

Continuous arrival is fine: frame N held by a slow listener never blocks frame N+1
(different slab). Steady-state in-flight slabs are **bounded** because every ring
drops-oldest, so:

```
capacity ≥ Σ ring_depths  +  N_consumers (one in-processing each)  +  Σ sink_depths  +  margin
```

(`FramePool.recommend_capacity(...)` computes this.) Sized this way, `acquire()` never
fails. The pool is **per-session**; ~256 slabs × 960 B ≈ 240 KB, ample for one session.

**Overflow policy — caller's call, default DROP_OLDEST.** `acquire()` **keeps raising
`FramePoolExhausted`**; what to do about it is the caller's decision, not the pool's.
Two levers, both provided:

- **Per-ring overflow** (`OverflowPolicy`, per subscriber): a full ring either
  `DROP_OLDEST` (default — evict the stalest buffered frame, keep the newest) or
  `DROP_NEWEST` (refuse the incoming, keep the buffered run).
- **Pool-exhaustion recovery** (ingress, all slabs busy): the **default is drop-oldest
  globally** — `bus.reclaim_oldest()` frees the stalest buffered slab across all rings
  (by `seq`), then ingress retries `acquire`. Newest audio matters most for realtime,
  so the *incoming* chunk is kept and the *oldest* is sacrificed. Drop-newest is the
  alternative (`try_acquire()` → skip the chunk). Either way: bump a metric, emit a
  **discontinuity marker** (STT gap — 09§E `backpressure-per-ring`), never crash, never
  touch user output (separate path).

```
# ingress, default drop-oldest:
try:
    frame = pool.acquire(...)
except FramePoolExhausted:
    if bus.reclaim_oldest():        # free the globally-oldest slab
        frame = pool.acquire(...)   # retry — keep the newest chunk
    else:
        drop_newest(); metric.inc(); emit_discontinuity()
```

Reaching exhaustion at all means every ring is full — usually the pool is **under-sized**
(size with `recommend_capacity`) or a consumer **leaked a `pop`ed ref**. It is a
config/logic signal, handled gracefully, not a crash.

**Detach-release rule (LOCKED):** unsubscribing a listener, demoting an agent, or
closing the session **drains that consumer's ring and releases every slab still in
it**; sink teardown releases likewise. Without this, frames buffered in a dropped
consumer's ring would pin slabs forever (a leak). Detach = release.

> **TODO(framepool-ownership — see 09§E):** `release()` is underspecified. N
> subscriber rings drain at different rates, drop-oldest must also release, and
> sinks copy-then-release — several decrement paths per slab. A missed decrement =
> slab reused while a listener still reads the view = **audio corruption**. Spell
> out the refcount/ownership protocol (who holds, who releases, view lifetime).

## Buffers & gates — flow control ("who gets audio, when")

Three buffer types, two gates.

### Buffers

```
1. FramePool        free-list; no per-chunk alloc/GC.
2. Fan-out bus      TWO producers (USER_RAW and, if anyone wants it, USER_CLEAN) →
                    N bounded subscriber rings. Each subscriber picks its SOURCE
                    (raw or clean, gate 1). Router attaches/detaches subscribers.
                    Drop-oldest on overflow.
                    TODO(listener-context, 09§E): producers = USER audio only →
                    listeners never hear the ACTIVE AGENT. Breaks 05's
                    "context-current listener" claim. Resolve agent-side feed.
3. Jitter+Output    vendor output arrives in BURSTS → jitter buffer smooths to the
                    speaker sample-clock; OutputGate ring is the paced drain.
```

### Gates (the two control points)

```
GATE 1 — INPUT SUBSCRIPTION  (Router-owned)
   decides WHICH agents receive user audio, AND from WHICH source (raw / clean).
   active agent  → ALWAYS subscribed.
   listeners     → subscribed per Router/RoutingPolicy (dynamic).
   each subscription carries: {agent, source=RAW|CLEAN, target_rate}.
   → source drives which producer feeds it; target_rate drives lazy resample.
   → this is the fan-out bus's subscriber set.

GATE 2 — OUTPUT TOKEN  (OutputGate, single writer)
   decides WHICH agent's audio reaches the speaker.
   only the token-holder (active) drains → user.
   TEXT listeners produce no audio to gate (double safety);
   AUDIO listeners DO produce audio → GATE 2 drops it (no token). Single-token
   invariant holds regardless. (Listener modality is per-listener, TEXT or AUDIO —
   not both, not globally text-only. See 05.)
   promotion = token transfer (+ text→audio flip only if the listener was text).
```

Barge-in is **not a third gate** — it's a control action *on* gate 2: VAD detects
user speech over active output → revoke token + flush OutputGate ring + vendor
cancel (the `CUT_NOW` seam). One action.

### Who gets audio, when (the matrix)

```
USER audio (raw or clean, per subscription):
   → active agent        ALWAYS (from its chosen source)
   → listener k          IFF Router subscribed it (gate 1), from its chosen source

AGENT audio (from vendor):
   → speaker             IFF that agent holds the output token (gate 2)
   → else                dropped. TEXT listeners emit no audio; AUDIO listeners
                         emit audio that gate 2 drops (billed but never heard).

SINKS (recording):        independent of both gates — taps observe, never gate.
```

### Backpressure

Audio is realtime — **never block the source**. All rings are **bounded**;
overflow = **drop-oldest** (newest audio matters most for realtime). A slow vendor
socket or slow sink can never stall the mic or the other agents.

> **TODO(backpressure-per-ring — see 09§E):** drop-oldest is correct for **speaker
> playout** only. For **STT-bound** rings (vendor-tx), dropping mid-utterance frames
> silently corrupts recognition with no signal. Use drop-newest at utterance
> granularity / unsubscribe the laggard / send a discontinuity marker instead.

## Pluggable sinks — attach at ANY stage

Recording/observation is a **tap** at a named stage point. Sinks are pluggable and
attachable at **any** stage; zero cost when none attached.

```
Stage tap-points (attach a sink at any):
   INGRESS_RAW   post-decode, pre-clean     → raw user audio
   POST_CLEAN    post-RNNoise               → cleaned user audio
   VENDOR_TX     per-agent, post-resample   → exactly what we send a vendor  (opt)
   VENDOR_RX     per-agent, from vendor      → agent audio
   EGRESS        post-encode, to client      → final client stream          (opt)

AudioSink (Protocol):
   on_frame(frame, stage, agent_id?, meta) -> None    # called with a tap frame

rules:
   - MULTIPLE sinks per stage; a sink may bind to multiple stages.
   - runs OFF the hot path: tap hands frame → sink ring → sink's own task drains.
   - the sink COPIES out of the pooled frame before release (pool reuses the slab);
     that copy is a linear memcpy into the sink's own buffer — cheap, not churn.
   - bounded sink ring, drop-oldest → a slow sink (disk) never stalls audio.
   - NULL by default: no sink attached → tap is a no-op branch, zero cost.
   - sinks OBSERVE, never GATE — recording can't affect routing or the speaker.
```

Built-in sink impls (ship): file writer (per-stream wav/opus), in-memory buffer
(replay), null. Users implement `AudioSink` for anything else (S3, transcription
feed, metrics).

## Cleaner = swappable interface

```
AudioCleaner (Protocol): process(frame@48k) -> frame@48k
   default: RNNoiseCleaner  (48k, 480-sample frames; re-chunker aligns boundaries)
   disable: NullCleaner
   swap:    any rate-native denoiser (own impl)
```

## Constraints flagged

1. **RNNoise 480-sample boundary** — clean stage carries a tiny re-chunker (fixed
   buffer, not per-frame alloc) to hit 480-sample (10ms) frames. Only framing
   constraint the interior imposes.
2. **Low-rate-PCM + no-clean bypass** — fixed 48k interior taxes exactly this case
   (upscale 16k→48k→16k for nothing). The **per-consumer source + lazy resample**
   rules (above) already cover most of it: a consumer that wants RAW and whose vendor
   rate == the client rate pays no clean and no resample. Full bypass (keep the
   interior itself native when *every* consumer is no-clean + one vendor-native rate)
   stays a reserved flag. Default path never forces it.

## Libs (all existing C — fits the no-custom-C rule)

```
opuslib / pyogg  → libopus   (decode/encode, native 48k)
soxr             → libsoxr   (edge resample; high quality + fast)
RNNoise binding  → librnnoise(denoise)
numpy            → int16 buffers / views
base64           → stdlib (edge only)
```

## Where audio does NOT go

Context log = **transcripts only** (locked, 01). Sinks are a **separate** persistence
path. Audio never enters the event log → keeps the log cheap.

## LOCKED

- AudioFrame field set; canonical interior = **48kHz PCM int16 mono**.
- Codec/encoding/vendor-rate at edges only.
- Defaults: opus in/out, RNNoise on **by default but per-consumer** (a consumer may
  take RAW; RNNoise skipped entirely if nobody wants clean), binary frames on client leg.
- **Cleaning is per-consumer** (raw vs clean chosen at subscription; self-denoising
  vendor models take raw). **Resampling is lazy + shared** (only for a subscribed
  consumer whose vendor rate ≠ 48k; memoized per distinct target rate).
- opus = the latency lever; bytes = density lever (vendor leg base64 forced).
- FramePool free-list; transforms in-place/sibling; fan-out via views.
- Two gates: input subscription (Router — picks agent, **source**, target rate) +
  output token (OutputGate). Barge-in = action on gate 2, not a third gate. Bounded
  rings, drop-oldest, never block source.
- Pluggable **AudioSink** at any of 5 tap-points; observes never gates; off hot path;
  null default = zero cost.
- Swappable **AudioCleaner** (RNNoise default).
- soxr / opuslib / RNNoise / numpy.

## Still open (audio)

- 🟡 **Local VAD / barge-in ownership — DEFERRED to a future release.** v1 relies on
  **vendor server-side VAD** (Gemini/OpenAI both detect user speech and interrupt).
  **TODO(v1-vad, 09§E):** deferring this means v1 barge-in = a vendor round-trip =
  no latency edge over competitors, yet 00 names interruption latency as a
  differentiator. Reconcile: promote local VAD into v1 or soften 00's claim.
  Local VAD (~10–30ms, the latency win, avoids the round-trip) is future work; dual-VAD
  fighting is a future concern. The `CUT_NOW` seam mechanism (revoke+flush+vendor
  cancel) stays — v1 just triggers it from the vendor's interrupt signal, not our own VAD.
- 🔴 **Modality flip on promote** — only **text-modality** listeners pay it (text→audio);
  an **audio-modality** listener promotes with no flip (per-listener modality, 05).
  So the lever = pre-warm likely-next as an audio listener (no-flip promote, audio-out
  cost) vs keep it text (cheap, but reconnect-flip on promote).
  **TODO(gemini-modality-flip, 09§E):** the text→audio flip is NOT instant on Gemini —
  `response_modalities` is setup config and Gemini forbids mid-session config change
  (07), so it needs resumption-reconnect or a fresh socket. #1 spike (measures both legs).
- 🟡 **Low-rate-PCM no-clean bypass flag** — reserved, deferred.
