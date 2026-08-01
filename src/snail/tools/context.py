"""``ToolContext`` — the handler-facing surface for blocking on external input (docs 14).

A handler that needs something from outside the process awaits it::

    async def get_weather(args, ctx):
        granted = await ctx.require("location_permission")
        ...

That is the entire authoring surface. The handler stays linear; the state machine is
framework-side (``registry/run.py``), so no tool declares its own.

This module holds no event loop. ``require`` awaits whatever the injected ``on_block``
returns, which is where the loop lives (the session, docs 06). Tests drive it with a
plain resolved awaitable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .input_required import InputRequired

#: Called when a handler blocks. Returns an awaitable resolving to the submitted value.
#: The session implements it: park the run, close the carrier call with
#: ``input_required``, and hand back the future the resume will resolve.
OnBlock = Callable[[InputRequired], Awaitable[Any]]


class ToolContext:
    """Per-run handle passed to handlers that take a second parameter."""

    __slots__ = ("_on_block", "_declared")

    def __init__(
        self,
        on_block: OnBlock,
        *,
        declared: dict[str, InputRequired] | None = None,
    ) -> None:
        self._on_block = on_block
        self._declared = declared or {}

    async def require(
        self,
        key: str,
        *,
        expects: str | None = None,
        ask: str | None = None,
        budget_s: float | None = None,
    ) -> Any:
        """Block until ``key`` is supplied, then return the value.

        Fields default to the tool's declaration for ``key`` when it has one, so the
        common call is just ``await ctx.require("location_permission")``. Passing
        ``ask`` overrides the wording for this invocation, which is how a question can
        depend on what the handler has computed so far.

        With no declaration, everything must be supplied here — the executor never
        needs to *look up* what a key means, it is holding the ``InputRequired`` it
        was just handed (docs 14, O4).
        """
        base = self._declared.get(key)
        if base is None:
            pending = InputRequired(
                key=key,
                expects=expects or "boolean",
                ask=ask or "",
                **({"budget_s": budget_s} if budget_s is not None else {}),
            )
        else:
            pending = InputRequired(
                key=key,
                expects=expects or base.expects,
                ask=ask if ask is not None else base.ask,
                budget_s=budget_s if budget_s is not None else base.budget_s,
            )
        return await self._on_block(pending)
