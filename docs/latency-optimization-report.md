# Snail latency optimization report — per-turn TTFB < 100 ms at 10k+ concurrent sessions

**Scope.** Per-turn TTFB = time from *the user stops speaking* to *the first agent audio
byte leaving Snail toward the client*. Third-party latency (Gemini's own generation
TTFT, network RTT to Google) is out of scope; everything below is in Snail's control.
Every estimate is per-turn unless marked otherwise, and each item names the file/line it
targets.

---

## 1. Where the per-turn TTFB actually goes today

Measured against the code as wired in `examples/multi-agent` (the realistic deployment
shape — `VadGeminiAdapter`, `AudioPipeline`, opus client leg):

| Stage | Cost today | In whose control |
|---|---|---|
| Gemini server-VAD end-of-speech wait (`silence_duration_ms=800`, `examples/multi-agent/backend/adapter.py:54`) | **~800 ms** | **Yours** |
| Gemini generation TTFT + WS RTT | ~200–500 ms | Vendor (out of scope) |
| Ingress processing (decode → resample → fan-out → resample → send) | ~0.5–2 ms | Yours |
| Egress jitter prebuffer (`prefill_frames=3` → 30 ms of audio, `src/snail/audio/jitter.py:49`) | 0–30 ms | Yours |
| Egress processing (parse → extract → resample 24k→48k → opus encode) | ~0.5–2 ms | Yours |
| Event-loop queueing + GC pauses **at 10k-session scale** | 0 ms unloaded → **10–100+ ms** overloaded | Yours |

Two conclusions frame everything else:

1. **The 100 ms goal is unreachable while `silence_duration_ms=800` stands.** No amount
   of numpy tuning recovers 800 ms. Section 2 is the recipe for driving it to
   effectively zero without breaking turn-taking.
2. **Unloaded, Snail's own overhead is already ~single-digit ms.** The engineering work
   for "low latency *at tens of thousands of concurrent sessions*" is keeping it that
   way: every microsecond of hot-path CPU is multiplied by ~100 frames/s × N sessions
   per core, and any excess becomes *queueing delay for every session sharing that
   event loop*. Sections 3–5 are therefore ranked by CPU-per-frame and tail-latency
   impact, not just single-session wall clock.

---

## 2. `silence_duration_ms` → 0: the client-side + server-side recipe

### What the parameter does, and why a literal `0` breaks things

With `AutomaticActivityDetection`, Gemini's server-side VAD declares *end of user
speech* only after `silence_duration_ms` of continuous non-speech. It is a debounce: it
exists so a mid-sentence breath doesn't end the turn. Setting it literally to `0` makes
end-of-speech fire on the first non-speech frame — you get mid-utterance turn commits,
the model answering half-questions, and turn thrash. So the goal is not "pass 0 to the
API"; it is **remove the server-side wait entirely and own end-of-turn yourself**, where
you can make it as close to zero as your product tolerates (0 ms for push-to-talk,
~100–200 ms for a local-VAD hangover — versus 800 ms today).

### The recipe: disable automatic VAD, drive activity manually

Gemini supports this natively: `AutomaticActivityDetection(disabled=True)` plus explicit
`activity_start` / `activity_end` markers on the realtime channel. On `activity_end`
Gemini commits the turn **immediately** — zero server-side silence wait. Snail already
has 90% of the plumbing:

* `RealtimeControl.ACTIVITY_START / ACTIVITY_END` exist (`src/snail/vendor/media.py:25`)
  and serialize correctly (`src/snail/vendor/gemini.py:240`).
* `AgentConnection.send_realtime_control` sends them (`src/snail/connections/connection.py:202`).

**Server-side changes needed (small):**

1. **Adapter config.** In your `build_setup` override, set
   `automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)` and
   drop `silence_duration_ms` / sensitivities entirely (they're meaningless once
   disabled).
