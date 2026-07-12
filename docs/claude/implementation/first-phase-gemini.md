# First Phase — Gemini-Only Implementation Plan

**Status:** implementation plan (design intent → shipped code).
**Scope of this phase:** integrate **Gemini Live** as the *only* vendor. Build the
full vendor-neutral architecture (the whole point of Snail), but wire exactly one
`VendorAdapter` behind it. **OpenAI Realtime is deferred** to a later version and
tracked in [§9 OpenAI deferral list](#9-openai-deferral-list-later-version).

This plan operationalizes the locked designs in `00`–`13`. It does **not** re-open
locked decisions; it sequences them into shippable increments and pulls the open
[`09§E`](../09-pending-items.md) review-TODOs into the phase where they must be resolved.

---

## 1. Phase scope & non-goals

### In scope (Gemini only)
- Target model: **`gemini-2.5-flash-live`** only (ignore 2.0 / 3.1). See `07`.
- Backend: **Gemini Developer API is the primary target** — it has native
  `Behavior.NON_BLOCKING` async tool calls *and* native `session_resumption`.
  **Vertex AI** is wired as a second capability profile but async tools fall back
  to emulation there (issue #1739), so Vertex is *build-behind-a-flag, verify-later*.
- The complete neutral stack: append-only event log → projections → `Item[]` hard
  boundary → `VendorAdapter`; connection pool; audio plane; tool layer +
  ToolCallRegistry; Router + RoutingPolicy; observability; client transport.
- Single active agent + N silent listeners **topology**, built end-to-end against Gemini.

### Explicit non-goals this phase
- **No OpenAI Realtime adapter** (§9).
- **No local VAD.** v1 uses Gemini **server-side VAD** to trigger the `CUT_NOW`
  seam (locked deferral, `11`/`09§E TODO(v1-vad)`). The seam mechanism ships; the
  low-latency local-VAD trigger does not.
- **No cross-vendor failover** (needs a 2nd vendor — §9).
- **No two-tier / model-keyed pool**, no cross-user socket sharing (`09§A`).
- **No compaction/summarization policy** beyond `last_n` truncation (`09§E
  TODO(log-completeness)` — tracked, not built this phase; long-session overrun is
  a known limitation of v1).

### The single most important consequence of "Gemini only"
Gemini **forbids mid-session config updates** (`07`, verified). Two locked-design
claims that assumed a free mutation are therefore **not free on Gemini** and become
the two gating spikes of this phase:

1. **Modality flip on promotion** (text-listener → audio-active). `response_modalities`
   is *setup* config → the flip needs a resumption-reconnect or a fresh socket.
   `09§E TODO(gemini-modality-flip)` — the **#1 spike**.
2. **History injection** must happen at setup / before first model turn (no late
   system turns) — `09§C` spike.

Because Gemini is the *only* vendor here, these are not "one branch of a matrix" —
they define what the promotion hot-path actually looks like in v1. Phase 0 resolves
them before any load-bearing code is written on top.

---

## 2. Architecture recap (what we're building toward)

```
client (WS + opus)
   │  audio in                                          audio out │
   ▼                                                              ▲
┌─────────────────────────── Audio Plane (11) ───────────────────────────┐
│ opus decode → 48k AudioFrame [FramePool] → RNNoise → FAN-OUT bus        │
│   GATE 1 (input subscription, Router-owned)                             │
│   → vendor edge: 48k→16k, int16→base64→JSON  ─────────────► Gemini WS   │
│ Gemini 24k base64 ► decode ► 24k→48k ► jitter ► GATE 2 (OutputGate) ►   │
│   opus encode → client                                                  │
└─────────────────────────────────────────────────────────────────────────┘
        ▲ transcripts/events                          ▲ decisions
        │                                             │
┌───────┴─────────┐   projection   ┌─────────────┐   │   ┌──────────────┐
│ Event Log (01)  │───────────────►│  Item[] ──►  │───┼──►│ VendorAdapter│
│ append-only     │   Item[] hard  │  (neutral)  │   │   │  = Gemini    │
└─────────────────┘   boundary     └─────────────┘   │   └──────────────┘
                                                      │
   ┌──────────────── Router / Arbiter (05) ───────────┴─────────────┐
   │ OutputGate (token) · input fan-out · RoutingPolicy (decide)    │
   │ ToolCallRegistry (04) · seam (CUT_NOW/AT_TURN_END/AT_IDLE)     │
   └────────────────────────────────────────────────────────────────┘
```

See [`13-mermaid-diagrams.md`](../13-mermaid-diagrams.md) for the full LLD set and
control flow, and [`lld-presentation.html`](../lld-presentation.html) for the live
zoomable deck.

### Proposed package layout (`src/snail/`)
```
snail/
  context/        Event, EventLog, Projection (spec + builder), Item        (01)
  vendor/
    base.py       VendorAdapter Protocol, capability descriptor             (07)
    gemini.py     GeminiAdapter (Dev API + Vertex profiles)                 (07)
    mock.py       MockVendorAdapter (deterministic tests)      09§E TODO(doc-test-strategy)
  connections/    AgentSpec, AgentConnection, Pool, ConnectionLifecycleManager (02)
  audio/          AudioFrame, FramePool, resample, cleaner, fanout, gates, sinks (11)
  tools/          Tool, ToolRegistry, ToolResult, schema policy             (03)
  registry/       ToolCallRegistry (in-flight FSM)                          (04)
  router/         Router, OutputGate, RoutingPolicy + built-in policies, seam (05)
  transport/      client WS server, framing, control channel        09§E TODO(client-protocol)
  observability/  Observer, Metric/Event, sinks                            (12)
  session/        top-level orchestration (wires everything per user-session)
```

---

## 3. De-risking spikes (Phase 0 — do first, throwaway allowed)

These validate vendor facts before we build load-bearing code on them. Each spike is
a small standalone script against a real Gemini key; output = a written finding in
`09` (flip the relevant 🔵/🔴 to resolved) plus a go/no-go on the dependent design.

| # | Spike | Question | Resolves | Blocks |
|---|-------|----------|----------|--------|
| S1 | **Modality flip / promotion** | Cheapest way to turn a text-only Gemini session into an audio-speaking one. Measure: (a) run listener in audio modality + discard audio (audio-out token cost); (b) `session_resumption` reconnect with modality change (latency ms); (c) fresh-socket + log-replay (latency ms). | `09§E TODO(gemini-modality-flip)`, `09§B modality-flip`, `11 🔴` | Phase 3 promotion path |
| S2 | **History injection** | Confirm setup `system_instruction` + `user`/`model` history turns land **before first model turn** on `gemini-2.5-flash-live` (Dev API **and** Vertex). | `09§C history-injection` | Phase 1 join path |
| S3 | **Session resumption / recycle** | `session_resumption` handle round-trip; `GoAway`+`timeLeft` timing; max-duration + idle-timeout values. | `02` recycle numbers, `09§D` | Phase 4 recycle |
| S4 | **Native async tools** | `Behavior.NON_BLOCKING` + `FunctionResponseScheduling` on Dev API Live; confirm it no-ops on Vertex (#1739). | `07` async cell | Phase 2 async path (deferrable) |
| S5 | **Server-side VAD / barge-in signal** | What interrupt signal Gemini emits on user speech-over-agent, and its latency; confirm `CUT_NOW` can be driven from it. | `09§E TODO(v1-vad)` scope | Phase 1 barge-in |
| S6 | **Client control channel** | Prototype the client-bound `flush/clear` frame + playout-position report needed for a *real* cut (server revoke alone leaves buffered playout). | `09§E TODO(client-protocol)` | Phase 1 transport |

**Gate:** S1, S2, S5, S6 must have findings before Phase 1 exits. S3/S4 can trail into
Phase 4/2 respectively but are scheduled here so surprises surface early.

> The critical honesty check lives in S1 + S5: they decide whether v1's promotion is
> "atomic token transfer" (OpenAI's story, **not** achievable on Gemini without a
> reconnect) and whether barge-in beats a vendor round-trip. Findings feed the `00`
> positioning TODO — soften the latency claim or fund the fix. **Do not let marketing
> copy outrun what S1/S5 prove.**

---

## 4. Phase 1 — single-agent vertical slice (one Gemini agent, end to end)

**Goal:** one user talks to one Gemini agent through Snail's own stack — audio in,
audio out, no overlap primitive stressed yet, one agent only. Proves the neutral
boundary and the audio plane against a real vendor.

**Build:**
- `context/`: `Event`, `EventLog` (append-only, msgspec, slotted), `Projection`
  Mode-1 declarative spec + `Item`. Freeze the **canonical Event/Item field schema**
  now (`09§B` open decision — the vertical slice forces it).
- `vendor/base.py` + `vendor/gemini.py`: `VendorAdapter` protocol; `GeminiAdapter`
  serializing `Item[]` → Gemini setup + `user`/`model` turns; parsing Gemini events
  → neutral events. **Dev API profile first.**
- `connections/`: `AgentSpec`, `AgentConnection` (connect → warm → active → closed),
  lazy connect only (pool comes in Phase 4). `SetupParam` (model/voice/instruction/
  tools bound at setup) vs `JoinContext` (history/facts on join) — per S2 finding.
- `audio/`: `AudioFrame`, `FramePool`, opus decode/encode (opuslib), soxr edge
  resample (48k↔16k in / 48k↔24k out), RNNoise cleaner + 480-sample rechunker,
  base64 vendor edge. **Single consumer** (no fan-out yet), single `OutputGate`
  drain (trivial one-holder case).
- `transport/`: client WS server, opus binary frames, **plus the control channel
  from S6** (flush/clear + playout clock) — build it now, not later; barge-in needs it.
- `observability/`: `Observer` with null default, a couple of core metrics/events.
- `session/`: wire one agent, one client.

**Barge-in (server-VAD path):** trigger `CUT_NOW` from Gemini's interrupt signal
(S5): revoke OutputGate token + flush ring + send client flush frame + Gemini
turn/activity cancel. Local VAD stays deferred.

**Must resolve here:**
- `09§E TODO(client-protocol)` — write the client wire protocol (framing, control
  channel, playout clock). **Now load-bearing** because a cut that doesn't clear
  client playout isn't a cut.
- `09§E TODO(framepool-ownership)` — even single-consumer, spell out `release()`
  refcount + view lifetime *before* fan-out multiplies the decrement paths in Phase 3.
- `09§E TODO(offload-threshold)` — inline the 10ms RNNoise/soxr DSP on the loop;
  don't thread-hop per frame (`06`).

**Exit criteria:** a human holds a spoken conversation with one Gemini agent through
Snail; barge-in cuts the agent mid-sentence (server-VAD) with the client actually
going silent; audio is clean; no per-frame allocation in steady state (verify via a
FramePool counter); the event log is a faithful transcript.

---

## 5. Phase 2 — tools & registry

**Goal:** the single agent can call tools and get results back, with the full
lifecycle/authority model.

**Build:**
- `tools/`: `Tool` (stateless, reusable), `ToolRegistry`, common-denominator schema
  policy, `ToolResult` envelope + status taxonomy + speech directives (`03`).
- `registry/`: `ToolCallRegistry` in-flight FSM, single-resolution invariant,
  cancel/timeout sweeps (`04`). Router owns it (execution authority).
- `vendor/gemini.py`: map neutral tool declarations → Gemini `FunctionDeclaration`;
  parse Gemini function-call events; return `FunctionResponse`.
- **Async tools (Dev API):** wire `Behavior.NON_BLOCKING` +
  `FunctionResponseScheduling` (`interrupt|when_idle|silent`) per S4. Gate behind the
  capability descriptor so Vertex/emulation is a clean fallback. If S4 slips, ship
  **blocking tools only** this phase and defer async to Phase 4 — architecture already
  builds registry entries as futures (`09§A`).

**Exit criteria:** agent calls a sync tool and speaks the result; a `deferred`/
non-blocking tool resolves late without stalling the turn (Dev API); cancel + timeout
sweeps proven with a deterministic `MockVendorAdapter` (build it here —
`09§E TODO(doc-test-strategy)`).

---

## 6. Phase 3 — multi-agent Router (the core novelty)

**Goal:** one active + N silent listeners, promotion/handoff, the seam — the density
and handoff-latency story, on Gemini.

**Build:**
- `router/`: full `Router` (mechanism), `OutputGate` (single-producer ring + atomic
  token), input **fan-out bus** (GATE 1, dynamic subscription), seam engine
  (`CUT_NOW` / `AT_TURN_END` default / `AT_IDLE`), token-revoke + vendor-cancel as
  separate actions.
- `router/policy.py`: `RoutingPolicy.decide(signal) → decision|None`; built-ins
  `ControlToolPolicy`, `RulePolicy` (declarative `{field,op,value}` + callable escape
  hatch), `ProgrammaticPolicy`, `ChainPolicy`. **Ship the default chain as
  `Programmatic → ControlTool → Rule`** (`09§E TODO(chain-default-order)` — app/backend
  decision must beat the model's `transfer_to`). `LLMRouterPolicy` = stub/opt-in only.
- **Per-listener modality (TEXT or AUDIO, not both).** A listener carries a fixed
  response modality; a session mixes them. TEXT = cheapest (no audio-out) but needs a
  text→audio flip (Gemini reconnect) on promote; AUDIO = costs audio-out for
  suppressed audio but promotes with **no flip**. Router/pool assigns modality per
  listener — default pattern: keep the **1 likely-next** in AUDIO (no-flip promote),
  the rest in TEXT. GATE 2 drops an audio listener's audio; the single-token invariant
  holds regardless. (`05` listener modality.)
- `connections/`: promotion path using the **S1 finding**. On Gemini a **text**
  listener's promote is *not* a free token flip — implement the chosen mechanism
  (resumption-reconnect, or keep-likely-next-as-audio-listener, or replay-promote) and
  health-gate it (`02` pre-flight). An **audio** listener's promote is the near-atomic
  no-flip path.

**Must resolve here (the load-bearing listener questions):**
- `09§E TODO(gemini-modality-flip)` — implement per S1; document the real promotion
  cost in `05`.
- `09§E TODO(listener-context)` — the fan-out bus carries **user audio only**; a
  listener never hears the **active agent's** turns. Decide + implement how a listener
  stays current on the agent side (stream agent transcripts in as turns? projection
  replay on promote?). Until resolved, "context-current, no replay" is false.
- `09§E TODO(listener-divergence)` — a listener (text **or** audio) that *responds*
  every turn accumulates `model` turns the user never heard. Pick fast-but-divergent
  vs correct-but-replayed and document the trade in `05`.
- `09§E TODO(listener-economics)` — measure and record cost/quota per listener-hour
  on Gemini (audio-in + text-gen per turn; Gemini Live concurrent-session quota is
  tight). This decides how many listeners v1 can actually afford. Put numbers in `00`/`05`.
- `09§E TODO(backpressure-per-ring)` — vendor-tx rings are STT-bound: use
  drop-newest / unsubscribe-laggard / discontinuity marker, **not** playout's
  drop-oldest.

**Exit criteria:** a second Gemini agent listens silently, then promotes to active on
a rule/control-tool trigger with the user hearing a single continuous stream across
the seam (no overlap, worst case a maskable gap); listener cost + quota measured and
written down; the four listener TODOs above have a documented resolution (even if the
resolution is an accepted v1 limitation).

> This phase is where Snail's headline claim ("listeners kill the handoff gap") is
> either earned or honestly downgraded for v1. The single-stream **invariant** holds
> regardless (structural); the *latency* of promotion is what S1/listener-context decide.

---

## 7. Phase 4 — hardening (pool, recycle, resilience)

**Goal:** production-shaped lifecycle and failure handling.

**Build:**
- `connections/`: full **Pool** — pre-warm (predictive) / park / keepalive / recycle /
  evict / admission cap; per-`AgentSpec` key. `ConnectionLifecycleManager`:
  `deadline − margin` proactive recycle, `GoAway` handling, keepalive vs recycle split.
- **Recycle, both paths** (`09§D`): assigned (restore context via **native
  `session_resumption`** fast-lane per S3, log-replay fallback) vs unassigned (fresh
  socket + re-apply SetupParams). Atomic connection-ref swap under a stable `AgentSpec`.
- **Vertex profile** finish + verify (async-tool emulation fallback, #1739).
- Async/blocking tool completion, batch-completion (`by_response_group`) if not
  already — else leave as `09§A` deferred.
- Failure/degradation: mid-utterance socket death (not `GoAway`), reconnect, filler
  masking (`09§E TODO(doc-failure)`).

**Must resolve / track here:**
- `09§E TODO(log-truncation-fidelity)` — barge-in fidelity: Gemini has **no**
  `item.truncate`, so decide what the log records when the user heard 40% of a
  sentence. Document the Gemini answer.
- `09§E TODO(log-completeness)` — transcript timing holes on replay; still **no
  compaction policy** in v1 (accepted limitation; `last_n` only). Record the budget.

**Exit criteria:** sessions survive vendor max-duration transparently via resumption;
recycle is invisible for listeners and boundary-safe for the active agent; a mid-turn
socket death recovers with a filler; benchmark harness (`09§C`) stood up against
pipecat for the density claim.

---

## 8. Cross-cutting: which `09§E` review-TODOs land in which phase

| TODO (`09§E`) | Severity | Phase | Disposition in v1 |
|---|---|---|---|
| `gemini-modality-flip` | 🔴 | **0 (spike) → 3** | Implement per S1; the promotion hot path |
| `client-protocol` | 🔴 | **0 (spike) → 1** | Build the control channel + playout clock now |
| `v1-vad` | 🔴 | **0 (spike) → 1** | v1 = server-VAD; local VAD stays deferred; soften `00` |
| `listener-context` | 🔴 | **3** | Decide agent-side feed; document |
| `listener-divergence` | 🔴 | **3** | Pick + document trade |
| `listener-economics` | 🔴 | **3** | Measure Gemini cost/quota; numbers into `00`/`05` |
| `chain-default-order` | 🟢 | **3** | Ship `Programmatic`-first default chain |
| `framepool-ownership` | 🟡 | **1** | Refcount protocol before fan-out |
| `backpressure-per-ring` | 🟡 | **3** | Per-ring policy (STT vs playout) |
| `offload-threshold` | 🟡 | **1** | Inline small DSP, threshold offload |
| `log-truncation-fidelity` | 🟡 | **4** | Gemini has no `item.truncate` — document |
| `log-completeness` | 🟡 | **4** | No compaction in v1 (accepted); record budget |
| `doc-public-api` | 🔴 | **1 (parallel)** | Write the hello-world DX doc — pressure-tests every abstraction |
| `doc-transport` | 🔴 | **1** | Falls out of the S6/transport work |
| `doc-failure` | 🟡 | **4** | Failure/degradation doc |
| `doc-test-strategy` | 🟡 | **2** | `MockVendorAdapter` + deterministic tests |

`doc-public-api` is flagged in `09§E` as the **highest-value next doc** — write it
alongside Phase 1 so the neutral surface is validated by a real hello-world before the
Router complexity lands.

---

## 9. OpenAI deferral list (later version)

OpenAI Realtime is a **locked co-target** (`00` vendor-neutrality fully stays) but is
**out of scope for this phase**. The architecture keeps it a pure adapter add — no
core rewrite — because everything above the `Item[]` boundary is vendor-neutral and
capability is keyed per `(vendor, model, backend)`. Deferred work, to implement in a
later version:

- [ ] **`vendor/openai.py` — `OpenAIRealtimeAdapter`**: `Item[]` → `session.update` +
      `conversation.item.create`; parse Realtime events → neutral events. (`07`)
- [ ] **SetupParam symmetry**: bind `system_instruction` + `tools` at setup even though
      OpenAI *could* inject late — keep the uniform mental model (`02`).
- [ ] **Recycle via log-replay** (no native resumption): open new socket + replay
      context from projection; budget the re-billed context + wall-time; masking plan
      (`02`, `09§E TODO(log-completeness)`).
- [ ] **`session.created.expires_at`** deadline handling in `ConnectionLifecycleManager`
      (vs Gemini `GoAway`). (`02`)
- [ ] **Async tools = emulate** (no native non-blocking continue) via the registry's
      future-based entries; neutral `schedule` field drives emulation. (`07`, `09§A`)
- [ ] **Barge-in / truncate**: OpenAI *has* `conversation.item.truncate` — wire it to
      the playout-position report so the log records "what the user actually heard"
      (`09§E TODO(log-truncation-fidelity)` — the piece Gemini lacks).
- [ ] **Modality flip is free on OpenAI** (`modalities:["text"]` ↔ audio mid-session):
      the "atomic token-transfer promotion" the Gemini phase couldn't achieve becomes
      real here — implement the fast path and branch on the capability descriptor. (`05`)
- [ ] **Rates**: OpenAI 24k both directions (vs Gemini 16k in / 24k out) — edge
      resample config. (`11`)
- [ ] **Cross-vendor failover** (Gemini dies → OpenAI listener promotes): nearly free
      once a 2nd vendor exists; a marketable goal to write up. (`09§E TODO(doc-failure)`)
- [ ] **Two-tier / model-keyed pool** for OpenAI: pool key is a vendor-supplied
      function → config flip, not a rewrite. (`09§A`)
- [ ] **Vendor capability matrix**: fill the OpenAI column end-to-end; add a
      conformance test suite both adapters must pass.

---

## 10. Sequencing summary

```
Phase 0  Spikes S1–S6           gate on S1/S2/S5/S6 findings
Phase 1  Single Gemini agent    neutral boundary + audio plane + transport + barge-in
Phase 2  Tools + registry       + MockVendorAdapter, async tools (Dev API)
Phase 3  Multi-agent Router     listeners, promotion (per S1), the 4 listener TODOs
Phase 4  Hardening              pool, recycle (resumption), Vertex, failure, benchmark
────────────────────────────────────────────────────────────────────────────────
Later    OpenAI adapter (§9)    pure adapter add; unlocks cross-vendor failover
```

**Ordering rationale:** de-risk the Gemini-specific "no mid-session update" holes
first (Phase 0), prove the neutral boundary + audio + real cut on one agent
(Phase 1), add tool authority (Phase 2), *then* stress the multi-agent novelty
(Phase 3) once the promotion mechanism is known from S1, and only then productionize
(Phase 4). OpenAI slots in behind the frozen `Item[]` boundary with no core changes.
