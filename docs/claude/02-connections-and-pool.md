# Connections & Pool

## AgentSpec ≠ AgentConnection

```
AgentSpec       — declarative config. Cheap data. No I/O.
                  = static identity: model, voice, system_instruction, tools
AgentConnection — live vendor WebSocket. Expensive.
                  Lifecycle: cold → connecting → warm → active → closed
```

Building an `AgentSpec` must **not** open a socket. The framework owns the
connection lifecycle and a connection pool. Users can override.

The separation pays off repeatedly: a connection can be **recycled** (socket
swapped) while the `AgentSpec` and everything above (Router, roles, output token)
stay pointed at the same logical agent. `AgentSpec (stable) → AgentConnection
(swappable)`.

## SetupParam vs JoinContext (verified vendor split)

```
SetupParam  (bound at connect, BOTH vendors):  model, voice, system_instruction, tools
            = the agent's STATIC identity
JoinContext (injected on join, BOTH vendors):  history, per-client facts
            = genuinely dynamic per-client data
```

- Decided to bind `system_instruction` **and** `tools` at **setup** for **both**
  vendors (symmetric), even though OpenAI *could* inject them later. Uniform
  mental model beats the OpenAI-only micro-optimization.
- Empty string / empty instruction is allowed (agent with no persona steering).

### Why this split (verified against Gemini docs/SDK)

- Gemini Live does **not** support `role="system"` content turns, nor mid-session
  config/instruction/tool updates. Valid content roles = `user`, `model` (+`tool`
  for function results). Official Google forum confirmation:
  *"You cannot update the configuration while the connection is open."*
- So instructions/tools must be set at **setup**. We made it symmetric for both
  vendors.
- **Client history** is injected as `user`/`model` content turns on join, **before
  the first model turn**.

### Consequence (accepted)

Per-client *instructions/tools* are **not** dynamic — they are a distinct
`AgentSpec` = distinct pool bucket. Cost scales with **instruction-variants**, not
with clients. (Document this so users don't expect free per-client persona.)

## Pool

The framework **pre-opens sockets** (handshake + auth done), holds them in a
pool, and **injects client context on join**. Saves ~100–300ms handshake off the
critical path.

- **No cross-session session reuse.** Realtime sessions are stateful and
  conversation-bound (unlike DB connections). The pool is **per-AgentSpec**,
  scoped to a user-session.
- **Pool key** = `(vendor, model, system_instruction, tools, ...)` → uniformly
  **per-AgentSpec**. One rule, both vendors. (A coarser model-keyed pool for
  OpenAI is possible later — the pool key is a vendor-supplied function so it's a
  config flip, not a rewrite. Deferred.)

### Pool jobs

```
pre-warm   — open + configure likely-next agent before it's needed (predictive)
park       — after handoff-away, keep prior agent warm+idle (fast return)
keepalive  — ping/refresh to survive vendor IDLE-timeout
recycle    — vendor MAX-duration → reconnect + restore context transparently
evict      — under memory/cost pressure, drop cold-parked agents
admission  — GLOBAL cap on total warm sockets → pre-warm is best-effort,
             falls back to lazy connect. Respects cost + vendor concurrency limits
```

Connection timing policy: predictive pre-warm is the default (warm the likely next
agent in the background before it's needed); lazy connect is the fallback; eager
"warm everything" is an opt-in flag.

## Connection recycle / vendor timeout handling

Vendor sessions have hard limits (idle-timeout **and** max-duration). A pooled
listener may hit the limit before it is promoted. Handling:

### Two recycle paths — assigned vs unassigned

A connection can be recycled in two situations, and they differ sharply:

```
ASSIGNED   (bound to a client/session — has JoinContext + live conversation):
   - recycle MUST restore context (native resume OR log-replay)
   - time-sensitive (client is live)
   - invisibility depends on role (listener = anytime; active = safe boundary)
   - this is the case detailed in the steps below

UNASSIGNED (pool standby — SetupParams only, no client bound yet):
   - recycle = fresh socket + re-apply SetupParams. NO context to restore.
   - not time-sensitive → background housekeeping
   - fully invisible (no client attached)
   - still ages toward vendor max-duration → MUST be recycled too, so a promoted/
     assigned standby is never stale. A "fresh standby" = an unassigned socket
     kept young by this path.
```

Both paths are driven by the same `ConnectionLifecycleManager` and the same
`deadline − margin` scheduling; the difference is only *whether context must be
restored*. The unassigned path is the cheap common case; the assigned path is the
careful one.

### Enabling insight: connections are disposable

Because context lives in the **append-only log** (not the vendor session), any
connection is **reconstructable from the log**. Recycle = kill + rebuild-from-log,
invisibly. We never depend on the vendor holding state.

### Steps

1. **Know the deadline.**
   - OpenAI Realtime: `session.created` carries `expires_at`.
   - Gemini Live: sends `GoAway` (with `timeLeft`) before terminating; plus
     `session_resumption` updates and known duration limits.
   - Store per connection: `{created_at, vendor_deadline, resumption_handle?,
     last_activity, health}`.

2. **Recycle proactively, not reactively.**
   ```
   recycle_at = vendor_deadline − safety_margin   # e.g. ~80% of limit
   on GoAway  → recycle immediately
   keepalive  → periodic ping to dodge IDLE-timeout
   ```
   Two vendor timeouts, two responses: idle-timeout → keepalive; max-duration →
   recycle (keepalive cannot beat max-duration).

3. **Restore context on recycle (capability asymmetry).**
   - Gemini: native `session_resumption` → pass `resumption_handle` → resume with
     context intact. Cheap, near-seamless.
   - OpenAI: no native resumption → open new socket + **replay context from the
     log projection**. Manual, heavier. (Log-replay works for both; Gemini
     resumption is just the fast lane.)
     > **TODO(log-completeness — see 09§E):** "heavier" is under-budgeted. OpenAI
     > replay **re-bills the full context** every recycle and costs real wall-time
     > mid-conversation on a long session. Need a latency/cost budget + masking
     > plan (and depends on the compaction policy from 01).

4. **Invisibility depends on role.**
   - Listener (not speaking): recycle any time — fully seamless.
   - Active (speaking): recycle at a **safe boundary** (turn-end / idle), scheduled
     proactively so it never hits the deadline mid-turn. Forced mid-turn → Gemini
     resume = seamless; OpenAI = brief gap, mask with a filler ("one moment").

5. **Promotion pre-flight (health gate).**
   ```
   before promoting a listener → check TTL headroom.
      healthy + margin → promote (instant)
      near deadline    → recycle first, then promote
                         (or promote a fresh listener / keep a fresh standby)
   NEVER promote a stale connection into the active seat.
   ```
   Proactive recycle keeps listeners essentially always promotion-ready, so the
   pre-flight almost always passes instantly.

### Component

```
ConnectionLifecycleManager (in the pool):
   per-connection: deadline, health, resumption_handle, last_activity
   schedules proactive recycle (deadline − margin)
   handles GoAway / idle-keepalive
   recycle = new socket + restore context (native resume OR log-replay)
             + ATOMIC swap of the connection reference
```

Recycle swaps the underlying `AgentConnection` while the `AgentSpec` and all upper
layers stay pointed at the same logical agent. Upper layers never see the socket
change.