2. **Wire protocol.** Extend `ControlType` (`src/snail/transport/protocol.py:26`) with
   `ACTIVITY_START` and `ACTIVITY_END` client→server messages, and map them in
   `ClientBridge._handle_control` (`src/snail/transport/bridge.py:172`) to
   `send_realtime_control(RealtimeControl.ACTIVITY_START / ACTIVITY_END)` — exactly the
   pattern the existing `END → AUDIO_STREAM_END` mapping uses.
3. **Local barge-in cut (free latency win).** When `ACTIVITY_START` arrives while the
   agent is speaking, call `pipeline.cut()` + send the client `flush` **immediately**,
   without waiting for Gemini's `Interrupted` round-trip (`bridge._flush_client` already
   does the mechanics, `src/snail/transport/bridge.py:201`). With manual activity,
   sending `activity_start` mid-generation is also what tells Gemini to interrupt — but
   your client-facing cut no longer waits on that RTT. Barge-in latency drops from
   *vendor round-trip (100–300 ms)* to *one client→server hop*. This is the local-VAD
   plan your own docs defer (`docs/claude/09-pending-items.md` TODO(v1-vad)) — manual
   activity is the cheapest way to ship it.

**Client-side changes needed:**

1. **A local end-of-speech decision.** Two options:
   * **Push-to-talk** — `activity_end` on release. True zero added latency.
   * **On-device VAD** — Silero VAD (WASM, ~1 MB, runs in an `AudioWorklet` fine) or
     WebRTC VAD. Emit `activity_start` on speech onset and `activity_end` after your own
     hangover. You choose the hangover: 100–200 ms is the practical floor before natural
     pauses split turns; even 200 ms is a **600 ms saving** over today.
2. **Prefix buffering replaces `prefix_padding_ms`.** With auto-VAD off, Gemini no
   longer back-fills the 300 ms before speech onset. Keep a small rolling buffer
   (~200–300 ms) of mic audio client-side and flush it right after `activity_start`, so
   the first syllable isn't clipped. (Alternative: stream audio continuously and only
   send the markers — Gemini ignores audio outside an activity window, so continuous
   streaming wastes uplink; buffered-flush is cheaper at scale.)
3. **Send `activity_end` *before* the last audio flush completes**, i.e. mark end as
   soon as your VAD fires — don't serialize "drain buffered audio, then end".

**Functionality preserved:** turn-taking (you commit turns explicitly), barge-in
(improved — see above), transcripts, tool calls, resumption — none of these depend on
automatic VAD.

**Fallback if you must keep automatic VAD** (e.g. no client control): set
`silence_duration_ms=100–200` + `END_SENSITIVITY_HIGH`, and lower `prefix_padding_ms`
to ~100. Saves ~600 ms with some risk of turn splits on slow speakers. Do not use `0`.

> **Gain: ~600–800 ms per turn.** This single change is the difference between a
> ~1.2 s and a ~400 ms perceived response, and it is a precondition for the 100 ms goal.

---

## 3. Hot-path CPU: the vendor leg (biggest per-frame costs)

These matter because the receive/send loops run ~100×/s per session; at 10k sessions the
per-frame cost *is* your per-core session capacity, and loop congestion *is* your p99.

### 3.1 Audio is parsed twice per vendor message — eliminate the duplicate

`ClientBridge._on_vendor_msg` calls `adapter.parse_event(raw)` to look for
`Interrupted` (`src/snail/transport/bridge.py:194-199`), then forwards the same raw
message to `Session.on_vendor_raw`, which calls `parse_event(raw)` **again**
(`src/snail/session/session.py:79-82`). The example bridge has the same double parse
(`examples/multi-agent/backend/bridge.py:318-330`). Every field access on a
`LiveServerMessage` is pydantic attribute machinery, so this doubles the most expensive
pure-Python work on the hot path.

Fix: parse once in the bridge and hand the **parsed event list** to the session (add a
`Session.on_events(list[ParsedEvent])` next to `on_vendor_raw`), or move the
`Interrupted → flush` reaction into a session callback. Mechanical change, no behavior
difference.

