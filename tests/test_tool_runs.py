"""Tests for multi-step tool runs (docs 14).

Covers the four primitives: ``InputRequired``, the ``input_required`` envelope and its
wire payload, ``ToolContext.require`` blocking through the one authoritative executor,
and the per-agent ``RunSlots`` state machine.

``RunSlots`` is deliberately loop-free, so most of it is tested with a plain
:class:`Promise` and an injected clock — no event loop, no vendor.
"""

from __future__ import annotations

import asyncio

import pytest

from snail.registry import (
    Promise,
    RunSlots,
    RunState,
    SubmitOutcome,
)
from snail.tools import (
    PROVIDE_INPUT,
    InputRequired,
    Tool,
    ToolContext,
    ToolResult,
    ToolStatus,
    build_provide_input_tool,
    declared_keys,
    execute,
    extract_value,
    provide_input_schema,
    validate,
)

_OBJ = {"type": "object"}


def _consent(key: str = "location_permission", **kw) -> InputRequired:
    return InputRequired(key=key, expects="boolean", ask="ask about location", **kw)


# --- InputRequired -----------------------------------------------------------


def test_input_required_schema_feeds_the_existing_validator() -> None:
    assert _consent().schema == {"type": "boolean"}
    assert InputRequired(key="city", expects="string").schema == {"type": "string"}


def test_input_required_rejects_unknown_expects() -> None:
    with pytest.raises(ValueError, match="expects must be one of"):
        InputRequired(key="k", expects="address")


def test_input_required_requires_a_key() -> None:
    with pytest.raises(ValueError, match="key is required"):
        InputRequired(key="", expects="boolean")


# --- the envelope + wire payload --------------------------------------------


def test_input_required_result_speaks_the_ask() -> None:
    r = ToolResult.input_required("get_weather", _consent())
    assert r.status is ToolStatus.INPUT_REQUIRED
    assert r.response_mode.value == "speak"
    assert r.speak_directive is not None
    assert r.speak_directive.text == "ask about location"


def test_input_required_payload_carries_context_for_the_model() -> None:
    payload = ToolResult.input_required("get_weather", _consent()).to_payload()
    assert payload == {
        "status": "input_required",
        "for_tool": "get_weather",
        "key": "location_permission",
        "expects": "boolean",
        "ask": "ask about location",
    }


def test_payload_keeps_status_and_retriable_that_used_to_be_dropped() -> None:
    # The Gemini adapter previously flattened every result to {"result": str},
    # so status/retriable never reached the model at all (docs 14, A1).
    assert ToolResult.success({"t": 31}).to_payload() == {
        "status": "success",
        "data": {"t": 31},
    }
    assert ToolResult.invalid_args("acct: required field missing").to_payload() == {
        "status": "invalid_args",
        "reason": "acct: required field missing",
        "retriable": True,
    }
    assert ToolResult.skipped().to_payload() == {
        "status": "skipped",
        "reason": "handled elsewhere",
    }


# --- Tool: context arity + declarations --------------------------------------


def test_handler_arity_decides_context_passing() -> None:
    assert Tool("a", lambda args: {}, output_schema=_OBJ).takes_context is False
    assert Tool("b", lambda args, ctx: {}, output_schema=_OBJ).takes_context is True


def test_declared_requirements_are_keyed_and_unique() -> None:
    t = Tool("w", lambda a, c: {}, output_schema=_OBJ, requires=(_consent(),))
    assert t.declared["location_permission"].expects == "boolean"
    with pytest.raises(ValueError, match="duplicate InputRequired key"):
        Tool("w", lambda a, c: {}, output_schema=_OBJ, requires=(_consent(), _consent()))


# --- execute: the one authoritative owner ------------------------------------


async def test_handler_blocks_then_resumes_through_execute() -> None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    seen: list[InputRequired] = []

    async def on_block(pending: InputRequired):
        seen.append(pending)
        return await fut

    async def handler(args, ctx):
        granted = await ctx.require("location_permission")
        return {"granted": granted}

    tool = Tool("get_weather", handler, output_schema=_OBJ, requires=(_consent(),))
    ctx = ToolContext(on_block, declared=tool.declared)

    task = asyncio.create_task(execute(tool, {}, ctx=ctx))
    await asyncio.sleep(0)  # let the handler reach the await
    assert seen[0].key == "location_permission"
    assert seen[0].ask == "ask about location"  # default wording from the declaration

    fut.set_result(True)
    result, exc = await task
    assert (result.status, result.data, exc) == (
        ToolStatus.SUCCESS,
        {"granted": True},
        None,
    )


