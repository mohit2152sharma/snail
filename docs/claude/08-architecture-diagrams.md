# Architecture Diagrams

Low-level diagrams of the design finalized so far.

## Master — whole-system low-level view

Everything in one picture: transport edge → audio plane → Router → agents → vendors,
with the context log, pool, and the two cross-cutting tap layers (AudioSink,
Observer) shown where they attach.

```
                                        S N A I L   S E S S I O N
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                            │
 │  CLIENT LEG (we control: binary WS frames + opus, PCM opt-in)                              │
 │   mic ─opus/pcm bytes─►┐                                    ┌─◄─opus bytes─ speaker        │
 │                        │                                    │                              │
 │  ┌─────────────────────▼────────────────────────────────────┴──────────────────────────┐ │
 │  │  AUDIO PLANE            (interior = 48kHz PCM int16 mono; FramePool free-list)        │ │
 │  │                                                                                       │ │
 │  │  INGRESS: decode(opus/b64→pcm) ─► upsample→48k ─► AudioFrame[pool]                    │ │
 │  │             │                                        │                                 │ │
 │  │             │                                   (tap)INGRESS_RAW ─┐                    │ │
 │  │             ▼                                        ▼            │                    │ │
 │  │        RNNoise clean @48k (480-reframe) ──► (tap)POST_CLEAN ─┐    │                    │ │
 │  │             │                                               │    │                    │ │
 │  │             ▼                                               │    │                    │ │
 │  │   ┌───── FAN-OUT BUS ─────┐   GATE 1 = input subscription   │    │                    │ │
 │  │   │  (1 producer→N rings, │   (Router picks who hears)      │    │                    │ │
 │  │   │   bounded,drop-oldest)│                                 ▼    ▼                    │ │
 │  │   └───┬─────────────┬─────┘                        ┌───────────────────┐              │ │
 │  │       │ active      │ listeners (subscribed)       │   AudioSink layer  │             │ │
 │  │       ▼             ▼                              │  (pluggable @ any   │             │ │
 │  │  [vendor edge: down 48k→16k/24k + int16→b64]       │   of 5 tap-points;  │             │ │
 │  │       │             │                              │   off hot path;     │             │ │
 │  │       │             │           ▲ agent audio      │   observe≠gate)     │             │ │
 │  │       │             │           │ up 24k→48k       └───────────────────┘              │ │
 │  │       │             │      (tap)VENDOR_RX ─────────────────┘   ▲                       │ │
 │  │       │             │           │                              │                       │ │
 │  │       │             │      jitter buf ─► OUTPUTGATE ─► encode opus ─► EGRESS(tap)      │ │
 │  │       │             │                    (GATE 2 = single atomic token; only           │ │
 │  │       │             │                     token-holder drains; barge-in flushes here)  │ │
 │  └───────┼─────────────┼──────────────────────────────▲──────────────────────────────────┘ │
 │          │             │                               │                                    │
 │  ┌───────▼─────────────▼───────────────────────────────┴──────────────────────────────┐   │
 │  │  ROUTER / ARBITER                                                                    │   │
 │  │    owns: GATE1 subscriptions · GATE2 output token · seam · ToolCallRegistry          │   │
 │  │                                                                                       │   │
 │  │    RoutingSignal ─► RoutingPolicy.decide() ─► RoutingDecision|None                    │   │
 │  │       (mechanism vs decision split; policy = advice, Router validates+acts)           │   │
 │  │       ChainPolicy[ ControlTool → Rule → Programmatic → (LLMRouter async) ]            │   │
 │  │            order = precedence      RulePolicy: declarative {field,op,val} + callable   │   │
 │  │    decision.seam ∈ CUT_NOW | AT_TURN_END(default) | AT_IDLE                            │   │
 │  │    promote=token xfer+modality flip(text→audio) · demote=release+cancel→listener      │   │
 │  └───────┬───────────────────────────────────────────────────────────────────┬──────────┘   │
 │          │ intent (ToolCall)                                                  │ role/modality│
 │  ┌───────▼───────────────────────────────────────┐   ┌───────────────────────▼──────────┐   │
 │  │  TOOL LAYER                                    │   │  AGENTS  (1 ACTIVE + N LISTENERS) │   │
 │  │   ToolRegistry(defs)  ToolCallRegistry(inflight│   │   AgentSpec ─► AgentConnection    │   │
 │  │    call_id→PendingCall{future}; FSM; single-   │   │   active = AUDIO out              │   │
 │  │    resolution; cancel/timeout sweeps)          │   │   listener = TEXT or AUDIO (05)   │   │
 │  │   Tool: name+in/out schema+handler (stateless) │   │   ┌──────────┐   ┌──────────┐     │   │
 │  │   ToolResult envelope{status,data,speak_dir}   │   │   │VendorAdpt│   │VendorAdpt│     │   │
 │  │   exposure≠authority · intent-not-command      │   │   │ Gemini   │   │ OpenAI   │     │   │
 │  └───────┬────────────────────────────────────────┘   │   │ 2.5 Live │   │ Realtime │     │   │
 │          │                                             │   └────┬─────┘   └────┬─────┘     │   │
 │  ┌───────▼───────────────────────┐                     └────────┼──────────────┼──────────┘   │
 │  │  CONTEXT MANAGER              │                              │ base64/JSON  │ base64/JSON   │
 │  │   append-only EVENT LOG        │◄──── writes (user/agent speech, tool_call/result, handoff) │
 │  │   (msgspec structs; audio-free)│                              │              │              │
 │  │   ─► Projection (spec|builder) │                     ┌────────▼──────────────▼─────────┐    │
 │  │   ─► Item[] (neutral) ─► adapter│                    │  CONNECTION LIFECYCLE MGR (pool) │    │
 │  │      serialize per vendor       │                    │  pre-warm·park·keepalive·recycle │    │
 │  └────────────────────────────────┘                    │  ·evict·admission; deadline−margin│   │
 │                                                         │  assigned(restore) vs unassigned  │   │
 │                                                         └──────────────────────────────────┘    │
 │                                                                                                 │
 │  ═══ OBSERVER LAYER (cross-cutting) ══════════════════════════════════════════════════════════ │
 │    taps EVERY subsystem (audio/router/tool/pool/connection/session)                             │
 │    Metric|Event ─► N observers (off hot path, observe≠gate, null=zero-cost)                     │
 │    PUSH rare events · SAMPLED-aggregate hot metrics                                             │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
      CONCURRENCY: 1 asyncio loop / worker process (uvloop+anyio); GIL build; lockless registry;
                   scale by processes; CPU offloaded to threadpool.
```

