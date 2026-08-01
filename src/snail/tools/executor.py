"""The envelope executor — **the** owner of running a tool handler (see docs 03/14).

One authority, one code path. Everything that runs a handler goes through
:func:`execute`; the session no longer keeps a parallel copy, so the one-shot path and
the blocking path cannot drift apart.

What it does, in order:

* validates ``args`` against ``input_schema`` → ``invalid_args`` with detail, so the
  model can self-correct;
* runs the handler — sync or async, with or without a :class:`ToolContext` — where a
  raise becomes ``error`` with a **sanitized** reason and the raw exception is returned
  separately for log-only capture;
* lets a handler return a ``ToolResult`` directly, for the cases where it knows the
  envelope it wants (``blocked`` after a refused permission, say);
* otherwise validates the return against ``output_schema`` → ``invalid_output``, whose
  reason is deliberately generic because it is a tool-side bug.

Suspension is invisible here. When a handler awaits ``ctx.require(...)`` this coroutine
simply stays parked at that ``await`` until the value arrives; the run's state machine
lives in :mod:`snail.registry.run` and the loop-bound parts (timeout budgets,
cancellation, the carrier call) in the session (docs 06).
"""

from __future__ import annotations

import asyncio
import inspect

from .context import ToolContext
from .result import ToolResult
from .schema import validate
from .tool import Tool


async def execute(
    tool: Tool, args: dict, *, ctx: ToolContext | None = None
) -> tuple[ToolResult, Exception | None]:
    """Run ``tool`` on ``args``. Returns ``(result, raw_exception_for_log_only)``.

    The second element is non-``None`` only on ``error``/``invalid_output`` — the caller
    logs it; it never reaches the model (sanitization boundary, docs 03).
    """
    err = validate(args, tool.input_schema)
    if err is not None:
        return ToolResult.invalid_args(err), None

    try:
        if tool.takes_context:
            raw = tool.handler(args, ctx)
        else:
            raw = tool.handler(args)
        if inspect.isawaitable(raw):
            raw = await raw
    except asyncio.CancelledError:
        raise  # cooperative cancel (barge-in / displacement) — let it propagate
    except Exception as exc:  # noqa: BLE001 - envelope boundary: any raise → error
        # Sanitized, model-facing reason; the raw exc goes to the log only.
        return ToolResult.error(f"{tool.name} failed"), exc

    if isinstance(raw, ToolResult):
        return raw, None  # the handler chose its own envelope

    out_err = validate(raw, tool.output_schema)
    if out_err is not None:
        # Real detail (out_err) is a tool-side bug → log only; model gets generic.
        return ToolResult.invalid_output(), AssertionError(out_err)

    return ToolResult.success(raw), None
