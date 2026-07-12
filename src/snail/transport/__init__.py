"""Transport — expose the multi-agents over a WebSocket web server (see docs 09§E).

A utility layer whose job is twofold: **define the client ⇄ server wire protocol**
(:mod:`~snail.transport.protocol`) and **serve it** with a lifecycle that owns the
connection pool (:func:`create_app`). The FastAPI/uvicorn choice is the transport's — the
core stays behind the injected :class:`ClientSocket` seam, so the :class:`ClientBridge`
runs against a fake with no server.

Default behaviour: whatever the multi-agent generates is passed to the client.
"""

from .bridge import ClientBridge, PlayoutClock
from .protocol import (
    Control,
    ControlType,
    decode_control,
    encode_control,
)
from .server import create_app
from .socket import DISCONNECT, ClientSocket

__all__ = [
    "Control",
    "ControlType",
    "encode_control",
    "decode_control",
    "ClientSocket",
    "DISCONNECT",
    "ClientBridge",
    "PlayoutClock",
    "create_app",
]
