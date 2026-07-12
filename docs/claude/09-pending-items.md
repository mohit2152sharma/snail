# Pending Items

Status legend: 🔴 open decision · 🟡 deferred feature · 🔵 validation TODO ·
🟢 lean exists (not locked)

## A. Deferred features (punted; architecture ready)

- 🟡 **Batch-completion logic** — turn continuation when N parallel tool calls all
  resolve (`by_response_group`). Structure in place; logic deferred.
- 🟡 **`deferred` status / async late-resolve** — full late-result path. Registry
  entries are built as futures so it slots in. Native on Gemini 2.5 Live (Dev API
  only); emulated on Vertex + OpenAI.
- 🟡 **`will_continue` streaming tools** — Gemini generator tools (multiple
  responses per call). Noted for later.
- 🟡 **Cross-user generic-transport pooling** — pre-auth sockets shared across
  sessions. Maybe never; only if profiling proves the handshake is the bottleneck.
- 🟡 **Two-tier pool** — coarse model-keyed pool for OpenAI. Pool key is
  vendor-supplied so it's a later config flip, not a rewrite.

## B. Open decisions (resolve at their subsystem)

### Audio — LOCKED (see 11). Remaining action items:
- 🟡 **Local VAD / barge-in ownership — DEFERRED to a future release.** v1 uses
  vendor server-side VAD to trigger the CUT_NOW seam. Local VAD (~10–30ms, the perf
  win) + dual-VAD arbitration = future work.
- 🔴 **Modality flip on promote (the seam's real cost)** — **text-modality** listener
  → audio: instant on the vendor, or pre-warm the likely-next listener as
  audio-modality (no flip)? Only text listeners pay this; audio listeners promote with
  no flip. Was the deferred "audio-flow manager"; now scoped to this one flip.
- 🟡 **Low-rate-PCM no-clean bypass flag** — reserved; skip the 48k interior when
  no-clean + single-vendor-native-rate. Default path never hits it.

### Router — LOCKED (see 05). Remaining action items:
- 🟢 **RulePolicy predicate surface — LOCKED (see 05).** Ordered
  `Rule(predicate, decision)`; declarative `{field, op, value}` default + callable
  escape hatch; reads only the RoutingSignal; listener→active = a rule targeting a
  listener spec; text match = intent hint, never authority. Remaining: freeze the
  exact `op` set (small) — currently `{==,!=,>,<,>=,<=,~=,contains,in,and,or,not}`.
- 🔵 **LLMRouterPolicy async round-trip** — how the async result re-enters as a
  programmatic signal without racing a newer turn; only when someone opts in.
- 🟢 **Fan-out cost cap** — config number (max listeners/session).
- 🟢 **Seam default text/filler** — the ~20–50ms duck/crossfade + any "one moment"
  filler wording on CUT_NOW.

**Resolved by the lock:** seam atomicity → `seam` field (CUT_NOW/AT_TURN_END/AT_IDLE);
decision precedence → `ChainPolicy` order; demoted-active fate → demote-to-listener;
LLM-router-as-default → rejected (opt-in policy off the hot path).

### Other
- 🔴 **Canonical Event / Item schema** — exact fields for lossless dual-vendor
  serialization. Event *types* listed; full field schema not written.
- 🟢 **speak_directive framework defaults** — actual default text per status
  (error/blocked/timeout/…). Reason strings are sufficient (no code field).
- 🟢 **Async library** — lean = anyio + uvloop (see 06). Not formally locked.
- 🔴 **Reject/reroute exact reason strings** — model-facing text per status
  (deferred; reason strings deemed sufficient overall).

## C. Validation TODOs (spikes before load-bearing)

- 🔵 **Gemini 2.5 history-injection spike** — confirm setup `system_instruction` +
  `user`-role history injection **before first model turn** on
  `gemini-2.5-flash-live`, both Dev API and Vertex.
- 🔵 **Pipecat benchmark harness** — verify performance claims early; don't trust
  our own numbers. Build later.

## D. Recycle — two paths (added; to detail)

- 🔴 **Assigned vs unassigned recycle** — recycling a connection **assigned to a
  client** (has JoinContext; must restore context via native resume or log-replay,
  time-sensitive, invisibility depends on active/listener role) differs from
  recycling an **unassigned pool standby** (SetupParams only, no client bound;
  recycle = fresh socket + re-apply SetupParams, trivial, background housekeeping).
  Both age toward vendor max-duration and must be recycled. To be fully specified.

## E. Design-review TODOs (open — raised 2026-07-12, unresolved)

Critique against the 00 goals (single-face, vendor-neutral, density/cost,
barge-in/handoff latency). Ordered by severity. Each has an inline anchor in the
named doc.

### Critical — listener / handoff story (the core novelty)

- 🔴 **TODO(listener-context): listeners never hear the active agent.** Fan-out
  bus produces **only cleaned user audio** (11) → a listener hears **half** the
  conversation. 05 claims a promoted listener is "context-current, no replay" — it
  never heard the active agent's turns. Load-bearing claim of the architecture.
  Decide + document how a listener stays current with **agent-side** turns (stream
  agent transcripts in as `user`/`model` turns? projection replay on promote?).
  Anchors: 05 (§"kills the handoff gap"), 11 (fan-out bus).
- 🔴 **TODO(listener-divergence): a listener's own vendor session diverges from
  reality.** A listener (text **or** audio) *responds* every turn → those `model` turns
  accumulate in ITS session, a parallel conversation the user never heard. Promote
  it → it may cite things "it said" that were never spoken. Log-replay fixes it but
  05 claims promotion needs **no** replay. Pick: fast-but-schizophrenic vs
  correct-but-replayed; document the trade. Anchor: 05 (§promotion).
- 🔴 **TODO(gemini-modality-flip): text→audio promotion = reconnect on Gemini.** 07
  verifies "cannot update configuration while connection is open";
  `response_modalities` is setup config → a **text→audio** flip needs
  resumption-reconnect or fresh socket. Note the flip only bites **text-modality**
  listeners; an **audio-modality** listener promotes with **no flip** (listener
  modality is per-listener, TEXT or AUDIO — see 05). So "atomic token-transfer
  promotion" holds for **OpenAI**, and for **audio-modality listeners on Gemini** (at
  audio-out cost). This is the **#1 spike** (above history-injection). Options to
  measure: (a) keep likely-next listener in audio modality, discard audio (costs
  audio-out tokens — quantify) → no-flip promote; (b) resumption-based flip of a text
  listener (measure latency); (c) accept replay-promote on Gemini. Supersedes the
  existing "modality flip on promote" item in §B. Anchors: 05, 07, 11 (🔴 modality flip).