> **Gain:** ~0.1–0.5 ms CPU per vendor message removed → roughly **halves parse cost**,
> directly adds per-core headroom and shaves loop-queueing tail latency.

### 3.2 Bypass the SDK's pydantic on receive: a raw `LiveTransport` with msgspec

`google-genai`'s `AsyncSession.receive()` does `json.loads` → full
`LiveServerMessage.model_validate` (pydantic v2) for every message — including audio
messages whose payload is a large base64 blob. You already own the perfect seam:
`LiveTransport` is a Protocol (`src/snail/connections/connection.py:50`), and the
connector is injected. Implement a `RawGeminiTransport` that opens the Live WebSocket
itself (the setup message format is stable and documented) and parses inbound frames
with **msgspec** into a minimal typed struct covering only the fields
`parse_event`/`extract_output_audio` read: `serverContent.{modelTurn.parts[].inlineData.data,
inputTranscription, outputTranscription, interrupted, turnComplete}`, `toolCall`,
`goAway`, `sessionResumptionUpdate`. msgspec decodes straight into structs 5–20× faster
than json+pydantic and can decode base64 `bytes` fields natively.

This is the same class of win as "share an HTTPS client to avoid DNS cold starts" — you
stop paying a general-purpose SDK tax on a 100 Hz path. Keep the SDK for
connect/handshake if you like; only the frame codec needs replacing.

> **Gain:** ~0.3–1.5 ms → ~0.02–0.1 ms per inbound audio message. At 10k sessions this
> is the difference between the parse alone saturating ~5–15 cores and it being noise.
> Also removes per-message pydantic allocations → less GC pressure (see 6.2).

### 3.3 Same on send: pre-templated outbound audio frames

`send_realtime_input` builds a pydantic `LiveClientRealtimeInput` + `Blob`
(`src/snail/vendor/gemini.py:221-238` creates a `types.Blob` per 10 ms chunk, plus an
f-string mime type per chunk) and re-serializes JSON+base64 every time. The outbound
audio message is byte-identical except for the base64 payload. With the raw transport
from 3.2, prebuild the frame as two byte fragments:

```python
prefix = b'{"realtimeInput":{"audio":{"data":"'
suffix = b'","mimeType":"audio/pcm;rate=16000"}}}'
await ws.send(prefix + base64.b64encode(pcm) + suffix)
```

Zero object construction, one allocation. Until then, a cheap interim fix inside the
current adapter: cache the mime string per rate, and use `types.Blob.model_construct`
(skips validation).

> **Gain:** ~0.2–0.8 ms → ~0.01 ms per outbound chunk. Multiplied by 100 chunks/s ×
> sessions.

### 3.4 Coalesce drained ingress chunks — one send per client packet, not per 10 ms frame

`AudioPipeline.drain()` returns a **list** of per-480-sample chunks and both bridges
send each one as a separate vendor message
(`src/snail/transport/bridge.py:165-170`, `examples/multi-agent/backend/bridge.py:263-272`).
A client sending 20–40 ms packets triggers 2–4 full WS messages (each with JSON+base64+
pydantic+syscall) where one would do. The chunks are already in hand, so joining them
adds **zero latency**:

```python
chunks = self._pipeline.drain().get(self._conn.id)
if chunks:
    await self._conn.send_realtime(MediaChunk.audio(b"".join(chunks), sample_rate=rate))
```

Gemini accepts arbitrary-length PCM blobs; the 480-sample framing is an interior
(pool/RNNoise) constraint, not a wire one.

> **Gain:** 2–4× fewer outbound vendor messages → proportional cut of the 3.3 cost and
> of WS/syscall overhead. Free.

### 3.5 Disable permessage-deflate on both WebSocket legs