async def test_require_overrides_wording_per_invocation() -> None:
    seen: list[InputRequired] = []

    async def on_block(pending):
        seen.append(pending)
        return True

    async def handler(args, ctx):
        await ctx.require("location_permission", ask="ask about location in Delhi")
        return {}

    tool = Tool("w", handler, output_schema=_OBJ, requires=(_consent(),))
    await execute(tool, {}, ctx=ToolContext(on_block, declared=tool.declared))
    assert seen[0].ask == "ask about location in Delhi"
    assert seen[0].expects == "boolean"  # unspecified fields still come from the decl


async def test_require_works_with_no_declaration() -> None:
    seen: list[InputRequired] = []

    async def on_block(pending):
        seen.append(pending)
        return "Delhi"

    async def handler(args, ctx):
        return {"city": await ctx.require("city", expects="string", ask="which city?")}

    result, _ = await execute(
        Tool("w", handler, output_schema=_OBJ), {}, ctx=ToolContext(on_block)
    )
    assert result.data == {"city": "Delhi"}
    assert seen[0].expects == "string"


async def test_handler_may_return_its_own_envelope() -> None:
    async def handler(args, ctx):
        return ToolResult.blocked("the user declined location access")

    result, exc = await execute(
        Tool("w", handler, output_schema=_OBJ), {}, ctx=ToolContext(lambda p: None)
    )
    assert result.status is ToolStatus.BLOCKED
    assert exc is None


# --- RunSlots: one run per agent ---------------------------------------------


def test_newer_call_displaces_the_same_agent_only() -> None:
    slots = RunSlots()
    weather, _ = slots.start("agent-a", "get_weather", carrier_call_id="fc_1")
    other, _ = slots.start("agent-b", "get_news", carrier_call_id="fc_2")

    cab, displaced = slots.start("agent-a", "book_cab", carrier_call_id="fc_3")

    assert displaced is weather
    assert weather.state is RunState.CANCELLED
    assert slots.get("agent-a") is cab
    assert slots.get("agent-b") is other  # untouched — displacement never crosses agents
    assert len(slots) == 2


def test_displaced_run_does_not_evict_its_replacement() -> None:
    slots = RunSlots()
    first, _ = slots.start("a", "get_weather")
    second, _ = slots.start("a", "book_cab")
    assert slots.finish(first) is False  # stale completion arrives late
    assert slots.get("a") is second


def test_blocking_consumes_the_carrier_and_sets_a_deadline() -> None:
    slots = RunSlots()
    run, _ = slots.start("a", "get_weather", carrier_call_id="fc_1", now=100.0)
    slots.block(run, _consent(budget_s=30.0), resume=Promise(), now=100.0)
    assert run.state is RunState.BLOCKED
    assert run.carrier_call_id is None  # the ask already went out on fc_1
    assert run.deadline == 130.0
    assert slots.blocked("a") is run


def test_submit_resumes_the_parked_handler() -> None:
    slots = RunSlots()
    run, _ = slots.start("a", "get_weather")
    promise = Promise()
    slots.block(run, _consent(), resume=promise)

    outcome, resumed = slots.submit(
        "a", for_tool="get_weather", key="location_permission", value=True
    )
    assert outcome is SubmitOutcome.ACCEPTED
    assert resumed is run
    assert run.state is RunState.EXECUTING
    assert run.pending is None
    assert promise.done() and promise.result() is True


@pytest.mark.parametrize(
    ("for_tool", "key", "value", "expected"),
    [
        ("book_cab", "location_permission", True, SubmitOutcome.FOR_TOOL_MISMATCH),
        ("get_weather", "payment_ok", True, SubmitOutcome.KEY_MISMATCH),
        ("get_weather", "location_permission", "yes", SubmitOutcome.TYPE_MISMATCH),
    ],
)
def test_a_bad_submit_leaves_the_run_answerable(
    for_tool: str, key: str, value: object, expected: SubmitOutcome
) -> None:
    slots = RunSlots()
    run, _ = slots.start("a", "get_weather")
    promise = Promise()
    slots.block(run, _consent(), resume=promise)

    outcome, _ = slots.submit("a", for_tool=for_tool, key=key, value=value)
    assert outcome is expected
    assert run.state is RunState.BLOCKED  # still waiting
    assert promise.done() is False  # the handler was not fed a wrong value