- 🔴 **TODO(listener-economics): quantify listener cost + quota.** 05 rejects the
  LLM router as "a third always-on socket billing every turn" — but a subscribed
  listener is exactly that (audio-in tokens + generation, per turn, per listener) and
  consumes vendor concurrent-session quota (Gemini Live quotas are tight). Two-tier
  cost: a **TEXT** listener bills audio-in + text-gen; an **AUDIO** listener also
  bills **audio-out** for audio nobody hears (the price of no-flip promotion). Put
  numbers in 00/05: cost per listener-hour per vendor **per modality**;
  sessions-per-user vs quota.
  Anchors: 00 (performance framing), 05 (listener cost).

### Critical — barge-in claims vs v1 reality

- 🔴 **TODO(v1-vad): v1 barge-in = vendor-VAD round-trip = no differentiator.** 00
  names interruption latency as a thing Snail "can genuinely beat competitors on";
  11 defers local VAD → v1 ships **without** the differentiator. Either promote
  local VAD into v1 or soften 00's claim. Anchors: 00, 11 (🟡 local VAD).
- ✅ **RESOLVED(client-protocol): `snail.transport` defines the client wire protocol.**
  Binary=media (PCM16LE v0), text=JSON `Control` control channel with a client-bound
  `FLUSH` (real CUT_NOW) + `PLAYOUT` position reporting → `PlayoutClock` buffered-ahead
  accounting (feeds OpenAI `item.truncate` + honest "what the user heard"). `create_app`
  (FastAPI/uvicorn) owns pool lifecycle: server up→pool up, server down→`pool.aclose()`.
  Spec in 11 §Client wire protocol. Still open: opus codec on the client leg (v0=raw PCM),
  jitter buffer, soxr resample.

### Medium

- 🟡 **TODO(log-truncation-fidelity): barge-in log fidelity.** Model generated a
  full sentence; user heard 40%. Which does the log record? Projections + promoted
  listeners inherit the answer. OpenAI has `item.truncate`; Gemini doesn't. Anchor:
  01 (context is transcripts).
- 🟡 **TODO(log-completeness): transcript timing + replay cost + compaction.**
  User-speech transcripts are vendor-supplied (async, lossy, late) → log-replay may
  replay a hole. OpenAI recycle **re-bills full context** + costs wall-time
  mid-session (02 says "heavier," no masking budget). No compaction/summarization
  policy → long session overruns the vendor context window; `last_n` is truncation,
  not policy. Anchors: 01, 02 (recycle).
