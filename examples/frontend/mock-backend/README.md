# Mock backend

Throwaway server that speaks the playground wire contract without a real vendor.

    python server.py   # ws://localhost:8000/ws

Emits scripted JSON events on `start`, echoes `text`, flips active agent on
`handoff`, emits `interrupted` on `barge_in`. Uplink audio is ignored. Use it to
run and eyeball the frontend before the real FastAPI backend is ready.
