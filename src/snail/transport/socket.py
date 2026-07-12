"""ClientSocket — the WS surface the bridge drives (see docs 09§E).

A structural :class:`ClientSocket` protocol keeps the bridge independent of the concrete
web framework: FastAPI/Starlette's ``WebSocket`` satisfies it directly (``accept`` /
``receive`` / ``send_bytes`` / ``send_text`` / ``close``), and tests inject a fake — the
same inject-the-boundary pattern as the connection layer's ``Connector`` and the
cleaner's ``DenoiseBackend``.

``receive`` follows the ASGI shape Starlette returns: a dict with ``"bytes"`` (media
frame), ``"text"`` (control frame), or ``"type": "websocket.disconnect"`` (client gone).
:data:`DISCONNECT` names that sentinel.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

DISCONNECT = "websocket.disconnect"


@runtime_checkable
class ClientSocket(Protocol):
    """The minimal WebSocket surface the bridge needs (FastAPI ``WebSocket`` fits)."""

    async def accept(self) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def send_text(self, text: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...
