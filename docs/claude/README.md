# Snail — Design Docs (Claude discussion)

This folder captures the design discussion for **Snail**, a framework for
multi-voice-agents. These docs are a snapshot of decisions reached in a design
discussion — they are **design intent, not shipped code**.

## What Snail is

A framework for **multi-voice-agents**: multiple vendor voice agents (Gemini
Live, OpenAI Realtime) presented to the end user as **one face**. Vendor-neutral,
so you can mix/switch vendors without rewriting your app. Built for performance
(memory, CPU, latency) using pure Python + existing C-backed libraries.

## Reading order

| File | Contents |
|------|----------|
| [00-vision-and-goals.md](00-vision-and-goals.md) | Product definition, goals, constraints, performance framing |
| [01-context-model.md](01-context-model.md) | Append-only event log, snapshot projections, projection API |
| [02-connections-and-pool.md](02-connections-and-pool.md) | AgentSpec vs AgentConnection, pool, SetupParam/JoinContext, recycle/lifecycle |
| [03-tool-layer.md](03-tool-layer.md) | Tool, ToolRegistry, schema policy, ToolCall/ToolResult envelope, speech directives |
| [04-tool-call-registry.md](04-tool-call-registry.md) | ToolCallRegistry (in-flight tracker), cancel/timeout, lifecycle FSM |
| [05-router.md](05-router.md) | Router: planes, OutputGate, RoutingPolicy, seam, promotion/demotion (locked) |
| [06-concurrency.md](06-concurrency.md) | GIL, asyncio + uvloop + anyio, per-session loop model |
| [07-vendor-capability-matrix.md](07-vendor-capability-matrix.md) | Gemini 2.5 / OpenAI specifics, verified findings, capability cells |
| [08-architecture-diagrams.md](08-architecture-diagrams.md) | Low-level diagrams (components, data flow, FSMs) |
| [09-pending-items.md](09-pending-items.md) | Deferred features, open decisions, validation TODOs |
| [10-discussion-log.md](10-discussion-log.md) | Narrative timeline of the discussion |
| [11-audio.md](11-audio.md) | Audio plane: AudioFrame, 48k interior, opus/RNNoise, buffers/gates, pluggable sinks |
| [12-observability.md](12-observability.md) | Observer layer: metrics + events, tap-anywhere, zero-cost emission |
| [13-mermaid-diagrams.md](13-mermaid-diagrams.md) | Mermaid low-level diagrams + control flow (master view, planes, FSMs, sequences) |
| [implementation/first-phase-gemini.md](implementation/first-phase-gemini.md) | Phased build plan — **Gemini-only v1**, OpenAI deferred; maps 09§E TODOs to phases |

## Cross-cutting pattern (remember this)

**Adapters declare capabilities; the framework branches on them.** Capability is
keyed per **(vendor, model, backend)**, not per vendor. Examples: SetupParam vs
JoinContext binding, native-vs-emulated `deferred` tool calls, native-vs-manual
session resumption. The neutral surface stays the same; the adapter absorbs the
difference.
