# Snail playground frontend (shared)

A React+Vite browser playground reused across snail examples. Streams mic audio
(Opus) to a snail backend over one WebSocket, plays agent audio back, and renders
every neutral event on a live timeline.

## Run

    npm install
    npm run dev        # http://localhost:5173
    npm test           # unit tests (protocol, events, jitter)

## Parameterize per example

Pass via URL query (highest priority), Vite env, or defaults:

- `ws` / `VITE_WS_URL` — backend WebSocket URL
- `agents` / `VITE_AGENTS` — comma-separated agent ids
- `title` / `VITE_TITLE` — page title

Example: `http://localhost:5173/?ws=ws://localhost:8000/ws&agents=a,b&title=Demo`

## Wire contract

Binary WS frame = one raw Opus packet (uplink 48 kHz mono, downlink 24 kHz mono).
Text WS frame = JSON control (client→server) / event (server→client). See
`docs/superpowers/specs/2026-07-12-multi-agent-frontend-design.md`.

## Chrome only

Uses WebCodecs Opus (`AudioEncoder`/`AudioDecoder`).
