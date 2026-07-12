# Context Model

## Decision: append-only event log + snapshot projections (Option A)

The `ContextManager` holds the **entire session context** (the conversation plus
external/prior context). It is modeled as an **append-only event log** — an
ordered sequence of events. It is the **single source of truth**.

Agents receive **snapshot projections** of the log, taken at **turn/handoff
boundaries**.

### Why append-only + snapshots (vs live mutable context)

- **No locks, no torn reads.** Two vendor streams (e.g. Gemini + OpenAI) run
  concurrently; a shared mutable context would race on write-back. Append-only +
  point-in-time snapshots eliminate the race.
- **Cheap to share.** Immutable buffers, `msgspec` structs — copy-on-nothing.
- **One structure, many jobs.** The log is also the replay/persist/debug/trace
  format, and the thing projections are computed from.
- **Voice-appropriate.** Turns are the natural transaction boundary; agents swap
  at turn edges anyway, so point-in-time projections are sufficient.

Trade-off accepted: a projection is point-in-time — an agent doesn't see events
that land *during* its turn until the next boundary. Fine for voice turn-taking.

## The Event (canonical, vendor-neutral)

```
Event: id, ts, type, agent_id, content, meta
   type ∈ {user_speech, agent_speech, tool_call, tool_result,
           external_context, handoff}
```

- `msgspec.Struct`, slotted, flat. Append-only.
- This is also the log/replay/persist format — one structure, many jobs.

## Context is transcripts, not audio

Realtime APIs are seeded with **text conversation items (transcripts)**, not
replayed audio. Audio stays in the ring buffers and **never** enters the
log-as-context. This keeps projections cheap and vendor-portable.

> **TODO(log-truncation-fidelity / log-completeness — see 09§E):** two open
> questions this creates. (1) **Barge-in fidelity:** the model generated a full
> sentence, the user heard 40% — which does the log record? Projections + promoted
> listeners inherit the answer (OpenAI has `item.truncate`, Gemini doesn't). (2)
> **Transcript timing + compaction:** user-speech transcripts are vendor-supplied
> (async, lossy, sometimes late) → log-replay may replay a hole; and there is no
> compaction/summarization policy, so a long session eventually overruns the vendor
> context window (`last_n` is truncation, not a policy).

## Projection API (two modes)

A projection is a **filter/transform over the log** producing a vendor-neutral
list of `Item`s. Both modes stop at canonical `Item[]` — **never** raw vendor
dicts. The `VendorAdapter` does the vendor-specific serialization. This hard
boundary is what preserves vendor-neutrality.

### Mode 1 — declarative projection spec (default, ~90%)

```python
billing_ctx = Projection(
    include={"user_speech", "agent_speech", "tool_result"},
    agents=["main", "user"],       # what this agent may see
    last_n=20,                     # recency window
    instructions="You are a billing specialist. Be concise.",
    extra=[account_summary_doc],   # external context injection
)
```

Safe, declarative, cacheable, vendor-neutral guaranteed.

### Mode 2 — imperative builder (escape hatch, ~10%)

```python
def build_billing_context(log: EventLog) -> list[Item]:
    items = []
    for e in log.filter(types=("user_speech", "agent_speech")):
        items.append(Item(role=role_of(e), text=e.content))
    items.append(Item(role="system", text="You are billing..."))
    return items
```

Total control (custom redaction, summarization, reordering). **Must still return
canonical `Item[]`**, not vendor payloads — the adapter serializes. The moment a
user returns `{"type": "conversation.item.create", ...}`, vendor-neutrality dies.

```
Mode 1: user writes filters   ┐
Mode 2: user writes builder    ├─▶ Item[] ─▶ VendorAdapter ─▶ vendor wire
        (both stop here) ──────┘        ↑ hard boundary, framework-owned
```

## How other frameworks do vendor-neutrality (reference)

Both use **canonical IR + one adapter per vendor**:

- **Pipecat** — linear pipeline of `Frame` processors; vendor services convert
  vendor I/O ↔ canonical frames (`AudioRawFrame`, `TranscriptionFrame`,
  `LLMMessagesFrame`). Leans on OpenAI's message format as the lingua franca.
- **LiveKit Agents** — capability interfaces (`llm.LLM`, `stt.STT`, `tts.TTS`,
  `realtime.RealtimeModel`) + canonical `ChatContext`; WebRTC transport decoupled
  from AI plugins.

**Snail's difference:** the canonical IR is an **append-only log you project
from** (not a live pipeline or per-capability plugin). This is what makes
multi-vendor-single-face and cheap snapshots natural — Snail is built for two
vendors in one face; they were not.
