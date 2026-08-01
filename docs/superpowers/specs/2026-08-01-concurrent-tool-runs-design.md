# Concurrent tool runs — many questions in flight, one per tool

**Status:** built, tested live, **reverted**. Kept as a record of what was tried.
**Revises:** `docs/claude/14-multi-step-tool-runs.md` §"The slots", "Locked rules" — no
longer; docs 14 stands unchanged.

## Outcome — why this is history, not plan

Built in full and exercised against live Gemini. Known cost #1 below is what broke it, and
it broke harder than the write-up expects: three runs blocking in one turn send three
`input_required` envelopes, the vendor generates a response per envelope, and the user
hears **two questions spoken over each other**. Not a tuning problem — a consequence of
letting more than one run block at once.

Two repairs were attempted on top, both live-tested, both failed:

| repair | what happened |
|---|---|
| hold the second call open until the first is answered | the vendor re-issues the entire batch with fresh ids ~1s later; the re-issue evicts the queued runs, which re-block and hold more calls open. Runaway: 42 tool calls for ~9 requested, recovery only when a budget expired at 175s |
| answer the second call immediately with a `deferred` status, model re-calls it later | correct on the wire and in the walkthrough, but it makes the model the only thing that can re-raise a dropped question — the framework keeps no state for it |

Reverted to the original invariant at the author's direction: **one live run per agent**.
The whole apparatus this spec describes — `RunSlots` keyed by tool name, eviction by age,
the cap, parallel resumption — is gone.

What survives from the exercise is in `docs/claude/14`: call-terminality vs
run-terminality, the resolution ladder, and the finding that a vendor call may never be
left open.

## The assumption being broken

Docs 14 holds **one run per agent**: an agent can only be doing one thing at a time, so
any new tool call displaces whatever that agent was doing. That bought correlation for
free — the connection names the agent, the agent has at most one blocked run, so a
submitted value has exactly one candidate and the model tracks nothing.

It also means a user who is asked "may I take a photo?" and answers by asking for
something else has silently lost the photo. Two consents cannot coexist.

We now allow **any number of live runs per agent**, each on its own budget. A run that is
not resolved inside its budget is discarded; one that is resumes and finishes.

## The new invariant

> **At most one live run per `(agent_id, tool_name)`.**

This is what makes concurrency addressable without giving the model anything to remember.
`tool_name` is known at `start` — unlike `key`, which does not exist until the handler
reaches `ctx.require`. So uniqueness is enforced at the one point where enforcement is
cheap, and every later stage can assume it.

## Addressing

`for_tool` *is* the address. Direct lookup, no scan:

```
RunSlots:  agent_id -> {tool_name: ToolRun}
```

`key` is no longer part of the address. It stays as a **staleness check**: a run with more
than one `ctx.require` may be blocked on key B when a late answer to key A arrives, and
that answer must be rejected rather than misapplied.

Resolution ladder, most specific last:

| condition | outcome | sent to model |
|---|---|---|
| agent has nothing blocked | `NO_BLOCKED_RUN` | `skipped("no input was expected")` |
| nothing blocked named `for_tool` | `FOR_TOOL_MISMATCH` | `skipped("that input was not expected")` |
| found, `pending.key != key` | `KEY_MISMATCH` | `skipped("that input was not expected")` |
| found, value fails `pending.schema` | `TYPE_MISMATCH` | `invalid_args`, retriable |
| — | `ACCEPTED` | the run's eventual result |

Every rejection leaves the run blocked and still answerable. Unchanged from docs 14.

## Eviction — two triggers, one sentence

*Newer wins, oldest goes.*

| trigger | victim | what the vendor sees |
|---|---|---|
| `start` of a tool that already has a live run for this agent | that run, whatever its state | executing → its carrier closed with `skipped`; blocked → no carrier open, silent |
| `start` while the agent is at `max_per_agent` (default 4) | the oldest run by `started_at` | same |

Insertion order in the per-agent dict *is* age order (starts are monotonic, deletion does
not reorder), so picking the oldest is O(1).

The same-name rule is the one that matters in practice: *"call mom"* → blocked on a
number → *"no, call dad"* must kill the stale question, not queue behind it. The cap is
backpressure against a looping model, and rarely fires — distinct tool count is already a
ceiling.

## No serialization

An answered run resumes immediately on its own `asyncio.Task`, in parallel with every
other live run. Each result rides its own carrier call, so there is no wire-ordering
hazard and no lock. "Sequentially" describes the user's experience — they answer one
question at a time — not a framework guarantee.

## No wire change

`input_required` already carries `for_tool` and `key`; `provide_input` already requires
both. Only the system instruction gains a line:

```
More than one tool may be waiting at the same time. Copy "for_tool" and "key"
from the result you are answering, not from the most recent one.
```

**Docs 14 open item O1 (remove `for_tool`) is dead.** `for_tool` is now the address.

## API changes — `snail/registry/run.py`

```python
RunSlots(max_per_agent=4)

start(agent_id, tool_name, *, carrier_call_id, now)  -> (run, evicted | None)   # trigger changed
find(agent_id, *, for_tool, key)                     -> (SubmitOutcome, run | None)   # new
submit(agent_id, *, for_tool, key, value)            -> (SubmitOutcome, run | None)   # = find + validate + resume
blocked(agent_id)                                    -> tuple[ToolRun, ...]     # was: ToolRun | None
get(agent_id, tool_name)                             -> ToolRun | None          # was: get(agent_id)
runs(agent_id)                                       -> tuple[ToolRun, ...]     # new
cancel_agent(agent_id)                               -> list[ToolRun]           # was: ToolRun | None
block / finish / cancel / sweep_expired / agents / __len__     # unchanged semantics
```

`find` exists because the session must know the target **before** it sets
`carrier_call_id` — the parked handler resumes the instant the future resolves, so the
carrier has to be in place first. Order becomes `find → extract_value → set carrier →
submit`.

## Session changes — `snail/session/session.py`

- `_start_tool` — unchanged shape; `_discard(evicted)` now fires on same-name or cap, not
  on every call.
- `_deliver_input` — resolve the address first, then extract the value against **that
  run's** `expects` (today it reads the sole blocked run's).
- `barge_in` — spare *every* blocked run, not one per agent.
- `sweep_runs` — unchanged. Already per-run, already pumped every 50 ms by the bridge.

## Frontend — `examples/frontend`

`metrics.pending` becomes a list keyed by `run_id`; `PendingPanel` renders one countdown
row per waiting run. `lastWait` unchanged.

## Example — `examples/confirmation`

Demonstrates what the change is for: `record_meeting` and `make_call` blocked at the same
time, answered out of order, both completing. Plus the same-name displacement path.

## Known costs

1. **Budget clocks start at block time, not ask time.** Three runs blocking in one turn
   send three `input_required` envelopes at once; the model may voice one and drop two,
   while all three clocks run. We cannot observe the model speaking, so no other start
   point is available. Mitigation is per-tool: generous `budget_s` on tools likely to
   co-occur.
2. **A second call to a live tool destroys the first**, including a question the user was
   about to answer. Deliberate — the alternative (reject the new call) drops the model's
   freshest intent in exactly the case the rule exists for.
3. **Skipped results still enter context.** Unchanged from docs 14: `TOOL_RESULT` maps to a
   real conversation item, so silence controls speech, not history.
