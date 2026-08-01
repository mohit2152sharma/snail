# Multi-step Tool Runs (tools that wait on external input)

Extends **03 (tool layer)** and **04 (tool call registry)**. Neither is amended — this
sits on top of both and preserves their locked rules.

Design history: `scratchpad/tool-call.md` (problem), `scratchpad/tool-call-approaches.md`
(solution space, 6 dimensions × 11 computation models).

## The problem

A tool's execution can depend on something outside the process — user consent, a
confirmation, a chosen option, a value only the user knows. The tool cannot finish until
that arrives.

The vendor makes this awkward: Gemini emits a `tool_call_id` and expects it echoed back
in a `ToolResponse`. **Any** response closes that loop — including one that means "I'm
not done yet."

We do not fight this. We accept that the call closes, and make *call*-terminality and
*run*-terminality two different things.

## Two loops, different terminality

```
PendingCall   = the vendor's loop.    Opened by tool_call_id. MUST close exactly once.
ToolRun       = the executor's loop.  Spans N PendingCalls. Closes when the work is done.
```

```mermaid
flowchart TB
    subgraph L["what the LLM sees — only CLOSED calls"]
        c1["fc_1 get_weather<br/>→ input_required"]
        c2["fc_2 provide_input<br/>→ success"]
    end
    subgraph E["what the executor holds"]
        r["ToolRun R1<br/>executing → blocked → executing → done"]
    end
    c1 -. opens .-> r
    r -. closes fc_1, intermediate .-> c1
    c2 -. resumes .-> r
    r -. closes fc_2, terminal .-> c2
```

`input_required` is **terminal for the call, intermediate for the run.** The LLM treats
it as an answer and moves on. The executor knows better.

**No run identifier ever crosses the LLM boundary.** The key it echoes back is a semantic
name it can re-derive from the conversation it just had — not opaque state it must
safekeep.

## The protocol — one entry, two exits

The whole contract:

```
ENTRY   a tool call arrives
EXIT    exactly one of:
          FINAL          — here is the result
          INPUT_REQUIRED — here is what I need; ask the user, then call provide_input
```

A tool with no external dependency simply never takes the second exit. Its declaration
to the vendor is unchanged. Nothing about authoring it changes.

`provide_input` is **not special to the executor**. It arrives as a tool call and takes
the same two exits. A run that blocks twice produces `input_required` from the first
`provide_input` and is resumed by a second.

## `InputRequired` — the thing a run blocks on

One pending question, with a typed answer.

```python
InputRequired(
    key      = "location_permission",
    expects  = "boolean",                 # boolean | string | number
    ask      = "ask whether you may use their current location …",
    budget_s = 60,
)
```

*Naming: `Ask`, `Need`, and MCP's `Elicitation` were the alternatives.
`InputRequired` was chosen because it matches the status (`input_required`) and the
framework tool (`provide_input`) with no ripple — one word across the whole vocabulary.*

Handlers stay linear:

```python
async def get_weather(args, ctx):
    granted = await ctx.require("location_permission")
    if not granted:
        return ToolResult.blocked("the user declined location access")
    lat, lon = await device_location()
    r = await weather_api(lat, lon, unit=args.get("unit", "C"))
    return {"temp": r.temp, "condition": r.condition}
```

### Declared at setup, or created at runtime — OPEN

Both work. The executor never needs to *look up* what a key means: it created the
`InputRequired` moments earlier and is holding it in the slot, so it already knows the
type and can validate the answer against it.

Declaring on the `Tool` (`requires=(InputRequired(...),)`) buys three things: the
setup-time enum that steers the model toward valid keys, a home for default wording and
budget, and introspection of what a tool might ask for without reading its body. It costs
the ability to ask for something undeclared, and two places to keep in step.

Runtime-only means the handler passes everything at the call site:

```python
granted = await ctx.require(
    "location_permission",
    expects="boolean",
    ask=f"ask whether you may use their location to check the weather in {city}",
)
```

Not settled. See **O4**.

## The `provide_input` declaration

One static declaration, session-wide, `is_framework=True`.

If keys are declared at setup, `key` carries an enum **built by walking the tool
registry**, which bounds the set of possible questions before the session starts. If
requirements are created at runtime, `key` degrades to a plain `STRING` validated
server-side against the slot — the design behaves identically, the model just gets less
steering.