The `websockets` client library (used by google-genai) negotiates permessage-deflate by
default; base64 PCM/opus is nearly incompressible, so deflate burns CPU and adds
per-message latency for nothing — and its per-connection zlib contexts cost real memory
at 10k sockets (~300 KB/connection at default windowBits). Verify with a capture what
your Gemini endpoint negotiates; with the raw transport (3.2) pass `compression=None`
explicitly. On the client leg, uvicorn's `websockets` protocol also enables it — run
with `--ws websockets --ws-per-message-deflate=false` (or the equivalent `Config`
option).

> **Gain:** ~0.1–0.5 ms per message CPU + several GB of RAM at 10k connections + lower
> jitter. One flag on each side.

---

## 4. Hot-path CPU: the audio plane

The plane is already well designed for this goal — pooled slabs (`FramePool`), no locks,
lazy memoized resamplers, msgspec structs. The remaining wins:

### 4.1 Cut or bypass the jitter prebuffer for TTFB

`JitterBuffer(prefill_frames=3)` holds the first 30 ms of a turn's audio before
`PLAYING` (`src/snail/audio/jitter.py:49-57`). Gemini's first burst usually exceeds
30 ms so the wait is often zero — but when the first message is small, prefill adds up
to 30 ms directly to TTFB. Options, best first:

* **First-burst bypass:** start each turn in `PLAYING` and only fall back to
  prebuffering after the first underrun. TTFB pays nothing; steady-state smoothing is
  preserved.
* Or reduce to `prefill_frames=1` (10 ms).

Note the egress drain isn't actually paced today — `_to_client` pops every available
frame immediately (`src/snail/transport/bridge.py:186-192`), so the buffer functions as
a rechunker + prefill gate, and prefill is its only latency contribution.

> **Gain:** 0–30 ms of per-turn TTFB, worst case removed entirely.

### 4.2 Rate-native fast path: skip the 48k interior when it buys nothing

For a single-agent session with no denoise and no fan-out (the overwhelming majority at
scale), the pipeline still does: client decode → resample to 48k → rechunk → pool copy →
ring → resample 48k→16k → `tobytes` on ingress, and 24k→48k resample + opus encode on
egress. The bridge already supports `pipeline=None` passthrough
(`src/snail/transport/bridge.py:156-170`). Make that the default deployment for
single-agent sessions and let the client speak vendor-native rates:

* **Ingress:** client captures/sends 16 kHz PCM (or opus-at-16k decoded server-side) →
  forward bytes untouched.
* **Egress:** ship Gemini's 24 kHz output as-is and let the client's `AudioContext`
  resample (browsers do this natively and for free), or encode opus at 24k.

Wire the full pipeline **only** when a session actually needs fan-out/CLEAN/multi-agent
— the same "if no consumer wants CLEAN, RNNoise never runs" lever, applied one level up.

> **Gain:** ~60–80% of per-session audio CPU for single-agent sessions ≈ 2–4× more
> sessions per core; removes soxr's polyphase group delay (a few ms) from both
> directions.

### 4.3 soxr quality: `HQ` → `MQ`/`LQ` for speech legs

`SoxrResampleBackend(quality="HQ")` (`src/snail/audio/soxr_backend.py:53`) is tuned for
music. Telephony/speech pipelines are indistinguishable at `MQ`, and 16 kHz vendor input
is band-limited anyway. Lower quality = shorter filter = less CPU **and lower intrinsic
filter delay** (HQ's group delay is on the order of a few ms per leg; you pay it twice —
ingress and egress).

> **Gain:** ~2–5 ms total filter delay off TTFB + ~30–50% resample CPU. One constructor
> arg.

### 4.4 Micro-allocations on the per-frame path (small, cheap to fix)

* `_rechunk_raw` allocates a Python list of views + `np.concatenate` for the carry every
  call (`src/snail/audio/pipeline.py:184-197`) — reuse the `Rechunker` pattern from
  `clean.py` (preallocated accumulator) or yield frames without building a list.
* `drain()` builds `dict[str, list[bytes]]` fresh per client packet
  (`src/snail/audio/pipeline.py:115-136`) — with 4.1's coalescing this can return
  `dict[str, bytes]` and skip the inner list entirely.
