"""FastAPI app for the host+echo multi-agent example.

One WebSocket route (``/ws``) speaking the frontend contract. The app owns a
``ConnectionPool`` over the Gemini Dev API; each connected client gets a
``MultiAgentBridge`` that acquires the host + echo connections, runs the multi-agent
runtime, and releases them on disconnect.

Run (needs a real key):

    GEMINI_API_KEY=... PYTHONPATH=examples/multi-agent python -m backend.app
"""

from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI, WebSocket

from snail.connections import ConnectionPool, GeminiConnector
from snail.vendor import Backend, GeminiAdapter

from .adapter import TranslateGeminiAdapter
from .agents import (
    BACKEND,
    ECHO_ID,
    HOST_ID,
    MODEL,
    TRANSLATE_ID,
    TRANSLATE_MODEL,
    TRANSLATE_TARGET,
)
from .bridge import MultiAgentBridge

log = logging.getLogger("multiagent")


def _main_client():
    if BACKEND is Backend.GEMINI_VERTEX:
        # ADC auth (gcloud auth application-default login) + project/location.
        # google-auth does NOT expand '~' in the creds path → expand it ourselves.
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.expanduser(creds)
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")  # 2.5-live = global
        if not project:
            raise RuntimeError(
                "Vertex backend: set GOOGLE_CLOUD_PROJECT and authenticate with ADC "
                "(`gcloud auth application-default login`)."
            )
        return GeminiAdapter.build_client(
            Backend.GEMINI_VERTEX, project=project, location=location
        )
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Dev backend: set GEMINI_API_KEY.")
    return GeminiAdapter.build_client(Backend.GEMINI_DEV, api_key=key)


def build_runtime() -> tuple[dict, list[str]]:
    """Build the pool-per-purpose map + the agent list to run this deployment.

    "main" pool serves host + echo. The "translate" pool (Gemini 3.5 Live Translate,
    Dev-API only) is added iff GEMINI_API_KEY is set; without it the example still runs
    host + echo.
    """
    pools: dict = {}
    main_adapter = GeminiAdapter(backend=BACKEND, model=MODEL)  # server-side VAD (floored)
    pools["main"] = ConnectionPool(
        connector=GeminiConnector(client=_main_client(), adapter=main_adapter),
        max_warm=4,
    )
    agent_ids = [HOST_ID, ECHO_ID]

    dev_key = os.environ.get("GEMINI_API_KEY")
    if dev_key:
        tr_adapter = TranslateGeminiAdapter(
            target_language_code=TRANSLATE_TARGET,
            backend=Backend.GEMINI_DEV,
            model=TRANSLATE_MODEL,
        )
        tr_client = TranslateGeminiAdapter.build_client(Backend.GEMINI_DEV, api_key=dev_key)
        pools["translate"] = ConnectionPool(
            connector=GeminiConnector(client=tr_client, adapter=tr_adapter), max_warm=1
        )
        agent_ids.append(TRANSLATE_ID)
    else:
        log.warning("GEMINI_API_KEY unset → translate agent disabled (host + echo only)")
    return pools, agent_ids


def create_app() -> FastAPI:
    app = FastAPI()
    pools, agent_ids = build_runtime()
    app.state.pools = pools
    log.info("agents this deployment: %s", agent_ids)

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        bridge = MultiAgentBridge(socket=socket, pools=pools, agent_ids=agent_ids)
        await bridge.run()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for pool in pools.values():
            await pool.aclose()

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("multiagent").setLevel(logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
