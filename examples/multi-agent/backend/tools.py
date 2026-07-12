"""Tool registries for host and echo.

The handlers are intentionally trivial: their only job is to *resolve* so the Session
emits a ``TOOL_RESULT`` routing signal. The Router (routing.py) turns that signal into
the actual agent handoff — the tool itself neither knows nor cares about routing.
"""

from __future__ import annotations

from snail.tools import Tool, ToolRegistry

_OK_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
_EMPTY_INPUT = {"type": "object", "properties": {}}


def host_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            "start_echo",
            lambda args: {"ok": True},
            output_schema=_OK_SCHEMA,
            input_schema=_EMPTY_INPUT,
            description="Switch to echo mode.",
        )
    )
    reg.register(
        Tool(
            "start_translation",
            lambda args: {"ok": True},
            output_schema=_OK_SCHEMA,
            input_schema=_EMPTY_INPUT,
            description="Switch to translation mode (translate to Hindi).",
        )
    )
    return reg


def echo_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            "stop",
            lambda args: {"ok": True},
            output_schema=_OK_SCHEMA,
            input_schema=_EMPTY_INPUT,
            description="Return to the host.",
        )
    )
    return reg
