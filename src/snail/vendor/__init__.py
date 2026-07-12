"""Vendor boundary: capability descriptor, neutral params, adapter protocol.

See docs 07-vendor-capability-matrix. The adapter is pure translation (neutral ↔
wire); the live socket lives in the connection layer. ``MockVendorAdapter`` is the
key-free stand-in for deterministic tests (09§E).
"""

from .base import VendorAdapter
from .capabilities import Backend, VendorCapabilities
from .gemini import GeminiAdapter, gemini_capabilities
from .events import (
    AgentTranscript,
    GoAway,
    Interrupted,
    ParsedEvent,
    ResumptionUpdate,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
    VendorError,
)
from .media import MediaChunk, MediaKind, RealtimeControl
from .mock import MockVendorAdapter
from .params import (
    InputSource,
    JoinContext,
    ResponseModality,
    SetupParam,
    ToolSpec,
)

__all__ = [
    "VendorAdapter",
    "Backend",
    "VendorCapabilities",
    "SetupParam",
    "JoinContext",
    "ResponseModality",
    "InputSource",
    "ToolSpec",
    "MediaChunk",
    "MediaKind",
    "RealtimeControl",
    "MockVendorAdapter",
    "GeminiAdapter",
    "gemini_capabilities",
    # parsed events
    "ParsedEvent",
    "UserTranscript",
    "AgentTranscript",
    "ToolCallRequest",
    "TurnComplete",
    "Interrupted",
    "GoAway",
    "ResumptionUpdate",
    "VendorError",
]
