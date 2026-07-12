# Discussion Log (narrative timeline)

A chronological narrative of the design discussion, for context on *why* decisions
were made. The other docs hold the *what*.

1. **Framing.** Snail = vendor-neutral multi-voice-agent framework (Gemini Live +
   OpenAI Realtime), one face to the user, built for performance in pure Python.
   Initial "30x faster / 30x less memory" claim was interrogated.

2. **Performance reality check.** Voice is I/O-bound; vendor dominates the loop.
   30x latency is invisible to users; the real, defensible wins are **density/cost**
   (memory) and **barge-in/handoff latency** (framework-controlled). Dropped the
   "30x" headline; performance = design principle. Benchmark vs pipecat later.

3. **Constraints.** Pure Python, no custom Rust/C; lean on numpy + msgspec. Wins
   come from avoiding pipecat/livekit waste (pydantic, fat frames, object churn).

4. **Context model.** Chose **append-only event log + snapshot projections**
   (Option A) over live mutable context — no locks, no torn reads, cheap sharing,
   one structure for context/replay/persist. Context = transcripts, not audio.

5. **Projection API.** Two modes — declarative spec (default) + imperative builder
   (escape hatch) — both returning canonical `Item[]`, never vendor dicts. Compared
   to pipecat (frame pipeline) and livekit (capability interfaces); Snail's IR is
   the append-only log.

6. **Connections.** Separated **AgentSpec (config) from AgentConnection (socket)**.
   Framework owns lifecycle + pool. Pre-open sockets, inject client context on
   join. No cross-session session reuse (realtime sessions are stateful).

7. **SetupParam vs JoinContext.** Verified (docs + SDK + Google forum) that Gemini
   Live can't update system_instruction/tools mid-session and has no `role="system"`
   content turn. Decided system_instruction + tools = **setup, both vendors**
   (symmetric). History = injected on join before first model turn. Per-client
   instructions ⇒ distinct AgentSpec ⇒ distinct pool bucket.

8. **Pool.** Per-AgentSpec, predictive pre-warm + park + keepalive + recycle +
   evict + global admission control.

9. **Tool layer.** Common-denominator schema (policy A). Stateless, reusable,
   vendor-independent `Tool`. **Exposure ≠ authority.** Vendor call = **intent**;
   Router owns execution. **Handoff = a control tool** the Router catches.

10. **ToolCall vs ToolResult** naming clarified (request vs execution output),
    correlated by `call_id`. Result's durable home = the log (`tool_result` event).

11. **output_schema** = required; binds as `data` on success. Handler returns a
    neutral structured value; adapter encodes per vendor.

12. **ToolResult envelope + status taxonomy** — success/error/blocked/skipped/
    invalid_args/timeout/invalid_output/not_found/cancelled (+deferred later).
    Every call_id → exactly one enveloped result; sanitization boundary;
    `retriable` hint. `code` field dropped.

13. **Speech directives** — `response_mode` (speak/silent) + `speak_directive`
    (hint/verbatim, verbatim = best-effort). Framework defaults per status,
    overridable per-tool / per-call.

14. **executing vs terminal status** — `executing` is an internal lifecycle state,
    not an envelope status. Distinguished `skipped` (close now, handled elsewhere)
    from `deferred` (keep call_id open, result comes later). "Somewhere else" maps
    back via `call_id`.

15. **NON_BLOCKING research (subagent)** — verified `google-genai` v2.11.0 supports
    `Behavior.NON_BLOCKING` + `FunctionResponseScheduling` (SILENT/WHEN_IDLE/
    INTERRUPT) on **Gemini 2.5 Flash Live, Live API, Dev API only** — NOT Vertex
    (#1739), NOT 3.1. Our neutral `schedule` maps 1:1. Explains past errors (wrong
    model / Vertex / old SDK).

16. **ToolCallRegistry** — in-flight tracker keyed by call_id; form, indexes
    (by_response_group, by_connection), lifecycle FSM, single-resolution invariant,
    boundaries. **Cancel/timeout** designed: co-operative task cancellation, no
    side-effect rollback, atomic resolution (lockless via single loop), cleanup.
    Cancel is part of barge-in.

