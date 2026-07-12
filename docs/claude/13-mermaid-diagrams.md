# Mermaid Diagrams (low-level, with control flow)

Machine-renderable companion to the ASCII diagrams in
[08-architecture-diagrams.md](08-architecture-diagrams.md). Everything finalized so
far, one place. Convention across all diagrams:

- **Solid arrows** = **data / audio flow** (bytes, frames, items).
- **Dashed arrows** = **control flow** (decisions, gate ops, token moves, signals).
- Gates are the two control points: **GATE 1** = input subscription (Router-owned),
  **GATE 2** = output token (OutputGate, single writer).

---

## 1. Master system view (data + control)

```mermaid
flowchart TB
  %% ---------- CLIENT LEG ----------
  subgraph CLIENT["CLIENT LEG — we control: binary WS + opus (PCM opt-in)"]
    MIC["mic — opus/pcm bytes"]
    SPK["speaker — opus bytes"]
  end

  %% ---------- AUDIO PLANE ----------
  subgraph AUDIO["AUDIO PLANE — interior 48kHz PCM int16 mono, FramePool free-list"]
    direction TB
    DEC["INGRESS decode opus/b64 to PCM, upsample to 48k, acquire AudioFrame"]
    CLEAN["RNNoise clean @48k (480-sample reframe)"]
    FANOUT{{"FAN-OUT BUS — 1 producer to N bounded rings, drop-oldest"}}
    VTX["vendor edge TX — down 48k to 16k/24k, int16 to base64"]
    VRX["vendor edge RX — decode, up 24k to 48k, acquire AudioFrame"]
    JIT["jitter buffer — smooth vendor bursts"]
    OGATE{{"OUTPUTGATE — GATE 2 single atomic token; only holder drains"}}
    ENC["encode opus to client"]
  end

  %% ---------- TAP LAYERS ----------
  SINKS["AudioSink layer — 5 tap points, off hot path, observe never gate, null=0-cost"]
  OBS["OBSERVER layer — Metric/Event, taps every subsystem, PUSH rare / SAMPLED hot, disposable"]

  %% ---------- ROUTER ----------
  subgraph ROUTER["ROUTER / ARBITER — owns GATE1 subs, GATE2 token, seam, ToolCallRegistry"]
    direction TB
    RPOL["RoutingPolicy.decide(signal) to decision|None — ChainPolicy: ControlTool then Rule then Programmatic then LLM(async)"]
    RMECH["mechanism: token xfer, subscription flip, seam cut, cancel sweep"]
    TCR[("ToolCallRegistry — call_id to PendingCall, FSM, single-resolution")]
  end

  %% ---------- AGENTS ----------
  subgraph AGENTS["AGENTS — 1 ACTIVE + N LISTENERS"]
    direction TB
    ACT["ACTIVE agent — AUDIO out, holds token"]
    LIS["listener agents — TEXT or AUDIO (per-listener), output suppressed"]
    ADP["VendorAdapter — Gemini 2.5 Live / OpenAI Realtime"]
  end

  %% ---------- TOOLS ----------
  subgraph TOOLS["TOOL LAYER"]
    TREG["ToolRegistry — static catalog name to Tool{in/out schema, handler}"]
    TOOL["Tool — stateless, common-denominator schema"]
    TRES["ToolResult envelope — status,data,reason,retriable,response_mode,speak_directive"]
  end

  %% ---------- CONTEXT ----------
  subgraph CTX["CONTEXT MANAGER — single source of truth"]
    LOG[("append-only EVENT LOG — msgspec structs, audio-free")]
    PROJ["Projection — spec (declarative) or builder (imperative)"]
    ITEM["Item[] — vendor-neutral, HARD boundary"]
  end

  %% ---------- POOL ----------
  POOL["CONNECTION LIFECYCLE MGR (pool) — pre-warm, park, keepalive, recycle, evict, admission; deadline minus margin; assigned vs unassigned"]

  %% ===== DATA FLOW (solid) =====
  MIC --> DEC --> CLEAN --> FANOUT
  FANOUT --> VTX --> ADP
  ADP --> VRX --> JIT --> OGATE --> ENC --> SPK
  ADP --- ACT
  ADP --- LIS
  TREG --> TOOL
  TOOL --> TRES
  LOG --> PROJ --> ITEM --> ADP

  %% audio taps (data)
  DEC -. INGRESS_RAW .-> SINKS
  CLEAN -. POST_CLEAN .-> SINKS
  VTX -. VENDOR_TX .-> SINKS
  VRX -. VENDOR_RX .-> SINKS
  ENC -. EGRESS .-> SINKS

  %% ===== CONTROL FLOW (dashed) =====
  ADP -. "ToolCall intent (call_id,name,args)" .-> ROUTER
  ADP -. "user/agent speech, tool events, handoff" .-> LOG
  FANOUT -. "GATE 1 subscribe/unsubscribe" .-> ROUTER
  ROUTER -. "GATE 1 who hears user audio" .-> FANOUT
  ROUTER -. "GATE 2 grant/revoke token" .-> OGATE
  ROUTER -. "promote/demote, modality flip text<->audio" .-> AGENTS
  ROUTER -. "classify/authorize/decide; register call_id" .-> TCR
  TCR -. "dispatch handler" .-> TREG
  TRES -. "append tool_result; resolve future" .-> LOG
  TRES -. "serialize to vendor; model continues" .-> ADP
  RPOL -. advice .-> RMECH
  RMECH -. acts .-> RMECH
  POOL -. "atomic swap AgentConnection (spec unchanged)" .-> ADP
  LOG -. "log-replay restore on recycle" .-> POOL

  %% observer taps everything (control/telemetry)
  AUDIO -. telemetry .-> OBS
  ROUTER -. telemetry .-> OBS
  TOOLS -. telemetry .-> OBS
  POOL -. telemetry .-> OBS
  AGENTS -. telemetry .-> OBS

  classDef gate fill:#2d3748,stroke:#f6ad55,color:#fff;
  classDef store fill:#1a365d,stroke:#63b3ed,color:#fff;
  class FANOUT,OGATE gate;
  class LOG,TCR store;
```