### How to read it

- **Vertical flow** = a turn: mic → audio ingress → fan-out (GATE 1) → active agent →
  vendor → context log; vendor audio back → OUTPUTGATE (GATE 2) → speaker.
- **Router** sits in the middle owning both gates + tool authority + the seam. Decisions
  come from `RoutingPolicy` (advice); Router executes mechanism.
- **Two cross-cutting tap layers** hang off the side: **AudioSink** (heavy binary, 5
  audio points) and **Observer** (light telemetry, every subsystem). Both *observe,
  never gate*; both null-default zero-cost.
- **Edges** carry codec/base64/vendor-rate; the **interior** is uniform 48k int16.

## Component overview

```
                         ┌───────────────────────────────────────────────┐
                         │                   SNAIL SESSION                 │
                         │                                                 │
  user mic ─────────────┼──► AudioTransport ──► INPUT plane (fan-out)     │
                         │        (VAD,            │                        │
  user speaker ◄─────────┼──── ring buffers)       ▼                        │
                         │         ▲          ┌──────────┐                  │
                         │         │          │  ROUTER  │                  │
                         │    OUTPUT plane    │ (Arbiter)│                  │
                         │   (OutputGate,     └────┬─────┘                  │
                         │    single token)        │ owns                   │
                         │         ▲               ├── ToolCallRegistry     │
                         │         │               ├── OutputGate token     │
                         │         │               ├── input subscriptions  │
                         │         │               └── promotion/demotion    │
                         │         │                                        │
                         │   ┌─────┴──────────────────────────────┐        │
                         │   │  Agents (1 active + N listeners)    │        │
                         │   │   AgentSpec → AgentConnection       │        │
                         │   │   [Gemini Live] [OpenAI Realtime]   │        │
                         │   └─────┬───────────────────┬──────────┘        │
                         │         │ VendorAdapter     │                    │
                         │         ▼                   ▼                    │
                         │   ┌──────────────┐   ┌──────────────┐           │
                         │   │ ContextMgr    │   │ ToolRegistry │           │
                         │   │ (append-only  │   │ (definitions)│           │
                         │   │  event log)   │   └──────────────┘           │
                         │   └──────────────┘                              │
                         │         ▲                                        │
                         │   ConnectionLifecycleManager (pool: pre-warm,    │
                         │   park, keepalive, recycle, evict, admission)    │
                         └───────────────────────────────────────────────┘
                                     │
                          vendor WebSockets (Gemini / OpenAI)
```

