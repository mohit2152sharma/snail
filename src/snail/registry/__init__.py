"""In-flight tool-call tracking (see docs 04).

``ToolCallRegistry`` is the live tracker ("what calls are happening now"), distinct
from the static :class:`snail.tools.ToolRegistry` catalog ("what tools exist").
"""

from .call_registry import RegistryFull, ToolCallRegistry
from .pending import (
    CallState,
    Destination,
    PendingCall,
    Promise,
    TERMINAL_STATES,
)

__all__ = [
    "ToolCallRegistry",
    "RegistryFull",
    "PendingCall",
    "Promise",
    "CallState",
    "Destination",
    "TERMINAL_STATES",
]
