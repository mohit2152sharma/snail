"""Vendor boundary: capability descriptor, neutral params, adapter protocol.

See docs 07-vendor-capability-matrix. The adapter is pure translation (neutral ↔
wire); the live socket lives in the connection layer. ``MockVendorAdapter`` is the
key-free stand-in for deterministic tests (09§E).
"""

from .base import VendorAdapter
from .capabilities import Backend, VendorCapabilities
from .gemini import (
    GeminiAdapter,
    clamp_silence_ms,
    gemini_capabilities,
    realtime_input_config,
)
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
    MIN_SILENCE_DURATION_MS,
    InputSource,
    JoinContext,
    ResponseModality,
    SetupParam,
    ToolSpec,
    TurnDetectionParam,
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
    "TurnDetectionParam",
    "MIN_SILENCE_DURATION_MS",
    "MediaChunk",
    "MediaKind",
    "RealtimeControl",
    "MockVendorAdapter",
    "GeminiAdapter",
    "clamp_silence_ms",
    "gemini_capabilities",
    "realtime_input_config",
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
