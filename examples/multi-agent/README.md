# Multi-agent example

Two Gemini agents in one snail session; one is active, the other listens. The
shared playground frontend (`../frontend`) drives it.

## Run

1. Start your FastAPI backend for this example (built separately) on
   `ws://localhost:8000/ws`, loading agents `gemini-a`, `gemini-b`.
2. Start the frontend:

       cd ../frontend && npm install && npm run dev

3. Open Chrome at:

       http://localhost:5173/?title=Multi-Agent&ws=ws://localhost:8000/ws&agents=gemini-a,gemini-b

   (Params match `config.json`.)

Speak, watch the timeline, use "hand off" to force-switch the active agent.

## Standalone (no backend)

Use the mock backend to eyeball the UI: see `../frontend/mock-backend/README.md`.