- 🟡 **TODO(framepool-ownership): FramePool refcount protocol unspecified.** N
  subscriber rings drain at different rates; drop-oldest must also release; sinks
  copy-then-release. Several decrement paths per slab → a miss = reuse-while-read =
  audio corruption. Spell out the ownership/refcount protocol — the hardest
  correctness problem in the audio plane. Anchor: 11 (FramePool).
- 🟡 **TODO(backpressure-per-ring): drop-oldest is wrong for vendor-bound audio.**
  Fine for speaker playout; for STT ingestion, dropping mid-utterance frames
  silently corrupts recognition with no signal. Prefer drop-newest at utterance
  granularity / unsubscribe the laggard / discontinuity marker. "Newest matters
  most" holds for playout only. Anchor: 11 (backpressure).
- 🟡 **TODO(offload-threshold): per-frame threadpool offload hurts.** RNNoise/soxr
  on a 10ms frame = microseconds; a thread hop costs more + adds jitter. Add a
  threshold: inline small per-frame DSP on the loop, offload only heavy/batch work.
  Anchor: 06 (offload rule).
- 🟢 **TODO(chain-default-order): ship a sane ChainPolicy default.** Default
  `ControlTool → Rule → Programmatic → LLM` lets the model's `transfer_to` beat an
  explicit app/backend decision — backwards. Put Programmatic first in the shipped
  default. Anchor: 05 (built-in policies).

### Missing docs (gaps, not flaws)

- 🔴 **TODO(doc-public-api): public API / DX doc.** All 12 docs are internals. No
  hello-world: declare a session, agents, tools, policies, run a server. For a
  framework this *is* the product; writing it will pressure-test every abstraction.
  **Highest-value next doc.**
- 🔴 **TODO(doc-transport): transport / server story.** WS+opus assumes we own the
  client. No client SDK, no telephony (Twilio), no WebRTC; raw WS from browsers has
  no congestion/jitter story — exactly where pipecat/livekit earn their keep. Even
  "bring your own transport behind interface X" is a decision worth writing.
- 🟡 **TODO(doc-failure): failure / degradation.** Unexpected mid-utterance socket
  death (not GoAway), vendor outage. Note: this architecture makes **cross-vendor
  failover** (Gemini dies → OpenAI listener promotes) nearly free — a marketable
  goal nobody wrote down.
- 🟡 **TODO(doc-test-strategy): test strategy + MockVendorAdapter.** Deterministic
  seam/router/registry tests need a scripted mock vendor. Cheap to spec now,
  painful to retrofit.

## Locked (for contrast — see other docs)

- Vision/goals, performance framing, vendor scope.
- Append-only log + snapshot projections; two-mode projection API.
- AgentSpec ≠ AgentConnection; per-AgentSpec pool; pre-warm/park/keepalive/recycle/
  evict/admission.
- SetupParam vs JoinContext (system_instruction + tools = setup, both vendors).
- Tool layer: common-denominator schema, stateless reusable Tool, exposure ≠
  authority, intent-not-command, handoff = control tool, output_schema required,
  ToolResult envelope + status taxonomy + speech directives.
- ToolCallRegistry: form, lifecycle FSM, cancel/timeout, single-resolution
  invariant, boundaries.
- Concurrency: GIL build, one loop per worker process, lockless registry, offload
  CPU, anyio + uvloop lean.
- Router #7: one active + silent listeners, input fan-out, single OutputGate,
  per-listener modality (TEXT or AUDIO, not both; a session mixes them),
  listeners-kill-the-handoff-gap.
- Router: mechanism-vs-decision split; `RoutingPolicy.decide → decision|None`
  (trigger-driven, advice not command); built-in policies (ControlTool/Rule/
  Programmatic/LLMRouter/Chain); precedence = ChainPolicy order; no default LLM
  router; three seam modes (CUT_NOW/AT_TURN_END default/AT_IDLE); token-revoke +
  vendor-cancel separate; single-stream invariant; demote-to-listener.
- Audio: AudioFrame (msgspec, int16 view); 48kHz PCM mono canonical interior;
  opus in/out + RNNoise default; resample vendor-edge only; codec=latency lever,
  bytes=density lever (vendor leg base64 forced); FramePool free-list; two gates
  (input subscription + output token), bounded/drop-oldest; pluggable AudioSink at
  5 tap-points (observe never gate); swappable AudioCleaner; opuslib/soxr/RNNoise.
- Observability: Observer layer = operational telemetry (Metric|Event), tap-anywhere,
  distinct from event log (never load-bearing); off hot path, observe never gate,
  zero-cost guarded emission; PUSH events vs SAMPLED-aggregate metrics; separate from
  AudioSink; built-ins Logging/Exporter/InMemory/Null.