```json
{
  "name": "provide_input",
  "description": "Supply a value that a tool asked for. Only call this after a tool result asked for input.",
  "parameters": {
    "type": "OBJECT",
    "properties": {
      "for_tool":     { "type": "STRING",  "description": "copy exactly from the tool result" },
      "key":          { "type": "STRING",  "enum": ["location_permission"],
                        "description": "copy exactly from the tool result" },
      "bool_value":   { "type": "BOOLEAN" },
      "text_value":   { "type": "STRING"  },
      "number_value": { "type": "NUMBER"  }
    },
    "required": ["for_tool", "key"]
  }
}
```

**The key is the authority on type, not the slot.** The executor resolves `key` against
the blocked run's `InputRequired` and validates the corresponding slot. Wrong slot filled
→ `invalid_args`, retriable, the run stays blocked and still answerable.

Booleans are real booleans. No `"yes"`/`"no"` string parsing anywhere.

`for_tool` is proposed for removal — see **O1**.

## The `input_required` envelope

A new `ToolStatus.INPUT_REQUIRED`. Unlike every other status, its `response` carries
structured fields rather than a flattened string:

```json
{
  "status":   "input_required",
  "for_tool": "get_weather",
  "key":      "location_permission",
  "expects":  "boolean",
  "ask":      "ask whether you may use their current location to check the weather"
}
```

`ask` is where all runtime dynamism lives — it never needs to appear in any schema.
`response_mode: SPEAK`, with the ask as the directive.

## The slot — latest always wins

The executor holds **one run**. Not a table, not a queue.

```
active_run:
    run_id            # internal only; for log correlation, nothing looks it up
    tool_name
    state             # executing | blocked
    pending           # the InputRequired, while blocked
    carrier_call_id   # which call receives the next output; None between outputs
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> executing: any tool call (not provide_input)
    executing --> blocked: ctx.require(...)
    blocked --> executing: provide_input accepted
    executing --> [*]: completed / failed
    executing --> [*]: displaced by a newer call
    blocked --> [*]: displaced by a newer call
    blocked --> [*]: budget expired
```

### Locked rules

1. **Any tool call that is not `provide_input` takes the slot.** Whatever was there —
   executing or blocked — is cancelled. This holds uniformly: within a turn, across
   turns, always. The latest thing the user asked for is the only thing that matters.
2. **`provide_input` never takes the slot.** Blocked slot → feed the value and resume.
   Empty slot, or slot executing → `skipped`.
3. **A finished run** closes its carrier call with the result and empties the slot.
4. **A blocked run** closes its carrier call with `input_required` and keeps the slot.
   `carrier_call_id` becomes `None` until the next call arrives.
5. **A cancelled run's carrier call, if still open, is closed with `skipped`.** Gemini is
   waiting on that `tool_call_id`; it must be answered. This upholds 04's one-result
   invariant — nothing is left hanging.
6. **Key mismatch → `skipped`.** The value is discarded; the blocked run is left
   untouched and still answerable.

### "Silently" means silent, not invisible

Every rule above that says `skipped` means: **the response is sent** (the model is never
left hanging) with `response_mode: SILENT` and no speak directive. The model is not
prompted to say anything about it.

`ToolResult.skipped()` (`tools/result.py:110`) already produces exactly this. No change.

Note the limit of "silent": `TOOL_RESULT` maps to a real conversation item in
`context/projection.py:44`, so a skipped result still sits in history reading
*"handled elsewhere"*. Silent controls speech, not context.

## Worked example — `get_weather`

### Tool definition

```python
Tool(
    name="get_weather",
    description="Current weather at the user's location.",
    input_schema={
        "type": "OBJECT",
        "properties": {"unit": {"type": "STRING", "enum": ["C", "F"]}},
    },
    output_schema={
        "type": "OBJECT",
        "properties": {"temp": {"type": "NUMBER"}, "condition": {"type": "STRING"}},
        "required": ["temp", "condition"],
    },
    requires=(
        InputRequired(
            key="location_permission",
            expects="boolean",
            ask="ask whether you may use their current location to check the weather",
        ),
    ),
    handler=get_weather,
)
```

### System instruction

Deliberately short — long instructions drift in live models.

```
Some tool results ask for input instead of giving an answer. When a result has
status "input_required":
  1. Ask the user the question in "ask", in your own words and voice.
  2. Wait for their answer.
  3. Call provide_input, copying "key" exactly from that result, and putting the
     user's answer in the slot named by "expects":
       boolean -> bool_value, string -> text_value, number -> number_value.
  4. Never supply a value the user did not actually give. If they refuse, decline,
     or change the subject, do not call provide_input.

Some tool results have status "skipped". Say nothing about them and carry on.
```

Rule 4 is the only guard against the model reporting `true` when the user said no. It is
an instruction, not an enforcement — it reduces the risk, it does not remove it.

### Wire trace

```
user: "what's the weather?"
```