def test_submit_with_nothing_blocked_is_a_no_op() -> None:
    slots = RunSlots()
    assert slots.submit("a", for_tool="t", key="k", value=True) == (
        SubmitOutcome.NO_BLOCKED_RUN,
        None,
    )
    slots.start("a", "get_weather")  # executing, not blocked
    outcome, _ = slots.submit("a", for_tool="get_weather", key="k", value=True)
    assert outcome is SubmitOutcome.NO_BLOCKED_RUN


def test_real_booleans_only() -> None:
    slots = RunSlots()
    run, _ = slots.start("a", "t")
    slots.block(run, _consent(), resume=Promise())
    for wrong in ("true", 1, None):
        outcome, _ = slots.submit(
            "a", for_tool="t", key="location_permission", value=wrong
        )
        assert outcome is SubmitOutcome.TYPE_MISMATCH


def test_budget_expiry_cancels_only_blocked_runs() -> None:
    slots = RunSlots()
    blocked, _ = slots.start("a", "get_weather", now=0.0)
    slots.block(blocked, _consent(budget_s=30.0), resume=Promise(), now=0.0)
    running, _ = slots.start("b", "get_news", now=0.0)

    assert slots.sweep_expired(now=29.0) == []
    assert slots.sweep_expired(now=31.0) == [blocked]
    assert blocked.state is RunState.CANCELLED
    assert slots.get("a") is None
    assert slots.get("b") is running  # an executing run has no human budget


# --- provide_input: one tool, typed slots ------------------------------------


def test_key_enum_is_built_from_declared_requirements() -> None:
    weather = Tool("get_weather", lambda a, c: {}, output_schema=_OBJ, requires=(_consent(),))
    cab = Tool(
        "book_cab",
        lambda a, c: {},
        output_schema=_OBJ,
        requires=(_consent(), InputRequired(key="payment_ok", expects="boolean")),
    )
    # de-duped across tools, order preserved
    assert declared_keys([weather, cab]) == ("location_permission", "payment_ok")

    schema = provide_input_schema(declared_keys([weather, cab]))
    assert schema["properties"]["key"]["enum"] == ["location_permission", "payment_ok"]
    assert schema["required"] == ["for_tool", "key"]


def test_key_degrades_to_a_plain_string_with_nothing_declared() -> None:
    schema = provide_input_schema(())
    assert "enum" not in schema["properties"]["key"]
    assert schema["properties"]["key"]["type"] == "string"


def test_declared_tool_is_framework_and_accepts_a_well_formed_call() -> None:
    tool = build_provide_input_tool(("location_permission",))
    assert tool.name == PROVIDE_INPUT
    assert tool.is_framework is True
    args = {"for_tool": "get_weather", "key": "location_permission", "bool_value": True}
    assert validate(args, tool.input_schema) is None


def test_a_key_outside_the_enum_fails_validation() -> None:
    tool = build_provide_input_tool(("location_permission",))
    args = {"for_tool": "get_weather", "key": "made_up", "bool_value": True}
    assert "not in enum" in (validate(args, tool.input_schema) or "")


@pytest.mark.parametrize(
    ("expects", "args", "value"),
    [
        ("boolean", {"bool_value": True}, True),
        ("string", {"text_value": "Delhi"}, "Delhi"),
        ("number", {"number_value": 31.5}, 31.5),
        ("integer", {"number_value": 3}, 3),
    ],
)
def test_value_is_read_from_the_slot_named_by_expects(
    expects: str, args: dict, value: object
) -> None:
    assert extract_value(args, expects) == (value, None)


def test_extra_slots_are_ignored_not_rejected() -> None:
    args = {"bool_value": True, "text_value": "yes", "number_value": 1}
    assert extract_value(args, "boolean") == (True, None)


def test_a_missing_slot_names_the_one_to_use() -> None:
    value, err = extract_value({"text_value": "yes"}, "boolean")
    assert value is None
    assert "bool_value is required" in err


def test_wrong_slot_is_caught_end_to_end_and_leaves_the_run_blocked() -> None:
    # model answers a boolean question in text_value → invalid_args, run still waiting
    slots = RunSlots()
    run, _ = slots.start("a", "get_weather")
    promise = Promise()
    slots.block(run, _consent(), resume=promise)

    value, err = extract_value({"text_value": "yes"}, run.pending.expects)
    assert err is not None and value is None
    assert slots.blocked("a") is run
    assert promise.done() is False


def test_cancel_agent_frees_the_slot() -> None:
    slots = RunSlots()
    run, _ = slots.start("a", "get_weather")
    assert slots.cancel_agent("a") is run
    assert run.state is RunState.CANCELLED
    assert slots.get("a") is None
    assert slots.cancel_agent("a") is None
