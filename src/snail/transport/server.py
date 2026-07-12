"""FastAPI server — exposes the multi-agents over WebSockets (see docs 09§E).

This is the utility surface: hand it a :class:`~snail.connections.Connector` and a
``resolve_spec`` (path-param → :class:`~snail.connections.AgentSpec`) and get a FastAPI
app whose lifecycle owns the :class:`~snail.connections.ConnectionPool`:

* **server up → pool up.** The pool is created with the app; ``prewarm`` specs are
  opened on startup so the first client skips the handshake.
* **server down → pool down.** The lifespan shutdown calls ``pool.aclose()``, closing
  every live vendor connection.

Each WebSocket connection is one user-session: acquire a connection for the resolved
spec, run a :class:`ClientBridge` (default = agent output → client), and
``pool.release`` the socket when the client leaves (realtime sessions are
conversation-bound — no reuse, docs 02).

Run it with uvicorn: ``uvicorn.run(app, ...)`` — the transport picks *how* to serve;
Snail only defines the wiring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from snail.connections import AgentConnection, AgentSpec, ConnectionPool, Connector

from .bridge import ClientBridge, OnMessage

ResolveSpec = Callable[[str], AgentSpec]
#: build per-connection orchestration; returns the on_message forward (or None).
SessionFactory = Callable[[AgentConnection], Awaitable[OnMessage | None] | OnMessage | None]


def create_app(
    *,
    connector: Connector,
    resolve_spec: ResolveSpec,
    max_warm: int = 8,
    input_sample_rate: int = 16000,
    prewarm: Sequence[AgentSpec] = (),
    session_factory: SessionFactory | None = None,
    path: str = "/v1/agents/{agent}",
) -> FastAPI:
    """Build a FastAPI app that serves the agents and owns the connection pool."""
    pool = ConnectionPool(connector=connector, max_warm=max_warm)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for spec in prewarm:
            await pool.prewarm(spec)
        try:
            yield
        finally:
            await pool.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.pool = pool

    @app.websocket(path)
    async def agent_endpoint(websocket: WebSocket, agent: str) -> None:
        try:
            spec = resolve_spec(agent)
        except (KeyError, ValueError):
            await websocket.close(code=4404)  # unknown agent
            return
        conn = await pool.acquire(spec)
        conn.activate()
        on_message = None
        if session_factory is not None:
            built = session_factory(conn)
            on_message = await built if hasattr(built, "__await__") else built
        bridge = ClientBridge(
            socket=websocket,
            connection=conn,
            input_sample_rate=input_sample_rate,
            on_message=on_message,
        )
        try:
            await bridge.run()
        finally:
            await pool.release(conn)

    return app
