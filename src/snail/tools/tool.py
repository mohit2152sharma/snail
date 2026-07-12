"""The Tool object — stateless, vendor-independent, reusable (see docs 03).

No result state lives on a Tool: it is reused across agents and concurrent calls. The
transient carriers (ToolCall / ToolResult) are correlated by ``call_id`` elsewhere.
``output_schema`` is required (binds as ``data`` on success). A framework tool (e.g.
``transfer_to``) is caught by the Router instead of dispatched — exposure ≠ authority.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from snail.vendor.params import ToolSpec

#: A handler maps validated args → a neutral, output_schema-shaped value (or raises).
#: Async handlers are supported by the session executor; the pure envelope path here
#: is sync (docs 06 — loop-bound orchestration lives in the session layer).
ToolHandler = Callable[[dict], Any]


class Tool:
    """``name + input_schema + output_schema + handler`` — stateless."""

    __slots__ = (
        "name",
        "handler",
        "description",
        "input_schema",
        "output_schema",
        "is_framework",
        "non_blocking",
        "timeout_s",
    )

    def __init__(
        self,
        name: str,
        handler: ToolHandler,
        *,
        output_schema: dict,
        description: str = "",
        input_schema: dict | None = None,
        is_framework: bool = False,
        non_blocking: bool = False,
        timeout_s: float | None = None,
    ) -> None:
        if not name:
            raise ValueError("Tool.name is required")
        if output_schema is None:
            raise ValueError(f"Tool {name!r}: output_schema is required (docs 03)")
        self.name = name
        self.handler = handler
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.is_framework = is_framework
        self.non_blocking = non_blocking
        self.timeout_s = timeout_s

    def to_spec(self) -> ToolSpec:
        """The vendor-neutral declaration bound at setup (exposure)."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.input_schema,
            non_blocking=self.non_blocking,
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        kind = "framework" if self.is_framework else "agent"
        return f"Tool(name={self.name!r}, {kind})"