```json
{"toolCall": {"functionCalls": [{"id": "fc_1", "name": "get_weather", "args": {}}]}}
```

Run takes the slot → hits `ctx.require` → `blocked`. Carrier `fc_1` is closed:

```json
{"toolResponse": {"functionResponses": [{
  "id": "fc_1", "name": "get_weather",
  "response": {
    "status": "input_required",
    "key": "location_permission",
    "expects": "boolean",
    "ask": "ask whether you may use their current location to check the weather"
  }
}]}}
```

`fc_1` is closed. Gemini has nothing pending. R1 is still in our slot.

```
model: "Sure — is it alright if I use your location?"
user:  "yeah go ahead"
```

```json
{"toolCall": {"functionCalls": [{
  "id": "fc_2", "name": "provide_input",
  "args": {"key": "location_permission", "bool_value": true}
}]}}
```

Slot blocked, key matches, `bool_value` matches `expects` → resume → finish:

```json
{"toolResponse": {"functionResponses": [{
  "id": "fc_2", "name": "provide_input",
  "response": {"status": "success", "data": {"temp": 31, "condition": "haze"}}
}]}}
```

```
model: "It's 31 degrees and hazy."
```

### Displacement

If the user changes the subject while R1 is blocked:

```json
{"toolCall": {"functionCalls": [{"id": "fc_2", "name": "book_cab", "args": {…}}]}}
```

`book_cab` is not `provide_input` → takes the slot → R1 cancelled. R1's call was already
closed, so nothing dangles. A late `provide_input` as `fc_3` finds the slot holding
`book_cab`, not blocked → `skipped`, silent. The stale answer is discarded.

## Event log

Run transitions are **control events, not conversation.** A new `EventType.TOOL_RUN`
carries the transition in `meta`.

`context/projection.py:50` returns `None` for unrecognised types — *"HANDOFF and any
future control-only events: not conversation context"* — so `TOOL_RUN` is invisible to
the model by construction. This matters: the run state we deliberately kept out of the
LLM must not leak back in through the projection.

| transition | meta |
|---|---|
| `started` | run, tool, call_id |
| `blocked` | run, key, expects, deadline |
| `resumed` | run, key, value |
| `displaced` | run, displaced_by, call_id |
| `completed` | run, status |
| `failed` | run, reason |
| `timed_out` | run, key |
| `input_rejected` | key, why: `no_blocked_run` \| `key_mismatch` \| `type_mismatch` |

Full trace for the worked example:

```
tool_call    fc_1  get_weather
tool_run     started      run=R1 tool=get_weather
tool_run     blocked      run=R1 key=location_permission expects=boolean
tool_result  fc_1  input_required
tool_call    fc_2  provide_input
tool_run     resumed      run=R1 key=location_permission value=true
tool_run     completed    run=R1
tool_result  fc_2  success
```

## Lifecycle

- **Budget belongs to the `InputRequired`**, not the tool. `Tool.timeout_s` bounds
  execution; a blocked run is bounded by how long a human takes. Different clocks. Reuse
  the existing `sweep_timeouts` pump rather than inventing a timer.
- **Barge-in does not cancel a blocked run.** In a voice session the interruption is
  frequently the answer. Barge-in still cancels actively-`executing` work, unchanged.
- **The slot is session-scoped, not connection-scoped.** A handoff mid-wait does not
  strand the run; the answer binds regardless of which agent relays it.
- **Side effects are never rolled back** (locked, doc 04). Displacement makes this easier
  to hit: a run that charged a card and then blocked is not refunded when displaced.
  Compensation remains the handler's job, on `CancelledError`.

## Deliberately not in scope

- **Machine-originated external factors** (webhook, background job, timer). They need a
  push path — a result delivered with no open call to carry it — which Gemini exposes via
  `scheduling` and OpenAI via injected item + `response.create`. `Tool.non_blocking`
  (live, used at `vendor/gemini.py:174`) and `PendingCall.schedule` (unused) are reserved
  for it. Not v1.
- **Concurrent runs / queued questions.** Latest-wins makes at most one run exist. The
  cost is real: two tool calls in one model response means the first is skipped and the
  user silently loses half of what they asked for. Accepted.
- **Restart durability.** A voice session dies with its websocket; serializable run state
  buys nothing. Inspectability comes from the event log instead.
- **Topic-change classification.** Rule 1 is purely structural — any non-`provide_input`
  call displaces. No classifier, because a classifier reintroduces exactly the
  nondeterminism this design removes.

## Impact on existing code

### Added

