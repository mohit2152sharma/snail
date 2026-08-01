"""Tool layer: static catalog, stateless tools, result envelope (see docs 03)."""

from .context import OnBlock, ToolContext
from .executor import execute
from .input_required import EXPECTS, InputRequired
from .registry import ToolRegistry
from .result import (
    DirectiveMode,
    ResponseMode,
    SpeakDirective,
    ToolResult,
    ToolStatus,
)
from .schema import validate
from .tool import Tool, ToolHandler

__all__ = [
    "Tool",
    "ToolHandler",
    "ToolRegistry",
    "ToolContext",
    "OnBlock",
    "InputRequired",
    "EXPECTS",
    "execute",
    "validate",
    "ToolResult",
    "ToolStatus",
    "ResponseMode",
    "DirectiveMode",
    "SpeakDirective",
]
