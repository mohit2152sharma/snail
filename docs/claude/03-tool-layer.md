# Tool Layer

## Two registries — do not merge

```
ToolRegistry      = static catalog. name → Tool{input_schema, output_schema, handler}.
                    Session/global. "What tools exist." Reusable across agents.
ToolCallRegistry  = live in-flight tracker. call_id → PendingCall. Per-session.
                    "What calls are happening now." (see 04-tool-call-registry.md)
```

## The Tool object (vendor-independent, reusable)

```
Tool = name + input_schema + output_schema + handler   # stateless
```

- **Stateless.** No result state on the Tool (see below). Reused across agents and
  concurrent calls.
- **Vendor-independent.** Written once, works on any agent/vendor.
- Lives in a session/global `ToolRegistry`, referenced by name from multiple
  `AgentSpec`s. Adapter serializes its schema to the vendor format at warm time.

## Schema policy: A — common denominator

Neutral schema dialect = the **common denominator** of both vendors (what BOTH
support). Portable, constrained. This is what makes a `Tool` reusable across any
agent/vendor with no per-vendor validation branching.

Vendor schema differences reconciled by this choice:
```
OpenAI Realtime:  JSON-Schema-ish, lowercase types
Gemini Live:      OpenAPI subset — UPPERCASE types (STRING/NUMBER/OBJECT),
                  `nullable` not type-arrays, no $ref, limited formats/keywords
```

## Exposure ≠ Authority

```
AgentSpec.tools = EXPOSURE  — which tools an agent SEES (serialized into vendor
                              setup as function declarations). Per-spec, setup-time.
Router          = AUTHORITY — whether a requested call actually RUNS. Central, runtime.
```

An agent can only *request* tools exposed to it, but even for an exposed tool the
Router arbitrates execution. Different layers — don't conflate.

## Vendor call = intent, not command

The model emitting a function-call is a **request**, not an order. The Router
decides whether/which/how it executes.

```
vendor emits  ToolCall(call_id, name, args)     ← the model's REQUEST (intent)
   → Router intercepts:
        classify   — framework/control tool (handoff) vs agent tool
        authorize  — is this agent allowed this call right now?
        decide     — execute / reject / reroute / trigger handoff
   → if execute:  ToolRegistry.dispatch(handler) → ToolResult
   → adapter returns result to the requesting (or post-handoff) agent
   → model continues its turn
```

**Handoff = a control tool the Router catches.** `transfer_to(billing)` is a
framework-registered control tool; the Router recognizes it and runs handoff
logic instead of a plain handler. Unifies LLM-driven handoff with normal tool
dispatch at one interception point.

Distinguish **agent tools** (user business logic) from **framework tools**
(handoff/control, injected by the framework).

## ToolCall vs ToolResult (naming, correlated by call_id)

```
ToolCall     vendor → framework   # the model's REQUEST  = {call_id, name, args}
ToolResult   framework → vendor   # the EXECUTION output = {call_id, ...envelope}
```

Neither lives on the `Tool` (stateless). Both are transient carriers, correlated
by `call_id`, and both recorded as events (`tool_call`, `tool_result`) in the log.

## Where the tool result lives

The **durable home = the append-only log**, as a `tool_result` event.

```
ToolCall arrives (call_id)
   → pending-call registry: call_id → awaiting      # transient, executor-owned
   → handler runs → value
   → APPEND tool_result event to log                # THE home (durable)
   → adapter reads it → serializes to vendor → model continues
   → clear pending entry
```

Never on the Tool; never persistently on the AgentConnection. `call_id` binds the
result to the connection that **made** the call (matters at handoff seams).

## output_schema

- **Required.** Defines the shape of `data` on success; the handler's success
  return must conform and slots into `data`.
- Input and output schemas are both the tool's **interface**, same dialect
  (common-denominator), same reuse guarantee.
- Handler returns a **neutral structured value** (not a vendor payload); the
  **adapter** does per-vendor encoding (OpenAI wants a string, Gemini wants an
  object).

## The ToolResult envelope (standard contract)

Every result the model sees has one consistent shape + a status enum.

```
ToolResult = {
  status:     success | error | blocked | skipped | invalid_args | timeout
              | invalid_output | not_found | cancelled     # (deferred: async 'deferred')
  data:       <output_schema-shaped>     # success only; output_schema binds here
  reason:     <model-facing str>          # non-success; sanitized
  retriable:  bool                        # model's retry hint
  response_mode:   speak | silent         # talk or stay quiet
  speak_directive: {mode: hint|verbatim, text} | null
}
```

(The `code` field was considered and dropped — reason strings suffice.)

### Status taxonomy

| status | when | data | retriable | model-facing reason |
|---|---|---|---|---|
| `success` | handler ran, output valid | ✅ value | — | — |
| `error` | handler raised at runtime | — | maybe | sanitized message |
| `blocked` | Router denied (authz/policy) | — | no | "not permitted" |
| `skipped` | Router chose not to run (deduped, superseded, handled by handoff) | — | no | "handled elsewhere" |
| `invalid_args` | ToolCall failed **input_schema** | — | **yes** | validation detail so model self-corrects |
| `timeout` | handler exceeded budget | — | maybe | "timed out" |
| `invalid_output` | handler return failed **output_schema** | — | no | **generic** (tool-side bug; real detail → log only) |
| `not_found` | model called unknown/unexposed tool | — | no | "tool does not exist" |
| `cancelled` | turn abandoned (barge-in / handoff) | — | no | may never reach the model |
| `deferred` *(deferred feature)* | async long-running: ack now, result later | — | — | interim ack |

### Locked rules

- **output_schema required**, binds as `data` on success.
- **Every `call_id` → exactly one enveloped result.** Turn never hangs.
- **Sanitization boundary:** model gets `status/reason/retriable/data`; raw errors
  (stack traces, internals) → **log only**. Security + avoids derailing the model.
- **`retriable`** shipped in v1.

## Speech directives (make the model talk, or not)

Separate from status: whether the model **speaks** on a result, and **what** it
conveys.

```
response_mode: speak | silent          # not every tool result should make the agent talk
speak_directive = {
  mode: "hint" | "verbatim"
  text: "Apologize, say you couldn't process the request"
}
```

- **hint** (default, portable): natural-language instruction; the model paraphrases
  in its own persona. Rides in the result content → works on both vendors.
- **verbatim** (best-effort): instruct the model to say exact words. **Not
  guaranteed** — in speech-to-speech the vendor owns the voice. Accepted as
  best-effort.

### Directive cascade (defaults → overrides)

```
framework defaults per status:
   error   → hint: "briefly apologize, say you couldn't process the request"
   blocked → hint: "tell the user you're unable to do that"
   timeout → hint: "say it's taking too long, ask to try again"
   success → silent or natural (model speaks from `data`)
overridable at:
   per-tool   (this tool's error apology is custom)
   per-call   (Router/handler sets a specific directive for this invocation)
```

### Vendor paths

- **Portable baseline:** directive rides in result content → both vendors' models
  read it and speak accordingly.
- **OpenAI bonus:** `response.create` takes per-response `instructions` → reinforce
  the directive for just that spoken turn.
- **Gemini:** no clean per-response instruction override → relies on in-content
  directive.