| Change | Where |
|---|---|
| `ToolStatus.INPUT_REQUIRED` | `tools/result.py` |
| `InputRequired`, `ToolRun`, the slot, `ctx.require` | `registry/` (beside `PendingCall`) |
| slot branch in dispatch; run resume path | `session/session.py:141` `_run_tool` |
| `provide_input` registration | `tools/registry.py` — **not** routed through the Router; it is a resume, not authority or handoff |
| **structured** tool responses | `vendor/` — `serialize_tool_result` takes `content: str` and `session.py:211` flattens via `_result_content`; `input_required` needs named fields in `response` |
| `EventType.TOOL_RUN` | `context/events.py` |

### Removable — proposed, pending decision

| Delete | Why it becomes dead |
|---|---|
| `CallState.AWAITING_EXTERNAL` (`registry/pending.py:26`) | the call never waits; it closes immediately with `input_required`. This state was for the hold-the-call-open approach we rejected |
| `Destination.DEFERRED_EXTERNAL` (`registry/pending.py:44`) | no reference anywhere |
| response-group machinery — `_by_group`, `response_group_id`, `sweep_response_group`, `group_size`, `group_call_ids`, `_group_counter`, `_current_group` (registry + session + `router.py:219`) | groups answer "which calls belong to this model response". Latest-wins makes one run exist, so barge-in cancels *the* run. Real capability loss (doc 04's batch-completion detection) but already unreachable. Tests: `test_call_registry.py:77-83`, `test_router.py:165-168` |
| `_tool_tasks: dict` (`session.py:75`) | one run → one task reference |
| `ToolStatus.DEFERRED` (`result.py:27`) | unused, and now sits confusingly beside `INPUT_REQUIRED`. Either delete until the machine-factor path lands, or document the split: `DEFERRED` keeps the call open, `INPUT_REQUIRED` closes it |

`_by_conn` stays — handoff still needs it. `Tool.non_blocking` stays — live in the Gemini
adapter.

### Pre-existing duplication worth fixing first

`tools/executor.py:execute()` and `Session._invoke()` (`session.py:194`) implement the
same envelope: validate args → run handler → validate output → wrap. `execute()`'s
docstring calls itself *"the pure, testable core they wrap"*, but nothing wraps it — it is
imported only by `tools/__init__.py` and exercised only by `test_tools.py:96-121`.
Production runs the copy in the session.

Adding runs makes a third path through the same logic. Collapse to one async-capable
envelope before building on it, or the run path drifts from the one-shot path the first
time either is touched.

## Open items

**O1 — drop `for_tool`.** `key` already identifies the blocked run, and the executor
knows which tool declared it. `for_tool` can only *reject a correct answer*: if
`location_permission` is declared by both `get_weather` and `book_cab`, and `get_weather`
is displaced by `book_cab` which blocks on the same key, the model may still echo
`for_tool: "get_weather"` — right answer, right key, rejected on a field that contributed
nothing. Removing it drops one model-filled field, one rejection reason, and one line of
system instruction.

**O2 — value typing.** Option A (above): one `provide_input`, three typed optional slots.
Option B: `provide_bool` / `provide_text` / `provide_number`, each with a single required
typed `value`, and `expects` disappears from the envelope because the tool name *is* the
type. B gives the model an easier target and nothing optional; A keeps the surface at one
tool.

**O3 — structured answers.** Three primitive slots do not cover an address or a list. The
escape hatch is JSON inside `text_value`, parsed and validated server-side — unlimited
shape, no vendor-side enforcement, so malformed output becomes a retry loop. Left out
until a real tool needs it.

**O4 — declared vs runtime `InputRequired`.** See above. Declaration buys the enum,
defaults, and introspection; runtime-only is one less concept and behaves identically.

**O5 — verify against the live API**, not from the spec:

- Does Gemini Live honour `enum` inside a function declaration's parameters?
- Does it accept a properties-less `OBJECT` parameter? If so, a single free-form `value`
  replaces the typed slots and handles structured answers natively, collapsing O2 and O3.
- Does the model reliably fill the slot named by `expects`?
- Does a stale `input_required` result lingering in history cause the model to re-ask?

## What this design does not guarantee

Stated plainly, because the machinery can look stronger than it is.

- **That the value is correct.** If the user says no and the model calls
  `provide_input(bool_value=true)`, the executor binds it to the right run and gets the
  wrong answer. Nothing here catches that.
- **That the model calls `provide_input` at all.** If it just talks, the run sits until
  its budget expires.
- **That the model asks what we asked it to ask.** Speech-to-speech models paraphrase and
  drift; doc 03 already flags this for `verbatim` directives.

What it *does* guarantee: exactly one candidate run for any incoming value, exactly one
response per `tool_call_id`, and no executor state that a model mistake can corrupt.