---

## 2. Two planes — one active + silent listeners

```mermaid
flowchart LR
  U["user audio (post-clean)"]
  subgraph IN["INPUT plane — FAN-OUT (GATE 1, Router-controlled)"]
    A["ACTIVE agent — always subscribed"]
    L1["listener 1 — AUDIO modality (likely-next, no-flip promote)"]
    Ln["listener N — TEXT modality (cheap)"]
  end
  OG{{"OutputGate — single token (GATE 2)"}}
  SPK["user speaker"]

  U --> A
  U -. "subscribe IFF Router chose" .-> L1
  U -. "subscribe IFF Router chose" .-> Ln
  A -- "agent audio (token holder)" --> OG --> SPK
  L1 -. "audio generated then DROPPED (no token)" .-> OG
  Ln -. "text only, no audio to gate" .-> OG
  A -. "promotion = token transfer (+text→audio flip only if TEXT listener)" .-> OG
```

**Invariant:** user hears exactly one stream — single token → overlap structurally
impossible.

---

## 3. Context: log to projection to vendor

```mermaid
flowchart TB
  subgraph EVT["Event types (append-only)"]
    E["user_speech | agent_speech | tool_call | tool_result | external_context | handoff"]
  end
  LOG[("EVENT LOG — canonical msgspec structs, single source of truth")]
  P1["Projection spec — include/agents/last_n/instructions/extra (~90%)"]
  P2["Projection builder — imperative escape hatch (~10%)"]
  ITEM["Item[] — canonical, vendor-neutral — HARD boundary, never raw vendor dicts"]
  OAI["OpenAI: session.update + conversation.item.create"]
  GEM["Gemini: setup(systemInstruction+tools) + user/model content turns"]

  E --> LOG
  LOG --> P1 --> ITEM
  LOG --> P2 --> ITEM
  ITEM --> OAI
  ITEM --> GEM
```

---

## 4. Control flow — turn + tool call (intent to result)

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant AU as Audio plane
  participant V as VendorAdapter (active)
  participant R as Router
  participant TCR as ToolCallRegistry
  participant TR as ToolRegistry
  participant LOG as Event Log

  U->>AU: speech (opus)
  AU->>AU: decode, clean @48k
  AU->>V: fan-out to active (GATE1), down-rate, base64
  V->>LOG: append user_speech (transcript)
  V-->>R: ToolCall(call_id,name,args)  [INTENT]
  R->>R: classify (agent vs control) + authorize (exposure != authority)
  alt execute
    R->>TCR: register call_id -> PendingCall{future}
    TCR->>LOG: append tool_call
    R->>TR: dispatch handler(args)
    TR-->>TCR: value (validate vs output_schema)
    TCR->>LOG: append tool_result (envelope)
    TCR-->>V: serialize ToolResult -> vendor wire
    V->>AU: agent audio (if response_mode=speak)
    AU->>U: speaker (token holder only, GATE2)
  else blocked / skipped / handoff
    R-->>V: terminal envelope (blocked|skipped)
    Note over TCR: INVARIANT every call_id -> exactly 1 result
  end
