# ToolCallRegistry (in-flight tracker)

## Purpose

The single authority tracking every in-flight `ToolCall` from vendor-emit to
terminal resolution, keyed by `call_id`. **Guardian of the invariant:** every
`call_id` resolves to exactly one terminal result — never zero, never two.

Owned/driven by the **Router**. One per user-session.

## Responsibilities

1. **Register** — vendor emits function-call → create entry by call_id.
2. **Correlate** — call_id → `origin_connection` (return path) + `destination`
   (handler / handoff / reroute / deferred-external).
3. **Track lifecycle** — internal FSM. Never surfaced to the model.
4. **Hold the future** — entry is a resolvable promise; terminal result resolves
   it. Supports late/out-of-band resolve (deferred).
5. **Enforce single-resolution** — reject double-resolve.
6. **TTL / timeout** — per-entry deadline → auto-resolve `timeout`.
7. **Cancel** — turn abandoned (barge-in / handoff / close) → sweep → `cancelled`.
8. **Liveness on resolve** — origin dead (session ended / handed off) → drop or
   reroute the late result.
9. **Batch coordination** — a response spawns N parallel calls; know when the
   whole batch is done → signal turn continuation. *(logic deferred)*
10. **Feed the log** — register → `tool_call` event; resolve → `tool_result` event.
11. **Backpressure** — cap concurrent in-flight calls per session.

## Form

```
ToolCallRegistry:
  entries: dict[call_id → PendingCall]          # O(1) lookup

  PendingCall (slots/struct):
     call_id, tool_name, args
     origin_connection_ref     # liveness-checkable (weakref/id)
     destination               # handler | handoff | reroute | deferred-external
     state                     # FSM enum, internal only
     future                    # awaitable → resolved with ToolResult
     created_at, deadline      # per-tool budget; deferred = longer TTL
     response_group_id         # which response/turn batch
     schedule                  # deferred only: interrupt|when_idle|silent

  indexes:
     by_response_group: response_group_id → set[call_id]   # batch-complete trigger
     by_connection:     connection_id     → set[call_id]   # sweep-cancel on handoff/close
```

Why the indexes (not just the dict):
- **by_response_group** → know when all parallel calls in a turn are done → trigger
  continuation. Without it you can't tell "turn finished."
- **by_connection** → handoff/close cancels all of a connection's calls in one
  sweep, not a linear scan.

## Lifecycle FSM (per entry)

```
received → validating → executing → resolving → done
              ↘ validation fail → done(invalid_args)
executing → awaiting_external → done      # deferred path
any → cancelled     # turn abandoned
any → timeout       # deadline fired
```

Note: `executing` is a **lifecycle state, internal only** — it is NOT a terminal
status and never goes in the envelope. The model is blocked on `call_id` waiting
for a **terminal** result; a progress ping it can't act on has no place there. The
only in-flight-ish signal that reaches the model is `deferred` (the interim ack).

## Cancel / Timeout (the tricky part)

Both force a terminal result **without the handler completing naturally**, uphold
the one-result invariant, and clean up.

### TIMEOUT — one call blew its budget

```
trigger: deadline fired (created_at + budget)
action:  resolve call_id as `timeout` → emit tool_result → clear entry
budget:  per-tool configurable; deferred calls get a SEPARATE, longer TTL
```

### CANCEL — the turn was abandoned

```
triggers: barge-in | handoff | session close
action:   SWEEP the relevant index → cancel each call_id → resolve `cancelled`
scope decides which index:
   barge-in → by_response_group   (just the current response's calls)
   handoff  → by_connection       (all of the replaced agent's calls)
   close    → everything
```

### Shared hard problems

1. **Stop the running handler.** `task.cancel()` — co-operative cancellation
   (`CancelledError`; handler cleans up in `finally`). Double-resolve guard is the
   backstop.
2. **Side effects are NOT rolled back.** Cancel/timeout stop the model's *wait* and
   free the slot; they do not undo work. A tool that charged a card can't be
   un-charged. Rollback/compensation = the **handler's** responsibility (act on
   `CancelledError`). Framework signals; it doesn't roll back.
3. **Double-resolve guard / atomic resolution.** First terminal wins; the rest
   no-op + log. **Single event loop makes this lockless** — resolve and timeout
   callbacks both run on the loop, serialized; the second sees `done`.
4. **Cleanup both ways.** Normal resolve → cancel the timeout timer. Timeout/cancel
   → cancel the handler task + remove from `entries` + both indexes. Leaking timers
   or entries bleeds memory over a long session.
5. **Late result after resolution.** Handler / deferred-external resolves after
   close → guard drops it. Dead origin → drop or reroute.

### Cancel is part of barge-in

```
user interrupts →
   OutputGate: stop agent audio NOW
   invalidate current model response
   ToolCallRegistry: sweep by_response_group → cancel in-flight calls   ← this
```
The registry offers the sweep; the Router pulls the trigger.

## "Handled elsewhere" and "result passed later"

Correlation is **always via `call_id`**, tracked in the pending entry
(`origin` + `destination`). Two distinct outcomes:

- **`skipped`** = close NOW. No late result coming for this call_id. The intent was
  consumed by a different mechanism (handoff, dedup, reroute). `skipped` closes the
  loop so the originating connection's `call_id` doesn't dangle.
- **`deferred`** = keep `call_id` OPEN. A result IS coming later from "someone
  else." The entry becomes a **resolvable future**: the Router hands the destination
  a `resolve(call_id, ToolResult)` handle. The late result is surfaced per its
  `schedule`. *(Deferred feature — see 07 for vendor support; registry entries are
  designed as futures from day one so it slots in without a rewrite.)*

## Boundaries (what it does NOT do)

- **Not authority** — Router decides blocked/skipped/reroute. Registry records the
  decision + outcome.
- **Not definitions** — `ToolRegistry` holds `Tool{schema, handler}`.
- **Not serialization** — the adapter encodes to the vendor wire.
- **Not durable history** — the log is the home. Registry is transient; entries die
  on resolution.

## Concurrency / perf

- Per-session single loop → mostly lock-free.
- External/deferred resolve from another thread must marshal onto the session loop
  (e.g. `call_soon_threadsafe`) — never mutate the registry cross-thread.
- `dict` by call_id = O(1); entries are small slotted structs; **cleared on
  resolve** → bounded memory; concurrent cap → bounded. Keep `PendingCall` flat.
