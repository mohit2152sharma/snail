"""FastAPI app for the host+echo multi-agent example.

One WebSocket route (``/ws``) speaking the frontend contract. The app owns a
``ConnectionPool`` over the Gemini Dev API; each connected client gets a
``MultiAgentBridge`` that acquires the host + echo connections, runs the multi-agent
runtime, and releases them on disconnect.

Run (needs a real key):

    GEMINI_API_KEY=... PYTHONPATH=examples/multi-agent python -m backend.app
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, WebSocket

from snail.connections import ConnectionPool, GeminiConnector
from snail.vendor import Backend, GeminiAdapter

from .bridge import MultiAgentBridge


def build_pool() -> ConnectionPool:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("set GEMINI_API_KEY to run the multi-agent example")
    adapter = GeminiAdapter()  # host + echo share model + backend → one adapter
    client = GeminiAdapter.build_client(Backend.GEMINI_DEV, api_key=key)
    connector = GeminiConnector(client=client, adapter=adapter)
    return ConnectionPool(connector=connector, max_warm=4)


def create_app() -> FastAPI:
    app = FastAPI()
    pool = build_pool()
    app.state.pool = pool

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        bridge = MultiAgentBridge(socket=socket, pool=pool)
        await bridge.run()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await pool.aclose()

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
