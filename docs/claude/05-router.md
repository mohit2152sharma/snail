# Router / Arbiter

The Router is the multi-agent brain. It owns tool-execution authority (see 03/04)
**and** agent orchestration (roles, routing, handoff). This is where the product
novelty and the barge-in/handoff perf story both live.

## #7 answered: one active + silent listeners

At any instant there is **one active agent** and zero-or-more **silent listeners**.

```
OUTPUT plane (agents → user):   SINGLE writer.
   only the ACTIVE agent holds the output token → its audio → user.
   listeners' output suppressed.  ← OutputGate enforces

INPUT plane (user → agents):    FAN-OUT.
   user audio → active agent (always)
             → selected listeners (dynamic subscription, Router-controlled,
               "simultaneously or whenever needed" based on logic, e.g. tool result)
```

## Why the listener model is powerful — it kills the handoff gap

A cold handoff would cost connection latency + context gap + dropped gap-audio. A
silent listener that has been *hearing the conversation* is already:

- **connected** (warm socket, no handshake),
- **context-current** (it heard everything — no projection replay needed),
- **audio-current** (it has the user's in-flight speech).

So promoting listener → active is **near-instant, no dead air, no replay.** The
listener IS the pre-warmed, context-loaded, audio-synced next agent. "Truly
real-time handoff" falls out of the architecture, not a hack.

> **TODO(listener-context / listener-divergence — see 09§E):** two unresolved
> holes in the claim above. (1) The fan-out bus carries **only cleaned user audio**
> (11) → a listener never hears the **active agent's** turns, so "context-current,
> no replay" is false for the agent side. (2) A listener (text **or** audio) responds
> every turn → those `model` turns pile up in its own vendor session (a conversation
> the user never heard), so a naive promote can cite things never spoken. Decide the
> listener-context model before relying on this.

**Pool connection:** listeners = the warm/parked pool made productive (actively
listening instead of idle).

## Listener modality — per-listener, TEXT or AUDIO (not both)

A listener's **response modality is a per-listener choice: TEXT or AUDIO** — never
both at once. Listeners are **not** uniformly text-only: a session can hold some
text-modality listeners and some audio-modality listeners **at the same time**.
"Silent listener" means silent **to the user** (no output token), not "text-only" —
an audio listener still generates audio; the OutputGate just suppresses it.

```
active agent     → response modality = AUDIO, drained to user (holds token)
text listener    → response modality = TEXT   → no audio synth, cheapest
audio listener   → response modality = AUDIO  → audio generated then DROPPED
                   (GATE 2 suppresses it; no token → never reaches the user)
```

**Modality is a per-listener cost-vs-promotion-latency lever:**

- **TEXT listener** — cheapest (no audio-out billing); analyzes / transcribes /
  emits tool calls without generating audio. But promotion to active needs a
  **text→audio flip**, which on Gemini is a config change = **reconnect/resumption**
  (mid-session config forbidden, 07) → that flip is the seam's real cost.
- **AUDIO listener** — costs audio-out tokens for audio nobody hears, but promotion
  = **no modality flip** → on Gemini it can be a near-atomic token grant with **no
  reconnect**. This is the "pre-warm the likely-next listener in audio modality"
  path (09§E TODO(gemini-modality-flip), option a).

Usual pattern: keep the **1 likely-next** listener in AUDIO modality (instantly
promotable, no Gemini reconnect) and the rest in TEXT (cheap analysis). The mix is a
per-listener Router/pool policy, not a global rule.

> **TODO(gemini-modality-flip — see 09§E):** the flip only bites **text→audio**
> promotions; an audio-modality listener sidesteps it entirely, at audio-out cost.
> Spike S1 must quantify both legs: audio-out token cost of an idle audio listener
> vs the reconnect latency of flipping a text listener on promote. Also weigh
> listener **cost/quota** (09§E TODO(listener-economics)): every subscribed listener
> bills audio-in + generation each turn and eats vendor concurrent-session quota —
> audio listeners bill more.

Both OpenAI (`modalities:["text"]`) and Gemini (response modalities) support
text-only **and** audio output. A text listener can't accidentally leak audio to the
user (nothing to gate); an audio listener relies on GATE 2 alone. Modality =
per-listener role config; on a text listener it flips on promotion.

## OutputGate = the "novel structure"

Single-producer output ring buffer + **atomic ownership token**. Only the
token-holder drains to the user. Promotion = atomic token transfer. One primitive
delivers overlap-avoidance (product) + low barge-in/handoff latency (perf).

## Router responsibilities (consolidated)

```
1. hold the output token → enforce single active speaker (OutputGate)
2. manage input subscriptions → fan-out user audio to active + chosen listeners
3. decide role changes → promote / demote / handoff, from signals:
      tool-call results, LLM transfer_to (control tool), rules, programmatic,
      listener signals (a listener's own tool call)
4. drive the seam → output-token transfer + cancel-sweep on demoted agent
5. own the ToolCallRegistry (execution authority)
```

## Mechanism vs decision — the split

Router owns **mechanism** (token transfer, subscription flip, seam cut, cancel
sweep). A pluggable **`RoutingPolicy`** owns the **decision** (what/when to route).
Policy never touches sockets; it consumes signals, returns advice; Router validates
against reality before acting.

### Why not a dedicated LLM "router agent"

Considered a standing LLM that passively listens and decides routes. **Rejected as
the default** — cons:

- Adds a **serial LLM hop** in the handoff hot path (the thing we optimize).
- **Third always-on socket** billing tokens every turn (~99% need no handoff) —
  fights the density/cost win.
- **Non-deterministic control plane** — routing wants determinism; LLM = probabilistic,
  hard to test, can hallucinate handoffs.
- **SPOF** on every turn; extra context sync + recycle burden.
- Two sources of routing truth (router LLM vs active agent's own `transfer_to`) →
  worsens the precedence problem.

Verdict: most routing is cheap + deterministic. LLM routing is **one opt-in policy**,
run **off the hot path** (async), not baked into the core.

## RoutingPolicy interface

```
RoutingSignal (Router → policy):
   event        : user_speech_final | tool_result | transfer_to(control tool)
                | transcript_delta | programmatic
   active_agent : current token holder
   available    : listeners + parked specs (with health / TTL)
   context_view : cheap read-only projection of recent log
   session_meta : turn count, cost-so-far, timers

RoutingDecision (policy → Router):
   action     : STAY | HANDOFF(target) | FANOUT(add/remove listener) | REJECT
   target     : agent_id | AgentSpec
   seam       : CUT_NOW | AT_TURN_END | AT_IDLE
   reason     : str (audit)
   confidence : optional (LLM policies only)

class RoutingPolicy(Protocol):
    def decide(signal: RoutingSignal) -> RoutingDecision | None
    #  None = "no opinion, keep current routing"  ← the cheap 99% path
```

- **Trigger-driven, not polling** — `decide()` runs only on real events. No always-on
  inference.
- **Sync-or-async is the policy's choice** — rule policies return in µs; an LLM policy
  returns `None` immediately, kicks off async inference, feeds the result back later
  via a **programmatic signal**. LLM latency lands **off the hot path**.
- **Decision is advice** — Router health-gates + validates target before acting. A
  policy can't force a broken handoff.

### Built-in policies (ship these)

```
ControlToolPolicy   — active agent emits transfer_to → HANDOFF(target). Default. Free.
RulePolicy          — user predicates on transcript / tool_result / meta.
ProgrammaticPolicy  — app pushes a decision from outside (button, backend event).
LLMRouterPolicy     — async classifier, opt-in, off the hot path.
ChainPolicy         — ordered composite; first non-None wins. ← encodes PRECEDENCE.
```

**`ChainPolicy` order = decision precedence.** This resolves the old precedence fork:
precedence isn't hardcoded, it's the chain order the user assembles.

> **TODO(chain-default-order — see 09§E):** the shipped **default** order should not
> let the model's `transfer_to` (ControlTool) beat an explicit app/backend decision
> (Programmatic). Put Programmatic first in the default chain.

### RulePolicy predicate surface

`RulePolicy` = ordered list of `Rule(predicate, decision_template)`. First matching
predicate wins; no match → `None`. Order inside RulePolicy = local precedence;
RulePolicy's slot in the ChainPolicy = global precedence.

A predicate reads **only** the `RoutingSignal` (no sockets, no live vendor state) —
so the readable surface is exactly the signal's fields:

```
event.type          user_speech_final | tool_result | transfer_to
                  | transcript_delta | programmatic
event.payload:
    tool_result       → status, tool_name, data, retriable, agent_id
    transcript_delta  → text, agent_id, is_final
    user_speech_final → text, duration
    transfer_to       → target, args
active_agent        id, spec_id, role
available[]         candidates (id, spec_id, health, ttl)
session_meta        turn_count, cost_so_far, elapsed, app tags
context_view        read-only recent-log projection (lookback)
```

Three predicate classes cover ~all routing:

```
FIELD MATCH      tool_result.status == "escalate"
STATE THRESHOLD  session_meta.turn_count > 20  AND  status=="error" AND !retriable
TEXT MATCH       user_speech_final.text ~= /refund|cancel/   (coarse intent only)
```

Combinable with `AND / OR / NOT`. Pure predicate — no loops, no side effects →
testable, microsecond-fast.

```
Rule(when = tool_result.status=="escalate",
     then = HANDOFF(spec("human"),   seam=AT_TURN_END))
Rule(when = tool_result.tool_name=="fraud_check" AND status=="blocked",
     then = HANDOFF(spec("security"), seam=CUT_NOW))       # urgent
```

**Declarative default + callable escape hatch** (LOCKED — mirrors the projection API
in 01):
- **Declarative** predicate = a `{field, op, value}` tree. Serializable, safe,
  inspectable, no `eval`. The default; covers field/threshold/text.
  `op ∈ {==, !=, >, <, >=, <=, ~=(regex), contains, in, and, or, not}`.
- **Callable** predicate = `fn(signal) -> bool`. Full power for the rare complex
  case; opaque, unsandboxed — escape hatch only.

**Listener→active trigger dissolves into this.** Promoting a listener is just a rule
whose `then` targets a listener spec, keyed on that listener's own fanned-in signal
(its `transcript_delta` / tool call). No special trigger path — listener-promote and
agent-handoff are the same code:

```
Rule(when = transcript_delta.agent_id=="es_listener" AND text ~= /billing/,
     then = HANDOFF("es_listener", seam=AT_TURN_END))
```

Rules can emit `HANDOFF` **and** `FANOUT` (add/drop a listener) — same surface.

**Text-match trust line (LOCKED):** transcript predicates are fuzzy (STT error,
phrasing) → fine for *coarse* routing (keyword → department). **Never gate
authority/security on a text match** — that belongs in a deterministic
`tool_result.status`. Text match = intent *hint*; tool_result status = ground truth.

## The audio seam — what happens to audio during a switch

Two things move on handoff: **who feeds the ring buffer** (upstream) and **who
drains it to the speaker** (the output token, downstream). The `seam` field picks
the behavior:

```
CUT_NOW      revoke token (drain stops instantly) → FLUSH buffer (drop queued
             audio) → optional 20–50ms duck/crossfade → grant token to new agent.
             Old agent's half-sentence dropped. For barge-in / urgent reroute.

AT_TURN_END  old agent keeps token, finishes current utterance, buffer empties
             naturally, token transfers at the silence boundary. No drop, no click.
             Cost = length of remaining tail. THE DEFAULT.

AT_IDLE      wait for a natural user-turn boundary. Zero artifact, unbounded delay.
             For non-urgent role swaps.
```

Two upstream cleanups on the old agent:
1. **Token revoke** stops the *user-facing* audio instantly.
2. **Vendor cancel/interrupt** (OpenAI `response.cancel`; Gemini turn/activity
   signal) stops the *wasted generation* — the vendor may keep emitting after
   revoke. Two separate actions. Demoted agent → listener → output suppressed anyway.

### The invariant

**User hears exactly one stream at all times.** Single output token → overlap is
**structurally impossible**, even mid-swap. Worst case = a gap (maskable with
fade/filler) or a clipped tail — never two voices. Design picks the recoverable
failure.

## Promotion / demotion logic

- **Promotion** = health-gate (see 02) → output-token transfer → modality flip **only
  if the listener is text-modality** (text→audio; on Gemini a reconnect/resumption).
  An **audio-modality listener needs no flip** → the transfer is near-atomic even on
  Gemini. Input subscription already active (listener was already hearing the user).
  Never promote a stale socket; recycle-first or use a fresh standby.
- **Demotion** = release token → vendor cancel → **optional** modality flip
  (audio→text, only if demoting into a text listener; may stay audio-modality to keep
  instant re-promote) → **keep as listener** (locked: keeps instant re-promote both
  directions).

## Locked for Router

- Topology: one active + N silent listeners. Listeners kill the handoff gap
  (connected + context-current + audio-current).
- Per-listener modality (**TEXT or AUDIO, not both**; a session mixes them). Text =
  cheapest but needs a text→audio flip (Gemini reconnect) on promote; audio = costs
  audio-out but promotes with no flip. Mix per listener (keep likely-next as audio).
- OutputGate = single-producer ring buffer + atomic ownership token. Single-writer
  invariant → overlap impossible.
- Input plane = Router-controlled fan-out; output plane = single token-holder drains.
- **Mechanism (Router) vs decision (RoutingPolicy)** split.
- No default LLM router; LLM routing = opt-in async policy off the hot path.
- `RoutingPolicy.decide(signal) → decision | None`; trigger-driven; advice not command.
- Precedence = `ChainPolicy` order (not hardcoded).
- RulePolicy = ordered `Rule(predicate, decision)`; **declarative default + callable
  escape hatch**; predicate reads only the RoutingSignal; listener-promote = a rule
  targeting a listener spec (no special path); text match = intent hint only, never
  authority.
- Three seam modes (`CUT_NOW` / `AT_TURN_END` default / `AT_IDLE`); token revoke +
  vendor cancel are separate actions.
- Demote-to-listener; single-stream invariant.
