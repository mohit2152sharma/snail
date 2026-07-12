"""Tests for the tool layer: schema, Tool, ToolRegistry, execute, ToolResult (docs 03)."""

from __future__ import annotations

import pytest

from snail.tools import (
    ResponseMode,
    Tool,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    execute,
    validate,
)

_OBJ = {
    "type": "object",
    "properties": {"acct": {"type": "integer"}, "note": {"type": "string"}},
    "required": ["acct"],
}


# --- schema validator ---


def test_validate_ok_and_missing_required() -> None:
    assert validate({"acct": 7}, _OBJ) is None
    assert "required" in validate({}, _OBJ)


def test_validate_type_mismatch_and_bool_is_not_integer() -> None:
    assert "expected integer" in validate({"acct": "x"}, _OBJ)
    # bool must not satisfy integer/number
    assert validate({"acct": True}, _OBJ) is not None


def test_validate_array_items_and_enum_and_nullable() -> None:
    arr = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}
    assert validate(["a", "b"], arr) is None
    assert validate(["a", "c"], arr) is not None
    nullable = {"type": "string", "nullable": True}
    assert validate(None, nullable) is None
    assert validate(None, {"type": "string"}) is not None


# --- Tool ---


def test_tool_requires_output_schema() -> None:
    with pytest.raises(ValueError):
        Tool("f", lambda a: a, output_schema=None)  # type: ignore[arg-type]


def test_tool_to_spec() -> None:
    t = Tool(
        "get_balance",
        lambda a: {"balance": 1},
        description="d",
        input_schema=_OBJ,
        output_schema={"type": "object"},
        non_blocking=True,
    )
    spec = t.to_spec()
    assert spec.name == "get_balance"
    assert spec.parameters == _OBJ
    assert spec.non_blocking is True


# --- ToolRegistry ---


def test_registry_register_dup_get_specs() -> None:
    reg = ToolRegistry()
    a = reg.register(Tool("a", lambda x: {}, output_schema={"type": "object"}))
    reg.register(Tool("b", lambda x: {}, output_schema={"type": "object"}))
    assert reg.get("a") is a
    assert "a" in reg and len(reg) == 2
    with pytest.raises(ValueError):
        reg.register(Tool("a", lambda x: {}, output_schema={"type": "object"}))
    # exposure subset
    specs = reg.specs(["b", "missing"])
    assert [s.name for s in specs] == ["b"]


# --- execute (envelope) ---


def _tool(handler, in_s=_OBJ, out_s=None) -> Tool:
    return Tool(
        "t", handler, input_schema=in_s, output_schema=out_s or {"type": "object"}
    )


def test_execute_success() -> None:
    res, exc = execute(_tool(lambda a: {"ok": True}), {"acct": 1})
    assert res.status is ToolStatus.SUCCESS
    assert res.data == {"ok": True}
    assert exc is None


def test_execute_invalid_args() -> None:
    res, exc = execute(_tool(lambda a: {}), {})  # missing required acct
    assert res.status is ToolStatus.INVALID_ARGS
    assert res.retriable is True
    assert "acct" in res.reason


def test_execute_handler_raise_is_sanitized() -> None:
    def boom(a):
        raise RuntimeError("secret stack detail")

    res, exc = execute(_tool(boom), {"acct": 1})
    assert res.status is ToolStatus.ERROR
    assert "secret" not in (res.reason or "")  # sanitized
    assert isinstance(exc, RuntimeError)  # raw exc returned for log-only


def test_execute_invalid_output() -> None:
    # handler returns a string but output_schema wants an object
    res, exc = execute(_tool(lambda a: "nope"), {"acct": 1})
    assert res.status is ToolStatus.INVALID_OUTPUT
    assert exc is not None  # detail captured for the log, not the model


# --- ToolResult constructors / cascade ---


def test_result_success_is_silent_by_default() -> None:
    r = ToolResult.success({"x": 1})
    assert r.response_mode is ResponseMode.SILENT
    assert r.speak_directive is None


def test_result_error_speaks_with_default_directive() -> None:
    r = ToolResult.error()
    assert r.response_mode is ResponseMode.SPEAK
    assert r.speak_directive is not None
    assert "apolog" in r.speak_directive.text


def test_result_blocked_skipped_timeout_notfound() -> None:
    assert ToolResult.blocked().response_mode is ResponseMode.SPEAK
    assert ToolResult.skipped().response_mode is ResponseMode.SILENT
    assert ToolResult.timeout().retriable is True
    assert "does not exist" in ToolResult.not_found("ghost").reason
