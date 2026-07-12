# Multi-Agent Playground Frontend — Design

Date: 2026-07-12
Status: Approved (design)

## Goal

A browser playground under `examples/multi-agent/` to exercise a snail
multi-agent voice session end-to-end against a **real** vendor (Gemini Live).
The user speaks into the browser; audio streams to a backend that drives a snail
`Session`; every neutral event the agent(s) emit is rendered live in the browser.

Scope of **this** spec: the **frontend only** (React + Vite). The FastAPI/uvicorn
backend is built in parallel by the user and is out of scope here. This spec
**pins the wire contract** the two sides share, and ships a throwaway **mock
backend** so the frontend runs before the real backend lands.

## Non-goals

- No backend implementation (FastAPI/uvicorn) beyond the mock test harness.
- No new runtime deps added to the `snail` package's `pyproject.toml`.
- Not a benchmark/load harness. Not a pytest addition. This is an interactive
  example app.
- Cross-browser support: **Chrome-target only** (WebCodecs Opus).

## Directory layout

```
examples/
  multi-agent/
    README.md
    index.html
    vite.config.js
    package.json
    src/
      main.jsx
      App.jsx
      useSession.js          # WS lifecycle + control dispatch + event state
      audio/
        uplink.js            # mic -> AudioWorklet -> Opus encode -> WS binary
        downlink.js          # WS binary -> Opus decode -> jitter buffer -> play
        capture-worklet.js   # AudioWorklet processor (Float32 blocks)
      ui/
        ControlsBar.jsx
        Timeline.jsx
        AgentPanel.jsx
    mock-backend/
      server.py              # stdlib/websockets mock; echoes events + canned Opus tone
```

The frontend is a standalone Vite app (has its own `package.json`; node toolchain
lives inside `examples/multi-agent/`, not at repo root).

## Wire contract (frontend ↔ backend, single WebSocket)

Multiplexing: **binary frame = Opus audio; text frame = JSON control/event.**
The frontend branches on `event.data instanceof ArrayBuffer/Blob` vs string.

### Audio frames (binary)

- **Uplink (client→server):** mic audio, source 48 kHz mono, transmitted as raw
  Opus (`EncodedAudioChunk` bytes from WebCodecs `AudioEncoder({codec:'opus',
  sampleRate:48000, numberOfChannels:1})`).
- **Downlink (server→client):** agent audio, 24 kHz mono, raw Opus, decoded by
  WebCodecs `AudioDecoder({codec:'opus'})`.
- Each binary WS message carries exactly one Opus packet payload (no container,
  no extra framing header).

### Control messages (client→server, JSON text)

| type | fields | meaning |
|------|--------|---------|
| `start` | `agents: string[]` | open session, load these agent ids |
| `stop` | — | tear down session |
| `mute` | `on: bool` | stop/resume feeding mic to encoder |
| `barge_in` | — | manual interrupt of current agent turn |
| `handoff` | `agent_id: string` | force active-agent switch (router seam) |
| `text` | `text: string` | inject a user text turn (no mic) |

### Event messages (server→client, JSON text)

All events share `{ type, ts }` (`ts` = ms epoch). Type-specific fields:

| type | fields |
|------|--------|
| `user_transcript` | `text, is_final` |
| `agent_transcript` | `agent_id, text, is_final` |
| `tool_call` | `agent_id, tool_name, call_id, args` |
| `tool_result` | `agent_id, tool_name, call_id, status, content` |
| `turn_complete` | — |
| `interrupted` | — |
| `go_away` | `time_left_ms` |
| `active_agent_changed` | `agent_id` |
| `error` | `code, message` |

These mirror snail's neutral events (`UserTranscript`, `AgentTranscript`,
`ToolCallRequest`, `TurnComplete`, `Interrupted`, `GoAway`, plus router
active-agent transitions). The backend owns translation from snail internals to
this schema; the frontend only consumes it.

## Components

### `useSession` hook
Owns the WebSocket. Exposes: `status` (idle/connecting/live/closed), `events[]`,
`agents[]`, `activeAgentId`, and action fns (`start`, `stop`, `setMute`,
`bargeIn`, `handoff`, `sendText`). Wires uplink/downlink to the socket. On
`interrupted` or a `barge_in` action, flushes the downlink playback queue.
Transcript events with `is_final:false` update the last matching in-place row
rather than appending.

### Audio uplink (`audio/uplink.js`)
`getUserMedia({audio})` → `AudioContext({sampleRate:48000})` → `AudioWorkletNode`
(`capture-worklet.js`) emitting Float32 blocks → wrap as `AudioData` →
`AudioEncoder('opus')` → `output` callback sends `EncodedAudioChunk` bytes as WS
binary. Mute detaches the worklet→encoder path (no bytes sent).

### Audio downlink (`audio/downlink.js`)
WS binary → `AudioDecoder('opus')` → `AudioData` → push to a small jitter buffer,
scheduled onto a 24 kHz playback `AudioContext` via successively-timed
`AudioBufferSourceNode`s. Flush clears pending buffers immediately (barge-in).

### UI (bare minimum)
- **ControlsBar** — start/stop, mute toggle, barge-in button, text input+send.
- **Timeline** — append-only vertical list; one row per event (type badge,
  optional `agent_id`, text/payload). Auto-scroll to bottom. Minimal CSS.
- **AgentPanel** — lists loaded agents, highlights `activeAgentId`, a handoff
  button per non-active agent.

## Mock backend (`mock-backend/server.py`)
A throwaway `websockets`-based server (no FastAPI). On `start`, emits a scripted
sequence of JSON events (user_transcript → agent_transcript partial/final →
active_agent_changed → tool_call/tool_result) and streams a canned Opus tone as
downlink binary so playback is exercisable. Responds to `handoff`/`text`/`mute`
with plausible events. Purpose: run and eyeball the frontend before the real
backend exists. Not shipped as a product artifact.

## Error handling
- WebCodecs unsupported → surface a clear banner (Chrome required).
- WS drop → `status:closed`, disable controls, offer reconnect.
- Decoder/encoder errors → `error` row in timeline; do not crash the app.
- Mic permission denied → banner + disabled mic, text-turn path still works.

## Testing / verification
- Manual: `npm run dev` in `examples/multi-agent/`, run `mock-backend/server.py`,
  confirm mic capture, tone playback, live timeline, mute, barge-in, handoff,
  text turn.
- No automated frontend tests in this iteration (throwaway example scope).