* `PcmCodec.encode`'s `astype` check and `np.ascontiguousarray` in `drain` are no-ops on
  the common path already — good; keep them.
* `EventLog.events` copies the whole list into a tuple on every access
  (`src/snail/context/log.py:88-90`) — return the list (documented as read-only) or an
  iterator; projections at turn boundaries currently pay O(session length).

> **Gain:** tens of µs per frame each — worth batching into one cleanup pass; together
> maybe 10–20% of remaining plane CPU.

### 4.5 Replace stdlib `json` with msgspec at the edges you still use it

`Session._result_content` uses `json.dumps` (`src/snail/session/session.py:214`) and the
example bridge uses `json.loads`/`json.dumps` for client control/events
(`examples/multi-agent/backend/bridge.py:280,364`). The core transport protocol already
uses msgspec with preallocated encoder/decoder (`src/snail/transport/protocol.py:50-51`)
— extend that pattern everywhere. msgspec is ~5–10× faster and allocates less.

> **Gain:** small per event (~10–50 µs), but tool results and control messages sit on
> the turn boundary — and consistency costs nothing.

---

## 5. Runtime & network engineering for 10k+ concurrent sessions

### 5.1 Process-per-core with `SO_REUSEPORT`, sessions pinned to one loop

Your one-loop-per-session design (docs 06) is right; deploy it as N uvicorn workers = N
physical cores, each `SO_REUSEPORT`-bound so the kernel load-balances accepts, each
worker pinned (`taskset`/cpuset) to its core. No cross-core session migration → warm
caches for the pool slabs and resampler state. Size to keep **per-loop audio CPU under
~50–60% of the core** — beyond that, queueing delay grows nonlinearly and eats the
latency budget. With sections 3–4 done, a realistic budget is ~50–150 µs/frame/session
→ **500–1500 sessions per core**, so 10k concurrent ≈ 8–20 cores of audio work.

### 5.2 uvloop

Swap the default asyncio loop for uvloop (`uvicorn --loop uvloop`). 2–4× faster loop
internals (task switching, WS I/O, timers) — this is pure tail-latency and capacity, no
code change. Verify the wheel supports your Python 3.14 pin; if not, this alone is worth
running 3.13 for until it does.

> **Gain:** ~20–40% of event-loop overhead ≈ 10–30 ms off p99 under load.

### 5.3 GC discipline — the hidden p99 killer

CPython's generational GC scans all tracked objects; at 10k sessions × (events, frames,
tasks, pydantic models) a gen-2 collection is a **10–100 ms stop-the-world pause** — a
whole latency budget, gone, for every session on that worker. Do all of:

1. `gc.freeze()` after startup/warm-up (moves long-lived objects out of scanning).
2. Raise thresholds: `gc.set_threshold(50_000, 50, 100)` — with pooled frames and
   msgspec structs your allocation rate is low; frequent gen-0 scans buy nothing.
3. Prefer explicit lifecycle over cycles: the codebase is already mostly acyclic
   (msgspec structs, `__slots__`). The pydantic removal in 3.2/3.3 is the single biggest
   allocation cut.
4. Optionally schedule `gc.collect(1)` during idle ticks per worker, so collections
   happen when you choose.
5. Cap the `EventLog`: it grows unbounded per session (`src/snail/context/log.py`); for
   long sessions spill old events to disk/object storage past a few thousand — memory
   pressure degrades cache hit rates and lengthens GC scans.

> **Gain:** removes multi-10-ms p99/p999 spikes; the difference between "under 100 ms
> p50" and "under 100 ms p99".

### 5.4 Connection pool sizing — take the handshake off the join path

The Gemini WS handshake is ~100–300 ms. `create_app` builds **one shared pool** with
`max_warm=8` (`src/snail/transport/server.py:44-52`) — at 10k sessions, effectively
every acquire is a cold connect. This is join latency (first-turn TTFB), not per-turn,
but it's the same UX budget:

