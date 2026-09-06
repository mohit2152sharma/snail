"""Tool layer: static catalog, stateless tools, result envelope (see docs 03)."""

from .context import OnBlock, ToolContext
from .executor import execute
from .input_required import EXPECTS, InputRequired
from .provide_input import (
    PROVIDE_INPUT,
    PROVIDE_INPUT_INSTRUCTION,
    SLOT_BY_EXPECTS,
    build_provide_input_tool,
    declared_keys,
    extract_value,
    provide_input_schema,
)
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
    "PROVIDE_INPUT",
    "PROVIDE_INPUT_INSTRUCTION",
    "SLOT_BY_EXPECTS",
    "build_provide_input_tool",
    "provide_input_schema",
    "declared_keys",
    "extract_value",
    "execute",
    "validate",
    "ToolResult",
    "ToolStatus",
    "ResponseMode",
    "DirectiveMode",
    "SpeakDirective",
]