## Two planes (one active + silent listeners)

```
INPUT plane (fan-out)                     OUTPUT plane (single writer)

user audio                                 active agent audio
   │                                            │
   ├──────────────► ACTIVE agent  ──────────────┤ (holds output token)
   │                                            ▼
   ├──────────────► listener 1 (audio)       OutputGate ──► user speaker
   │                     ⋮  output suppressed    ▲
   └──────────────► listener N (text)             │ only token-holder drains
       (dynamic subscription,                  promotion = token xfer (+flip if text)
        per-listener modality)
```

## Context: log → projection → vendor

```
Event Log (append-only, canonical msgspec structs)
   events: user_speech | agent_speech | tool_call | tool_result
           external_context | handoff
        │
        │  projection = filter/transform (declarative spec OR imperative builder)
        ▼
   Item[]  (canonical, vendor-neutral)   ◄── HARD boundary: never raw vendor dicts
        │
        │  VendorAdapter serializes
        ▼
   ┌─────────────────────┐     ┌─────────────────────────┐
   │ OpenAI:             │     │ Gemini:                 │
   │ session.update +    │     │ setup (systemInstruction│
   │ conversation.item   │     │  + tools) + user/model  │
   │ .create             │     │  content turns          │
   └─────────────────────┘     └─────────────────────────┘
```

## Tool call flow (intent → Router → result)

```
vendor emits ToolCall(call_id, name, args)      ← INTENT (request, not command)
        │
        ▼
   ROUTER intercept ── classify (agent tool | control tool=handoff)
        │           ── authorize (exposure ≠ authority)
        │           ── decide: execute | reject | reroute | handoff
        │
        ├─ register in ToolCallRegistry:  call_id → PendingCall{origin,destination,future}
        │        (append tool_call event to log)
        ▼
   execute? ── yes ──► ToolRegistry.dispatch(handler(args)) ─► value
        │                    │ validate vs output_schema
        │                    ▼
        │              ToolResult envelope {status,data,reason,retriable,
        │                                   response_mode,speak_directive}
        │                    │ append tool_result event to log
        │                    ▼
        │              VendorAdapter serialize ─► vendor wire ─► model continues
        │                    │
        │              resolve future, clear pending entry
        │
        └─ no (blocked/skipped/handoff) ──► terminal envelope back to model
                                            (INVARIANT: every call_id → 1 result)
```

## ToolCall lifecycle FSM

```
                 ┌────────────► invalid_args ──► done
                 │ (input_schema fail)
 received ─► validating ─► executing ─► resolving ─► done
                              │  │
                              │  └──► awaiting_external ──► done   (deferred)
                              │
              any state ──────┼──► cancelled   (barge-in / handoff / close)
              any state ──────┴──► timeout      (deadline fired)

 note: `executing` is INTERNAL only — never a terminal envelope status.
```

## Cancel / timeout resolution

```
TIMEOUT (one call):          deadline fired
                             → resolve `timeout` + cancel handler task + cleanup

CANCEL (turn abandoned):     barge-in  → sweep by_response_group
                             handoff   → sweep by_connection
                             close     → sweep all
                             → resolve each `cancelled` + cancel tasks + cleanup

INVARIANT guard:  first terminal wins (single loop = lockless); stragglers dropped.
SIDE EFFECTS:     NOT rolled back — compensation is the handler's job.
```

## Connection recycle (vendor timeout)

```
track per connection: {created_at, vendor_deadline, resumption_handle?, health}
        │
recycle_at = vendor_deadline − margin   (or immediate on Gemini GoAway)
        │
        ▼
   new socket ── restore context:
        Gemini (Dev API/Vertex): native session_resumption  (fast)  OR log-replay
        OpenAI:                  log-replay (setup + conversation items)
        │
        ▼
   ATOMIC swap AgentConnection   (AgentSpec + upper layers unchanged)

role matters:  listener → recycle anytime (seamless)
               active   → recycle at safe boundary (proactive, never mid-turn)
promotion pre-flight:  never promote a stale socket → recycle-first or fresh standby
```

## Connection lifecycle states

```
cold ─► connecting ─► warm ─┬─► active   ─► (demote) ─► warm(listener)
                            └─► listener ─► (promote)─► active
   any warm/active ─► recycling ─► warm/active   (socket swapped, identity kept)
   any ─► closed
```