17. **Concurrency** — chose **standard GIL build** (voice is I/O-bound; GIL released
    on I/O + numpy/msgspec). One asyncio loop per worker process (uvloop), scale via
    processes, lockless registry, offload CPU to threadpool. Async lib lean:
    **anyio + uvloop** (free with FastAPI; avoids pure-trio's asyncio-bridge pain).

18. **Vendor scope** — Gemini **2.5 Flash Live only**, both Dev API and Vertex;
    OpenAI Realtime co-target; vendor-neutrality stays.

19. **Router #7 answered** — **one active + silent listeners**, input fan-out,
    single OutputGate. Listeners kill the handoff gap (connected + context-current
    + audio-current). Listener modality is **per-listener** (TEXT or AUDIO, not both;
    a session mixes them) — *corrected 2026-07-12 from the earlier "all listeners
    text-only" note*: text = cheapest but needs a text→audio flip (Gemini reconnect)
    on promote; audio = costs audio-out but promotes with no flip. OutputGate =
    single-producer ring buffer + atomic ownership token.

20. **Vendor timeout / recycle** — proactive recycle (`deadline − margin`),
    keepalive for idle-timeout; restore context via Gemini native resumption or
    log-replay (works for both because context lives in the log). Listener recycles
    anytime; active at safe boundary. Promotion is health-gated. Added nuance: two
    recycle paths — **assigned** (has client context, restore needed) vs
    **unassigned** (pool standby, SetupParams only, trivial recycle).

21. **Docs created** (this folder).

22. **Rejected the dedicated LLM "router agent."** Considered a standing LLM that
    passively listens and decides routes. Cons: serial LLM hop in the handoff hot
    path, third always-on billing socket, non-deterministic control plane, SPOF,
    two sources of routing truth. Verdict: LLM routing = opt-in async policy off the
    hot path, not the core default.

23. **Router LOCKED — mechanism vs decision.** Router owns mechanism (token transfer,
    subscription, seam, cancel). Pluggable **`RoutingPolicy.decide(signal) →
    decision | None`** owns the decision — trigger-driven, sync-or-async by the
    policy's choice, advice (Router health-gates before acting). Built-ins:
    ControlTool (default) / Rule / Programmatic / LLMRouter / **Chain**. Precedence
    fork resolved: **ChainPolicy order = precedence**.

24. **Audio seam locked.** Handoff moves upstream (who feeds buffer) + downstream
    (output token). Three **seam modes**: `CUT_NOW` (revoke+flush, for barge-in),
    `AT_TURN_END` (finish tail, default), `AT_IDLE` (opportunistic). Token-revoke
    (user-facing) and vendor-cancel (stop waste) are **separate actions**. Invariant:
    single output token → **overlap structurally impossible**; worst case is a
    maskable gap, never two voices. Demote-to-listener locked. The **modality flip
    (text-modality listener → audio on promote)** is the seam's real open cost — but
    only for text listeners; an audio-modality listener promotes with no flip →
    deferred to a future audio-flow-management design.

25. **RulePolicy predicate surface locked.** Ordered `Rule(predicate,
    decision_template)`, first match wins. Predicate reads only the RoutingSignal
    fields (event/payload, active_agent, available, session_meta, context_view).
    Three classes: field match / state threshold / text match, combinable AND/OR/NOT.
    **Declarative `{field, op, value}` default + callable escape hatch** (mirrors the
    projection API). Rules emit HANDOFF or FANOUT. **Listener→active fork dissolved:**
    promoting a listener = a rule targeting a listener spec on that listener's
    fanned-in signal — same code path as any handoff. **Text-match trust line:** fuzzy
    → coarse routing only; authority/security lives in deterministic tool_result
    status, never a text match.

26. **Audio plane locked (see 11).** `AudioFrame` = msgspec struct, numpy int16 view,
    no strings/dict/pydantic. **Canonical interior = 48kHz PCM int16 mono**, driven by
    the opus+RNNoise synergy (libopus decodes native 48k; RNNoise wants 48k/480) →
    default stack resamples **only at the vendor edge**. Defaults: opus in/out, RNNoise
    on, binary frames on client leg. **Codec (opus) = the latency lever; bytes = a
    density lever, not latency** — verified Gemini + OpenAI force base64-in-JSON on the
    wire (SDK accepts bytes but base64-wraps them), so bytes-forcing wins the client
    leg only. FramePool free-list kills per-chunk churn (the density win). **Two gates:**
    input subscription (Router) + output token (OutputGate); barge-in = action on gate
    2, not a third gate; bounded rings, drop-oldest, never block source. **Pluggable
    AudioSink** at any of 5 tap-points (INGRESS_RAW/POST_CLEAN/VENDOR_TX/VENDOR_RX/
    EGRESS) — observes never gates, off hot path, null=zero cost. Swappable AudioCleaner
    (RNNoise default). Libs: opuslib/soxr/RNNoise/numpy.

27. **Local VAD deferred to a future release.** v1 relies on vendor server-side VAD
    to trigger the (already-locked) CUT_NOW seam. Local VAD (~10–30ms latency win) +
    dual-VAD arbitration = future work.

28. **Observer layer locked (see 12).** Operational telemetry (metrics + events),
    pluggable at any pipeline point, framework-wide. **Distinct from the event log**
    (log = model context, load-bearing; observer = human/analytics, fire-and-forget,
    never load-bearing). Payload = Metric | Event. Named instrumentation points across
    all subsystems. `Observer.on_metric/on_event`; N observers, off hot path, observe
    never gate, bounded/drop-oldest, null default. **Zero-cost guarded emission** (build
    nothing when no observer). **PUSH (rare events) vs SAMPLED-aggregate (hot metrics)**
    split to avoid swamping. Separate from AudioSink (heavy binary) — same philosophy,
    different payload. Built-ins: Logging/Exporter/InMemory/Null.

## Next up

- Modality flip on promote (audio-flow manager / pre-warm-as-audio) — deferred.
- Freeze exact predicate `op` set (small).
- Canonical Event/Item field schema.
- Assigned-vs-unassigned recycle detail.
