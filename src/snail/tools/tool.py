"""The Tool object — stateless, vendor-independent, reusable (see docs 03).

No result state lives on a Tool: it is reused across agents and concurrent calls. The
transient carriers (ToolCall / ToolResult) are correlated by ``call_id`` elsewhere.
``output_schema`` is required (binds as ``data`` on success). A framework tool (e.g.
``transfer_to``) is caught by the Router instead of dispatched — exposure ≠ authority.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from snail.vendor.params import ToolSpec

from .input_required import InputRequired

#: A handler maps validated args → a neutral, output_schema-shaped value (or raises).
#: It may take a second parameter, a :class:`~snail.tools.context.ToolContext`, to block
#: on external input (docs 14); the arity is detected once, here, not per call.
#: Sync and async handlers are both supported by :func:`snail.tools.execute`.
ToolHandler = Callable[..., Any]


def _takes_context(handler: ToolHandler) -> bool:
    """True when ``handler`` accepts ``(args, ctx)`` rather than ``(args)``."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):  # builtins / exotic callables → assume (args)
        return False
    positional = [
        p
        for p in params.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


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
        "requires",
        "takes_context",
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
        requires: tuple[InputRequired, ...] = (),
    ) -> None:
        if not name:
            raise ValueError("Tool.name is required")
        if output_schema is None:
            raise ValueError(f"Tool {name!r}: output_schema is required (docs 03)")
        keys = [r.key for r in requires]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Tool {name!r}: duplicate InputRequired key")
        self.name = name
        self.handler = handler
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.is_framework = is_framework
        self.non_blocking = non_blocking
        self.timeout_s = timeout_s
        #: External input this tool may block on. Optional — a handler can also build
        #: an ``InputRequired`` at the call site (docs 14, O4).
        self.requires = requires
        self.takes_context = _takes_context(handler)

    @property
    def declared(self) -> dict[str, InputRequired]:
        """``key → InputRequired`` for defaults at ``ctx.require`` call sites."""
        return {r.key: r for r in self.requires}

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
