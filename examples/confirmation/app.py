"""Live backend for the confirmation example — one agent, three tools, real audio.

Everything this example decides is here or beside it: its own tools (``tools.py``), its
own instruction and spec (``agent.py``), its own adapter and therefore its own
endpointing policy (``adapter.py`` — Gemini's VAD, not the bridge's energy VAD).

What it borrows is ``MultiAgentBridge``, purely as transport: the audio plane, the
client wire contract and the observability instrumentation. That is 500 lines of pump
with no host/echo opinions left in it — everything example-specific reaches it through
keyword arguments. One agent means no handoffs, so the routing chain is just the
programmatic hook the UI's manual button needs.

Run, from the repo root::

    GOOGLE_CLOUD_PROJECT=... GOOGLE_CLOUD_LOCATION=global \\
      PYTHONPATH=examples:examples/multi-agent \\
      python -m confirmation.app

(Vertex + ADC by default; ``SNAIL_GEMINI_BACKEND=dev`` with ``GEMINI_API_KEY`` for the
Developer API — note the model names differ, see ``agent.py``.)

Then point the playground at it:

    http://localhost:5173/?agents=assistant&title=Confirmation

Say "what does this sign say?" and it will ask for camera consent before answering;
say "record this meeting" and it will ask before recording. "What time is it?" runs
straight through. See ``README.md`` for what to watch on the timeline.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI, WebSocket

from snail.connections import ConnectionPool, GeminiConnector
from snail.router import ProgrammaticPolicy, default_chain
from snail.vendor import Backend

from backend.bridge import MultiAgentBridge  # examples/multi-agent — transport only

from .adapter import GeminiVadAdapter
from .agent import AGENT_ID, BACKEND, MODEL, build_agent_spec
from .tools import build_registry

log = logging.getLogger("confirmation")

POOL = "main"
SPECS = {AGENT_ID: build_agent_spec()}
POOL_KEY = {AGENT_ID: POOL}

#: Gemini owns endpointing here (not the bridge's energy VAD): the client streams
#: continuously and the model ends the turn after this much trailing silence. 800ms is
#: the API default; this example runs tighter. ``SNAIL_GEMINI_SILENCE_MS`` overrides.
SILENCE_MS = int(os.environ.get("SNAIL_GEMINI_SILENCE_MS", "300"))


def _tools_for(agent_id: str):
    """Same catalog the headless demo uses — the tools are the example, not the transport."""
    return build_registry()


def _single_agent_policy():
    """No tool-driven handoffs with one agent; keep the manual hook the UI expects."""
    programmatic = ProgrammaticPolicy()
    return default_chain(programmatic=programmatic), programmatic


def _client():
    if BACKEND is Backend.GEMINI_VERTEX:
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds:  # google-auth does not expand '~'
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.expanduser(creds)
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError(
                "Vertex backend: set GOOGLE_CLOUD_PROJECT and authenticate with ADC, "
                "or run with SNAIL_GEMINI_BACKEND=dev and GEMINI_API_KEY."
            )
        return GeminiVadAdapter.build_client(
            Backend.GEMINI_VERTEX,
            project=project,
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Dev backend: set GEMINI_API_KEY.")
    return GeminiVadAdapter.build_client(Backend.GEMINI_DEV, api_key=key)


def create_app() -> FastAPI:
    app = FastAPI()
    pools = {
        POOL: ConnectionPool(
            connector=GeminiConnector(
                client=_client(),
                # Gemini's own VAD, not the bridge's: no activity markers, continuous
                # audio, turn ends after SILENCE_MS of trailing silence.
                adapter=GeminiVadAdapter(
                    backend=BACKEND, model=MODEL, silence_duration_ms=SILENCE_MS
                ),
            ),
            max_warm=2,
        )
    }
    app.state.pools = pools
    log.info(
        "confirmation backend: agent=%s model=%s backend=%s gemini_vad silence=%dms",
        AGENT_ID, MODEL, BACKEND.value, SILENCE_MS,
    )

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await MultiAgentBridge(
            socket=socket,
            pools=pools,
            agent_ids=[AGENT_ID],
            specs=SPECS,
            pool_key=POOL_KEY,
            reanchor={},
            tools_for=_tools_for,
            policy=_single_agent_policy(),
        ).run()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for pool in pools.values():
            await pool.aclose()

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("multiagent").setLevel(logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
