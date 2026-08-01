# Snail playground frontend (shared)

A React+Vite browser playground reused across snail examples. Streams mic audio
(Opus) to a snail backend over one WebSocket, plays agent audio back, and renders
every event the runtime emits — plus the timings that matter.

## Run

    npm install
    npm run dev        # http://localhost:5173
    npm test           # unit tests (protocol, events, metrics, jitter)

No backend and no API key? Run the scripted mock, which replays a full session
including a consent round-trip:

    python mock-backend/server.py

## What it shows

**Timeline** (centre) — every event in arrival order, stamped with elapsed session
time and filterable by kind (transcript / tool / turn / timing / system):

- transcripts, partials merged in place so a streaming reply is one row
- `tool_call` with its **arguments**, expandable as formatted JSON
- `tool_result` with its status, colour-coded (`success`, `input_required`, `error`, …)
- `tool_run` — the run-level state machine (docs 14): `started → blocked → submit →
  finished`, carrying the key it is waiting on and the submit outcome
- `speech_start` / `speech_end` — the server VAD's boundaries; `speech_end` is where
  the TTFB clock starts
- setup stages, handoffs, barge-ins, `go_away`, errors

Every row that carries more than it displays has a `raw` fold with the untouched
event JSON. Nothing is dropped, only folded.

**Connection setup** (right) — how long the session took to come up, split by stage
and scaled so the dominant term is visible at a glance:

| stage | measured by | what a slow value means |
|---|---|---|
| `websocket_open` | client | the network to your own backend |
| `mic_capture` | client | the browser's permission prompt / worklet + encoder start |
| `vendor_connect` (per agent) | server | the model provider's connect + setup handshake; a warm pool standby returns in ~0 ms |
| `start → live` | client | the whole thing, button press to live session |
| `backend setup` | server | first acquire → all agents ready |

**Turns · TTFB** (right) — one row per turn: agent, end-of-speech→first-audio-byte,
total turn duration, and the tools that ran in it. Plus last / p50 / min / max across
the session. TTFB is the server's number, measured at the bridge that decides
end-of-speech, so it excludes the listener's playout buffer and stays comparable
across runs.

**Agents** (right) — who holds the token, with manual handoff.

**Event counts** (right) — a tally per event type, and any errors. The cheapest way to
notice a stream that has gone wrong: no `agent_transcript` means the model is not
speaking; a climbing `interrupted` means the VAD is tripping on the agent's own echo.

## Parameterize per example

Pass via URL query (highest priority), Vite env, or defaults:

- `ws` / `VITE_WS_URL` — backend WebSocket URL
- `agents` / `VITE_AGENTS` — comma-separated agent ids
- `title` / `VITE_TITLE` — page title

Example: `http://localhost:5173/?ws=ws://localhost:8000/ws&agents=a,b&title=Demo`

## Wire contract

Binary WS frame = one raw Opus packet (uplink 48 kHz mono, downlink 24 kHz mono).
Text WS frame = JSON control (client→server) / event (server→client). Event types live
in `src/protocol.js`; the backend side is `examples/multi-agent/backend/events.py`.

Adding an event type takes two edits: a builder on the backend, and a line in
`KIND_OF` in `src/protocol.js` so it is classified and filterable. Give it a `case` in
`src/ui/EventRow.jsx` for a readable summary; without one it still renders, as its type
plus the raw JSON fold.

## Layout

| file | what it is |
|---|---|
| `src/protocol.js` | wire contract: event types, control builders, kind classification |
| `src/events.js` | pure reducer for the timeline list (partial-transcript merging) |
| `src/metrics.js` | pure reducer for the numbers: setup stages, turns, TTFB, counts |
| `src/useSession.js` | the WebSocket, the audio, and the client-side timings |
| `src/ui/*` | presentation only — every component takes props and renders |

Both reducers are pure and free of React, which is why the interesting behaviour is
unit-tested against scripted event streams rather than the DOM.

## Chrome only

Uses WebCodecs Opus (`AudioEncoder`/`AudioDecoder`). A denied or missing microphone is
not fatal — the session still connects and typed turns still drive the whole flow.
