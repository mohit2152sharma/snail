"""AgentSpec — declarative agent identity (see docs 02).

``AgentSpec`` is **cheap data, no I/O**: the static identity (backend + ``SetupParam``)
plus a stable ``id`` the upper layers (Router, roles, output token) point at. Building
one must never open a socket. The live socket is the :class:`AgentConnection`, which is
swappable underneath a stable spec (recycle) — ``AgentSpec (stable) → AgentConnection
(swappable)``.

The **pool key** is per-``AgentSpec``: two specs share a pool bucket iff they have the
same backend and byte-identical ``SetupParam`` (model, voice, instruction, tools,
modality, input source). Per-client *instructions/tools* are therefore a distinct spec
= distinct bucket — cost scales with instruction-variants, not clients (docs 02).
"""

from __future__ import annotations

import msgspec

from snail.vendor import Backend, SetupParam


class AgentSpec(msgspec.Struct, frozen=True, kw_only=True):
    """Static agent identity — the pool key. No I/O; no socket."""

    id: str
    backend: Backend
    setup: SetupParam

    @property
    def model(self) -> str:
        return self.setup.model

    @property
    def pool_key(self) -> tuple[Backend, bytes]:
        """Stable bucket key: backend + canonical ``SetupParam`` bytes.

        Byte-encoding the frozen struct sidesteps the unhashable ``dict`` in tool
        ``parameters`` while still keying on every setup field that a pooled socket
        was configured with.
        """
        return (self.backend, msgspec.json.encode(self.setup))
