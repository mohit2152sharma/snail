# Implementation Status

Tracks what's actually built against [first-phase-gemini.md](first-phase-gemini.md).
Legend: ✅ done + tested · 🟡 partial · ⬜ not started · 🔑 needs live Gemini key.

_Last updated: 2026-07-12._

## Built (key-free, unit-tested — 206 tests green)

| Module | What | Docs | TODOs resolved |
|---|---|---|---|
| ✅ `snail.context` | `Event`/`Item` frozen schema, `EventLog` (append-only, monotonic seq), `Projection` Mode-1 + Mode-2 builder | 01 | froze canonical Event/Item schema (09§B) |
| ✅ `snail.audio` | `AudioFrame` (int16 view), `FramePool` (**refcount ownership**, bounded/recycling, `try_acquire`/`recommend_capacity`, detach-release), `FanoutBus` + per-subscriber rings (GATE 1, raw/clean source, per-ring `OverflowPolicy` DROP_OLDEST default / DROP_NEWEST, `reclaim_oldest` on exhaustion) | 11 | `framepool-ownership` (09§E) |
| ✅ `snail.vendor` | `VendorAdapter` Protocol, `VendorCapabilities` (per vendor/model/backend), `SetupParam`/`JoinContext`/`ResponseModality`/`InputSource`/`ToolSpec`, `ParsedEvent` set, **`MockVendorAdapter`**, **`GeminiAdapter`** (Dev API + Vertex, `google-genai` translation) | 02/07 | `doc-test-strategy` mock (09§E) |
| ✅ `snail.tools` | common-denominator schema validator, `Tool` (stateless, output_schema required), `ToolRegistry`, `ToolResult` envelope + status taxonomy + speech-directive cascade, `execute` (sanitized) | 03 | — |
| ✅ `snail.registry` | `ToolCallRegistry` FSM, single-resolution invariant, group/connection indexes, cancel/`sweep_*`/`sweep_timeouts`, backpressure cap, loop-agnostic `Promise` | 04 | — |
| ✅ `snail.router` | `OutputGate` (GATE 2 token, single-writer ring, flush/transfer), `RoutingPolicy` + built-ins (ControlTool/Programmatic/Rule/Chain, `default_chain`=Programmatic-first), predicate DSL (`F`/`{field,op,value}` + callable, serializable), `Router` mechanism (promote/demote, seam CUT_NOW/AT_TURN_END/AT_IDLE, health-gate, barge-in, needs_flip hook) | 05 | `chain-default-order` (09§E) |
| ✅ `snail.session` | loop-bound orchestrator: `ParsedEvent`→log/registry/router; **async tool execution** (concurrent tasks, `wait_for` timeout, cooperative cancel), tool-result→vendor `send` + routing signal, barge-in cancels tasks + sweeps, turn/idle boundaries, `drain_tools`/`aclose` | 05/06 | wires the loop the lower layers deferred |
| ✅ `snail.connections` | `AgentSpec` (swap-stable identity + `pool_key`), `AgentConnection` (COLD→CONNECTING→WARM→ACTIVE→CLOSED, neutral send seams via adapter, inbound `run` loop, `ConnectionMeta` deadline/resumption, atomic `adopt` swap), `LiveTransport` protocol (Gemini `AsyncSession` fits; fake for tests), `Connector`/`GeminiConnector`, `ConnectionPool` (per-`AgentSpec` prewarm/acquire/park/recycle/evict + admission cap) | 02 | AgentSpec≠AgentConnection split; disposable-connection recycle |

Design note baked into code: `ResponseModality` is **per-agent (TEXT or AUDIO)** — the
corrected listener model. Registry & pool are **lock-free** (one loop per worker, 06).

## Deliberate deferrals in the built code

- **Registry is loop-agnostic:** `Promise` + explicit `sweep_timeouts(now)` instead of
  `asyncio.Future` + self-arming timers. Real await/timer/handler-cancel wiring lands in
  the **session layer** (owns the loop, 06). FSM/invariant logic stays pure + testable.
- **`execute` is sync-handler only.** Async handlers + per-tool timeout budget +
  co-operative cancel = session-layer orchestration (04/06).
