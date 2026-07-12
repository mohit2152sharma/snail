"""Connector — opens vendor sockets for the pool (see docs 02).

A :class:`Connector` is the only place that touches the network: given an
:class:`AgentSpec` it performs the handshake + config and returns a live
:class:`LiveTransport`. The pool owns *when* to open (pre-warm / lazy / recycle); the
connector owns *how*. Injecting the connector is what keeps the pool and connection
layers testable without a key — tests pass a fake connector.

:class:`GeminiConnector` is the real Gemini Live wire. ``client.aio.live.connect`` is an
async context manager, so the returned transport holds the CM and drives its
``__aexit__`` on ``close`` (:class:`_GeminiLiveTransport`).
"""

from __future__ import annotations

from typing import Any, Protocol

from snail.vendor import GeminiAdapter, VendorAdapter

from .connection import LiveTransport
from .spec import AgentSpec


class Connector(Protocol):
    """Opens a live transport for a spec (optionally resuming a prior session)."""

    @property
    def adapter(self) -> VendorAdapter: ...

    async def open(
        self, spec: AgentSpec, *, resumption_handle: str | None = None
    ) -> LiveTransport: ...


class _GeminiLiveTransport:
    """Adapts a ``google-genai`` live ``AsyncSession`` (held via its context manager) to
    :class:`LiveTransport`. The send methods delegate straight through; ``close`` exits
    the connect context manager that owns the socket.
    """

    def __init__(self, session: Any, cm: Any) -> None:
        self._session = session
        self._cm = cm

    def send_realtime_input(self, **kwargs: Any) -> Any:
        return self._session.send_realtime_input(**kwargs)

    def send_client_content(self, **kwargs: Any) -> Any:
        return self._session.send_client_content(**kwargs)

    def send_tool_response(self, *, function_responses: Any) -> Any:
        return self._session.send_tool_response(function_responses=function_responses)

    def receive(self) -> Any:
        return self._session.receive()

    async def close(self) -> None:
        await self._cm.__aexit__(None, None, None)


class GeminiConnector:
    """Opens Gemini Live sockets for one client + adapter (docs 02/07)."""

    def __init__(self, *, client: Any, adapter: GeminiAdapter) -> None:
        self._client = client
        self._adapter = adapter

    @property
    def adapter(self) -> VendorAdapter:
        return self._adapter

    async def open(
        self, spec: AgentSpec, *, resumption_handle: str | None = None
    ) -> LiveTransport:
        config = self._adapter.build_setup(
            spec.setup, resumption_handle=resumption_handle
        )
        cm = self._client.aio.live.connect(model=self._adapter.model, config=config)
        session = await cm.__aenter__()
        return _GeminiLiveTransport(session, cm)