```

---

## 5. Control flow — barge-in (CUT_NOW seam)

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant VAD as VAD (v1 = vendor server-side)
  participant R as Router
  participant OG as OutputGate (GATE2)
  participant V as VendorAdapter (active)
  participant TCR as ToolCallRegistry

  U->>VAD: starts speaking over agent output
  VAD-->>R: interrupt signal
  R-->>OG: revoke token + FLUSH ring (drop queued audio)
  R-->>V: vendor cancel/interrupt (stop wasted generation)
  R-->>TCR: sweep by_response_group -> cancel in-flight -> resolve `cancelled`
  Note over OG,V: token revoke (user-facing) and vendor cancel (generation) are SEPARATE actions
  Note over U: TODO(client-protocol 09E): client-side playout still buffered<br/>needs client flush frame + playout position to truly cut
```

---

## 6. Control flow — handoff / promotion (the listener win)

```mermaid
sequenceDiagram
  autonumber
  participant V as Active agent
  participant R as Router
  participant POL as RoutingPolicy
  participant L as Listener warm/subscribed
  participant OG as OutputGate

  V-->>R: transfer_to target OR rule/programmatic signal
  R->>POL: decide RoutingSignal
  POL-->>R: RoutingDecision action=HANDOFF target seam
  Note over R: health-gate target for TTL headroom, never promote stale socket
  alt seam is AT_TURN_END default
    Note over V: active keeps token, finishes utterance, buffer drains naturally
  else seam is CUT_NOW
    R-->>OG: revoke plus flush, see barge-in
  end
  alt target is TEXT-modality listener
    R-->>L: modality flip text to audio
    Note over L: TODO gemini-modality-flip 09E, NOT free on Gemini<br/>response_modalities is setup config, needs resumption/reconnect
  else target is AUDIO-modality listener
    Note over L: no flip needed, near-atomic promote (audio-out cost paid while listening)
  end
  R-->>OG: grant token to target
  R-->>V: demote to listener, release token, vendor cancel, optional flip audio to text
  Note over L: TODO listener-context 09E, listener heard only USER audio<br/>not the active agent turns, context-current claim unresolved
```

---

## 7. Connection lifecycle (state machine)

```mermaid
stateDiagram-v2
  [*] --> cold
  cold --> connecting
  connecting --> warm
  warm --> active: promote
  warm --> listener
  listener --> active: promote (health-gated)
  active --> warm: demote (to listener)
  warm --> recycling: deadline minus margin / GoAway
  active --> recycling: at safe boundary (never mid-turn)
  listener --> recycling: anytime (seamless)
  recycling --> warm: socket swapped, identity kept
  recycling --> active: socket swapped, identity kept
  active --> closed
  listener --> closed
  warm --> closed
  closed --> [*]
```

**Recycle = kill + rebuild-from-log** (context lives in the log, connections are
disposable). Gemini: native `session_resumption` (fast lane) OR log-replay. OpenAI:
log-replay only.

---

## 8. ToolCall lifecycle FSM (per registry entry)

```mermaid
stateDiagram-v2
  [*] --> received
  received --> validating
  validating --> invalid_args: input_schema fail
  validating --> executing
  executing --> resolving
  resolving --> done
  invalid_args --> done
  executing --> awaiting_external: deferred path
  awaiting_external --> done
  received --> cancelled: barge-in / handoff / close
  validating --> cancelled
  executing --> cancelled
  resolving --> cancelled
  received --> timeout: deadline fired
  validating --> timeout
  executing --> timeout
  cancelled --> done
  timeout --> done
  done --> [*]

  note right of executing
    executing = INTERNAL only,
    never a terminal envelope status
  end note
```

**Cancel scope by trigger:** barge-in to `by_response_group`; handoff to
`by_connection`; close to everything. First terminal wins (single loop = lockless);
side effects NOT rolled back (handler's job).

---

## 9. Pool — assigned vs unassigned recycle

```mermaid
flowchart TB
  CLM["ConnectionLifecycleManager — same deadline-minus-margin scheduler for both"]
  subgraph UN["UNASSIGNED (pool standby)"]
    U1["SetupParams only, no client bound"]
    U2["recycle = fresh socket + re-apply SetupParams"]
    U3["not time-sensitive, fully invisible, background"]
  end
  subgraph AS["ASSIGNED (bound to client)"]
    A1["JoinContext + live conversation"]
    A2["recycle MUST restore context: native resume OR log-replay"]
    A3["invisibility depends on role: listener anytime / active at safe boundary"]
  end
  CLM --> UN
  CLM --> AS
  U2 -. "keeps a fresh standby so promoted socket never stale" .-> AS
```

---

## Notes

- Diagrams reflect **locked** decisions across 01-12. Open items carry inline
  `TODO(...)` notes pointing at [09-pending-items.md](09-pending-items.md) §E
  (design-review TODOs) — chiefly listener-context, gemini-modality-flip,
  client-protocol, and v1-vad, which touch the barge-in/handoff control flow drawn
  above.
- Where a control-flow diagram shows a step that is **not yet free on a vendor**
  (Gemini modality flip, client-side cut), the note is on the arrow so the diagram
  stays honest rather than aspirational.
