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

from .agents import BACKEND, MODEL
from .bridge import MultiAgentBridge


def build_pool() -> ConnectionPool:
    # host + echo share model + backend → one adapter/connector.
    adapter = GeminiAdapter(backend=BACKEND, model=MODEL)
    if BACKEND is Backend.GEMINI_VERTEX:
        # ADC auth (gcloud auth application-default login) + project/location.
        # google-auth does NOT expand '~' in this path → expand it ourselves.
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.expanduser(creds)
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        if not project:
            raise RuntimeError(
                "Vertex backend: set GOOGLE_CLOUD_PROJECT (and optionally "
                "GOOGLE_CLOUD_LOCATION), and authenticate with ADC "
                "(`gcloud auth application-default login`)."
            )
        client = GeminiAdapter.build_client(
            Backend.GEMINI_VERTEX, project=project, location=location
        )
    else:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Dev backend: set GEMINI_API_KEY.")
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
    import logging

    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("multiagent").setLevel(logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
