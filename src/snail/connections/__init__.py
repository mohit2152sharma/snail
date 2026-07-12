"""Connections & pool (see docs 02).

Owns the vendor socket the rest of Snail deliberately keeps at arm's length:

* :class:`AgentSpec` — declarative, swap-stable agent identity (the pool key).
* :class:`AgentConnection` — one live socket: neutral send seams + inbound read loop +
  lifecycle bookkeeping; the swappable half (recycle) under a stable spec.
* :class:`LiveTransport` — the raw socket surface (Gemini's ``AsyncSession`` fits it;
  tests inject a fake).
* :class:`Connector` / :class:`GeminiConnector` — the only network-touching seam.
* :class:`ConnectionPool` — per-``AgentSpec`` warm-socket pool: pre-warm / acquire /
  park / recycle / evict, with a global admission cap.
"""

from .connection import (
    AgentConnection,
    ConnectionMeta,
    ConnectionState,
    LiveTransport,
)
from .connector import Connector, GeminiConnector
from .pool import ConnectionPool
from .spec import AgentSpec

__all__ = [
    "AgentSpec",
    "AgentConnection",
    "ConnectionMeta",
    "ConnectionState",
    "LiveTransport",
    "Connector",
    "GeminiConnector",
    "ConnectionPool",
]
