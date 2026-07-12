"""Envelope executor: run a Tool handler → a ToolResult (see docs 03).

Validates input against ``input_schema`` (→ ``invalid_args``, with detail so the model
self-corrects), runs the handler (a raise → ``error`` with a **sanitized** reason; the
raw exception is returned separately for log-only capture), then validates the return
against ``output_schema`` (→ ``invalid_output``, generic reason — a tool-side bug).

This is the sync envelope path. Async handlers, per-tool timeout budgets, and
co-operative cancellation are orchestrated by the session layer on the event loop
(docs 04/06); this function is the pure, testable core they wrap.
"""

from __future__ import annotations

from .result import ToolResult
from .schema import validate
from .tool import Tool


def execute(tool: Tool, args: dict) -> tuple[ToolResult, Exception | None]:
    """Run ``tool`` on ``args``. Returns ``(result, raw_exception_for_log_only)``.

    The second element is non-``None`` only on ``error`` — the caller logs it; it never
    reaches the model (sanitization boundary, docs 03).
    """
    err = validate(args, tool.input_schema)
    if err is not None:
        return ToolResult.invalid_args(err), None

    try:
        data = tool.handler(args)
    except Exception as exc:  # noqa: BLE001 - envelope boundary: any raise → error
        # Sanitized, model-facing reason; the raw exc goes to the log only.
        return ToolResult.error(f"{tool.name} failed"), exc

    out_err = validate(data, tool.output_schema)
    if out_err is not None:
        # Real detail (out_err) is a tool-side bug → log only; model gets generic.
        return ToolResult.invalid_output(), AssertionError(out_err)

    return ToolResult.success(data), None
