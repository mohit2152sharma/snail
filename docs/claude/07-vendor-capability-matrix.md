# Vendor Capability Matrix

Cross-cutting pattern: **adapters declare capabilities; the framework branches on
them.** Capability is keyed per **(vendor, model, backend)**, not per vendor.

## Scope

- Gemini: **`gemini-2.5-flash-live` only**, both **Developer API** and **Vertex AI**
  backends. Ignore 3.1 / 2.0.
- OpenAI: **Realtime API**.

## SetupParam vs JoinContext

| field | Gemini Live | OpenAI Realtime |
|---|---|---|
| model, voice | SetupParam | SetupParam |
| system_instruction | **SetupParam** (can't update mid-session) | SetupParam *(by our choice — could inject late, but symmetric)* |
| tools | **SetupParam** (can't add/modify mid-session) | SetupParam *(by our choice)* |
| history | JoinContext (`user`/`model` turns, **before first model turn**) | JoinContext (`conversation.item.create`) |
| per-client facts | JoinContext | JoinContext |

### Verified Gemini facts (from docs + SDK + Google forum)

- **No `role="system"` content turns.** Valid content roles = `user`, `model`,
  `tool`. System guidance = setup `system_instruction` only.
- **No mid-session config/instruction/tool update.** Official Google reply:
  *"You cannot update the configuration while the connection is open."* Config
  changes only via session resumption (pause/resume).
- Sources: ai.google.dev/api/live; Google AI dev forum thread on mid-session
  config; `googleapis/python-genai`.

## Audio input: self-denoise + rate (capability-driven, docs 11)

| capability | meaning | consequence |
|---|---|---|
| `self_denoise` | model does its own noise suppression | agent sets `input_source = RAW` → framework skips RNNoise for it. Some Gemini models qualify (verify per model). |
| `input_sample_rate` | vendor's expected input rate (Gemini 16k, OpenAI 24k) | consumer leg resamples **only if** ≠ 48k interior; **lazy + shared** per distinct rate (docs 11). |

Cleaning and resampling are **per-consumer**, not global: RNNoise runs only if some
agent wants `CLEAN`; a consumer at a vendor-native rate pays no resample. This is the
CPU lever — one self-denoising, native-rate agent pays neither clean nor resample.

## Outbound seams (neutral, multimodal) — converged on Gemini's shape

Gemini input is **multimodal** (audio/image/text) over two streaming shapes, so the
neutral layer converges on three seams instead of a narrow `send_audio`:

| neutral seam | carries | Gemini | OpenAI Realtime |
|---|---|---|---|
| **realtime** (`serialize_realtime(MediaChunk)`) | streaming audio / image / text; VAD-driven, unordered | `send_realtime_input(audio=/media=/text=)` | `input_audio_buffer.append` / image item |
| **realtime control** (`serialize_realtime_control`) | activity_start/end, audio_stream_end (manual VAD) | `send_realtime_input(activity_*/audio_stream_end)` | `input_audio_buffer.commit` etc. |
| **turns** (`serialize_turns(items, complete)`) | ordered content turns; `complete`→trigger response | `send_client_content(turns=, turn_complete=)` | `conversation.item.create` (+ `response.create`) |
| **tool_result** (`serialize_tool_result`) | function responses | `send_tool_response` | `function_call_output` |

`MediaChunk` (`audio`/`image`/`text_` factories) is the realtime unit; ordered turns
reuse `Item`. Turn boundaries for a **voice** session come from **inbound** VAD events
(`Interrupted`/`turn_complete`), not an outbound `turn_complete` we set. History
injection = `serialize_turns(history, complete=False)` at join (before first model turn).

## Async / non-blocking tool calls (`deferred`)

Verified against `google-genai` **v2.11.0** source + docs + issues.

| (vendor, model, backend) | native async tool calls? |
|---|---|
| Gemini 2.5 Flash Live + **Developer API** | ✅ native (`Behavior.NON_BLOCKING` + `FunctionResponseScheduling`) |
| Gemini 2.5 Flash Live + **Vertex AI** | ❌ emulate (not wired for Vertex — issue #1739) |
| Gemini 3.1 Flash Live | ❌ (async not supported) — out of scope anyway |
| OpenAI Realtime | ❌ emulate (no native non-blocking continue) |

### Gemini native API surface (verified, v2.11.0)

```python
from google.genai import types

# on the function declaration:
types.Behavior.NON_BLOCKING        # enum: UNSPECIFIED | BLOCKING | NON_BLOCKING
FunctionDeclaration(behavior=types.Behavior.NON_BLOCKING)
#   docstring: "currently only supported by the BidiGenerateContent method" (= Live API)

# on the function response:
types.FunctionResponseScheduling    # SILENT | WHEN_IDLE (default) | INTERRUPT
FunctionResponse(scheduling=..., will_continue=<bool>)
#   will_continue → generator tool streaming multiple responses.
#     "not supported in Vertex AI"
```

- Our neutral `schedule` field maps **1:1** to `FunctionResponseScheduling`
  (`interrupt|when_idle|silent`). Down-converts to native where available; drives
  emulation elsewhere.
- **NON_BLOCKING is Live-API-only** (BidiGenerateContent); does nothing in
  `generate_content`.
- **Likely cause of past errors** using this: wrong model (non-Live), Vertex
  backend (#1739), or old SDK version (`AttributeError` on `Behavior`).

## Session recycle / resumption

| capability | Gemini Live | OpenAI Realtime |
|---|---|---|
| deadline signal | `GoAway` (with `timeLeft`); duration limits | `session.created.expires_at` |
| native resumption | ✅ `session_resumption` handle → resume with context | ❌ none |
| recycle strategy | native resume (fast) OR log-replay | log-replay (open new socket + replay context) |

Log-replay works for **both** (context lives in our log); Gemini resumption is the
fast lane. See 02-connections-and-pool.md.