* Raise `max_warm` to your expected concurrent-join rate × handshake time (e.g. 50
  joins/s × 0.3 s ≈ 16 warm minimum; give it headroom).
* Prewarm predictively: refill the standby bucket asynchronously whenever an acquire
  drains it (a background task per pool key), instead of only at startup.
* Share one `genai.Client`/connector per process (already done — keep it), and reuse a
  single `ssl.SSLContext` across connects so TLS session resumption skips a round-trip
  on every new vendor socket. Warm DNS: resolve the Gemini endpoint once per process and
  keep a local caching resolver so a DNS blip never lands on a connect path.
* `_evict_one` is an O(all standbys) scan per over-cap acquire
  (`src/snail/connections/pool.py:201-210`) — with a big pool, keep a heap by
  `last_activity` or accept the scan consciously.

> **Gain:** first-turn TTFB drops by the full handshake (~100–300 ms) for every
> pooled hit; recycle already keeps mid-session swaps off the path.

### 5.5 Client leg: bytes on the wire

* **Opus, not PCM, for both directions** — already built (`OpusCodec`,
  `src/snail/audio/opus_codec.py`). At 48k PCM16 is 768 kbps/direction; opus VoIP is
  ~24 kbps: at 10k sessions that's **~7.7 Gbps → ~0.25 Gbps** egress. Less bandwidth is
  also less kernel time, smaller send buffers, fewer bufferbloat-induced delays for the
  same audio.
* **Binary control frames**: the msgspec JSON control channel is fine (low rate), but if
  you ever put per-frame metadata on the wire, prepend a 1-byte type tag to binary
  frames instead of a text channel — you already reserved that door
  (`src/snail/transport/protocol.py` "a codec/header can be layered later").
* **`TCP_NODELAY`** on every socket (client leg and vendor leg). uvicorn sets it for
  HTTP; verify for WS upgrades, and set it explicitly in the raw transport (3.2). A
  Nagle stall is 40 ms — nearly half the budget.
* Kernel: raise `net.core.somaxconn`, `fs.file-max`/ulimit for 10k+ FDs; modest
  `SO_SNDBUF` on the client leg (large buffers = queued stale audio = perceived
  latency; you want backpressure to reach the drop-oldest ring, not hide in the kernel).
* Colocate workers in the same cloud region as your Vertex/Gemini endpoint — the vendor
  RTT itself is out of scope, but *where you deploy* is in your hands and worth
  20–100 ms of every single turn.

### 5.6 Egress send coalescing

`_to_client`/`on_audio` awaits `send_bytes` per 10 ms frame in a loop
(`src/snail/transport/bridge.py:186-192`). Send the **first** frame immediately (that's
TTFB), then batch the remainder of the burst into one or a few larger sends. Cuts
syscalls/WS framing ~5–10× on egress with zero TTFB cost.

---

## 6. Data-structure notes (what's already right, and the few upgrades)

* **`FramePool` slab design is the correct call** — contiguous `(capacity, slab)` int16
  backing, refcounts in a numpy array, LIFO free-list for cache warmth
  (`src/snail/audio/pool.py:56-67`). Two µ-upgrades: store refcounts in a plain Python
  `list[int]` (scalar numpy element access `self._refcount[idx]` boxes on every
  incref/release — a list is faster for scalar ops), and consider `array("h")` only if
  you ever drop numpy — otherwise keep as-is.
* **Rings as `deque`** (`SubscriberRing`, `OutputGate`, jitter chunks) — right structure
  for FIFO push/pop at these depths; nothing faster in pure Python.
* **msgspec Structs everywhere** (`AudioFrame`, `Control`, `MediaChunk`) — already the
  fastest attribute-access objects in CPython; keep extending this to the parsed vendor
  events (they're plain classes today — `src/snail/vendor/events.py` — fine, but
  msgspec structs with `gc=False` would take them out of GC tracking entirely, which
  compounds with 5.3).