- **`deferred` tool status** enum exists; async late-resolve path not wired (09§A).
- **Connection recycle/keepalive *scheduler* is loop-deferred.** The pool ships the pure
  mechanism — `due_for_recycle(margin)` (query) + `recycle(conn)` (action, native resume +
  atomic swap) — but the timer that fires them at `deadline − margin` and periodic
  keepalive is a session-loop concern (same pattern as the registry's `sweep_timeouts`).

Requirement updates (2026-07-12) baked in: **per-consumer input source** (RAW/CLEAN,
`SetupParam.input_source` + `VendorCapabilities.self_denoise`) and **lazy, shared
resample** (docs 11/07). Fan-out bus already carries `source` + `target_rate` per sub.

## Next (still key-free)

- 🟡 `snail.audio` pipeline — ✅ **cleaner** (`RNNoiseCleaner` + 480-`Rechunker`,
  `DenoiseBackend` injected), ✅ **resample** (`LazyResampler`: no-op at equal rate, one
  stateful converter memoized per distinct `(from,to)`; ✅ **real `SoxrResampleBackend`**
  (libsoxr, guarded import, stateful `ResampleStream` per rate-pair, ~341-sample fixed
  latency @48k→16k)),
  ✅ **codec** (`AudioCodec` seam; `PcmCodec` PCM16LE + ✅ real `OpusCodec` (libopus, guarded,
  per-stream stateful enc/dec, 480=10ms frame matches interior, duration-agnostic decode) —
  **client leg only; Gemini always gets PCM16LE**), ✅ **jitter buffer** (`JitterBuffer`: prebuffer→PLAYING→underrun-rearm, boundary-
  stitching drain, `flush` for cut; feeds `OutputGate`). All dep-free (native bindings
  guarded/injected). ✅ **pipeline runner** (`AudioPipeline`): ingress = decode→resample-to-48k
  →480-rechunk→fan-out RAW (+CLEAN iff a clean subscriber) with drop-oldest recovery on
  exhaustion, `drain` = per-subscriber ring-pop→lazy resample-to-vendor-rate→bytes+release;
  egress = decode/upsample→jitter→OutputGate token→codec→client bytes, `cut` flushes both.
  **VAD deferred.**
- 🟡 `snail.transport` — ✅ **wire protocol** (binary=media PCM16LE, text=JSON `Control`:
  READY/FLUSH/TRANSCRIPT/BYE/PLAYOUT/END), `ClientSocket` seam (FastAPI `WebSocket` fits;
  fake for tests), `ClientBridge` (default = agent output→client; mic→`send_realtime`;
  `Interrupted`→client FLUSH; `PlayoutClock` buffered-ahead + cut-reset), `create_app`
  (FastAPI: **server up→pool up (prewarm), server down→pool.aclose()**; one WS = one
  session; release-on-disconnect). ✅ **audio-plane wiring**: `ClientBridge` takes an
  optional `AudioPipeline` — ingress client→plane→drain→vendor, egress vendor→jitter→gate
  →codec→client, `Interrupted`→`pipeline.cut()`+client FLUSH; auto attach/grant-token on
  start, detach on teardown. `create_app(session_factory=, pipeline_factory=)` assembles
  the **end-to-end vertical slice** (transport ↔ pipeline ↔ connection, and ↔ session via
  `on_message`). Resolves 09§E `client-protocol`. ⬜ opus codec on the client leg (v0 = raw
  PCM16); auth / reconnect / client-leg backpressure.

## Blocked on a live Gemini key (Phase 0 spikes — verification, not build)

- 🔑 **S1 modality flip** (#1): text→audio reconnect latency vs audio-listener audio-out cost.
- 🔑 **S2 history injection**: setup instruction + user/model turns before first model turn.
- 🔑 **S3 resumption/recycle**: `GoAway` timing, `session_resumption` round-trip.
- 🔑 **S4 native async tools**: `NON_BLOCKING` on Dev API; confirm no-op on Vertex (#1739).
- 🔑 **S5 server-VAD barge-in**: `Interrupted` mapping done; measure latency live.
- ✅ **`snail.vendor.gemini`** `GeminiAdapter` **built + unit-tested** (translation: setup/items/tools/
  results/events, both backends). Only the **live wire** (connect + stream) needs a key/ADC —
  that's the connection layer + the spikes.

## How to run

```
uv run pytest            # 206 tests, <1s, no network/key
```
