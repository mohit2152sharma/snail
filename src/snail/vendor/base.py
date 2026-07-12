"""VendorAdapter protocol — the vendor-neutral ↔ wire boundary (see docs 01/07).

The adapter is **pure translation**: it serializes neutral ``Item[]`` / setup / tool
results to a vendor's wire shape, and parses vendor wire messages into neutral
:mod:`ParsedEvent`\\ s. It owns no socket — the live connection (``AgentConnection``)
wraps a socket and *uses* an adapter. Keeping translation socket-free is what makes it
testable without a vendor key (see :class:`~snail.vendor.mock.MockVendorAdapter`).

This is the hard boundary from docs 01: nothing above the adapter holds a vendor
payload; nothing below produces a neutral ``Item``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from snail.context import Item

from .capabilities import VendorCapabilities
from .events import ParsedEvent
from .media import MediaChunk, RealtimeControl
from .params import SetupParam


@runtime_checkable
class VendorAdapter(Protocol):
    """Translate between the neutral surface and one vendor's wire format."""

    @property
    def name(self) -> str:
        """Human-readable adapter name, e.g. ``"gemini"`` / ``"mock"``."""
        ...

    @property
    def capabilities(self) -> VendorCapabilities:
        """What this ``(vendor, model, backend)`` supports (docs 07)."""
        ...

    def build_setup(self, setup: SetupParam) -> dict:
        """Serialize the static identity to the vendor's setup/config message."""
        ...

    def serialize_item(self, item: Item) -> dict:
        """Serialize one neutral ``Item`` to a vendor content turn.

        Vendors that forbid ``system`` content turns (Gemini — docs 07) down-convert a
        ``SYSTEM`` item here (fold into instructions / a leading ``user`` turn).
        """
        ...

    def serialize_history(self, items: list[Item]) -> list[dict]:
        """Serialize projected history for injection on join (before first turn)."""
        ...

    # --- outbound seams (converged on the multimodal shape, docs 07) ------

    def serialize_realtime(self, chunk: MediaChunk) -> Any:
        """Serialize a streaming multimodal chunk (audio/image/text) for the vendor's
        realtime channel (Gemini ``send_realtime_input``)."""
        ...

    def serialize_realtime_control(self, control: RealtimeControl) -> Any:
        """Serialize an out-of-band realtime control marker (activity/stream-end)."""
        ...

    def serialize_turns(self, items: list[Item], *, complete: bool) -> Any:
        """Serialize ordered content turns (Gemini ``send_client_content``).

        ``complete`` maps to ``turn_complete`` — set ``False`` to inject history
        before the first model turn, ``True`` to trigger a response.
        """
        ...

    def serialize_tool_result(
        self, *, call_id: str, name: str, content: str, meta: dict | None = None
    ) -> Any:
        """Serialize a tool result to the vendor's function-response shape."""
        ...

    def parse_event(self, raw: dict) -> list[ParsedEvent]:
        """Parse one raw vendor wire message into zero or more neutral events."""
        ...