* **Jitter `_take` fast path** already avoids concatenation when a frame fits one chunk
  (`src/snail/audio/jitter.py:122-133`) — good. Optional: a preallocated ring array
  (one `np.empty(N)` per session, head/tail indices) removes the per-burst array
  retention, but the deque version is close enough that this is low priority.
* **`schema.validate`** is a hand-rolled minimal validator (`src/snail/tools/schema.py`)
  — much faster than jsonschema; keep it. If tool-call rate ever matters, precompile
  per-tool validators (closure specialization) instead of re-walking the schema dict.

---

## 7. Priority order and expected gains

| # | Optimization | Per-turn TTFB gain | Scale/tail gain | Effort |
|---|---|---|---|---|
| 1 | Manual activity (VAD off) + client end-of-speech (§2) | **600–800 ms** | — | Medium (client + small server) |
| 2 | Local barge-in cut on `ACTIVITY_START` (§2.3) | 100–300 ms on barge-ins | — | Small |
| 3 | Jitter first-burst bypass (§4.1) | 0–30 ms | — | Small |
| 4 | soxr HQ→MQ (§4.3) | ~2–5 ms | 30–50% resample CPU | Trivial |
| 5 | Kill double `parse_event` (§3.1) | ~0.1–0.5 ms/msg | ~2× parse headroom | Small |
| 6 | Raw msgspec `LiveTransport` in+out (§3.2, §3.3) | ~0.5–2 ms/msg | **5–20× vendor-leg codec CPU**; big GC cut | Medium |
| 7 | Coalesce ingress sends (§3.4) + egress sends (§5.6) | 0 (free) | 2–4× fewer vendor msgs; 5–10× fewer egress syscalls | Small |
| 8 | Disable permessage-deflate both legs (§3.5) | ~0.1–0.5 ms/msg | CPU + GBs of RAM | Trivial |
| 9 | Rate-native passthrough default for single-agent (§4.2) | few ms (filter delay) | **2–4× sessions/core** | Small (wiring) |
| 10 | uvloop + worker-per-core + pinning (§5.1, §5.2) | — | 10–30 ms off loaded p99 | Small |
| 11 | GC freeze/thresholds + EventLog cap (§5.3) | — | removes 10–100 ms p99 spikes | Small |
| 12 | Pool sizing + predictive prewarm + shared SSLContext (§5.4) | 100–300 ms off **first** turn | join-storm resilience | Small–Medium |
| 13 | Opus client leg as default (§5.5) | ~ms (buffering) | ~30× bandwidth | Already built |
| 14 | Micro-alloc cleanups + msgspec-everywhere (§4.4, §4.5) | µs each | ~10–20% residual CPU | Small |

**Resulting budget** (loaded, p50 → p99), with items 1–11 done:

* end-of-speech decision: 0–200 ms (your hangover; 0 for PTT) — *was 800*
* Snail ingress + vendor send: < 1 ms
* [vendor generation — out of scope]
* Snail receive + egress to first client byte: 1–3 ms, no prefill wait
* loop queueing + GC at 10k sessions: < 10 ms p99

i.e. **Snail-attributable per-turn TTFB ≈ 5–15 ms p99** — comfortably inside 100 ms,
with the remainder of the user-perceived time being vendor generation and physics.

---

## 8. Measure before/after

Add a per-turn timing record (the hooks are all in code you own):

1. `t0` client `activity_end` sent (client clock, echoed in the control message);
2. `t1` bridge receives it; `t2` vendor send completes;
3. `t3` first vendor audio message received (`AgentConnection.run`, before `on_audio`);
4. `t4` first `send_bytes` to the client returns.

Export histograms (p50/p95/p99) per stage plus `FramePool.stats`, `JitterBuffer.stats`
(`underruns_total`), `OutputGate.stats`, pool `exhausted_total`, and per-loop lag (a
watchdog task that measures `await asyncio.sleep(0)` scheduling delay). Loop lag is the
single most honest "are we overloaded" signal at 10k sessions — alert when p99 lag
exceeds ~10 ms and shed load (refuse new sessions on that worker) before latency
collapses for everyone.
