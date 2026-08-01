"""The confirmation example, run headless (``examples/confirmation``).

Keeps the example honest: the tools, the instruction and the scripted walkthrough all
have to still work. The framework-side behaviour they exercise is asserted in
``test_session_runs.py``; here the subject is the example itself.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("examples").resolve()))

from confirmation import demo  # noqa: E402
from confirmation.agent import SYSTEM_INSTRUCTION  # noqa: E402
from confirmation.tools import (  # noqa: E402
    CAMERA_CONSENT,
    PHONE_NUMBER,
    RECORDING_CONSENT,
    build_registry,
)

from snail.tools import PROVIDE_INPUT, PROVIDE_INPUT_INSTRUCTION  # noqa: E402


TOOL_NAMES = ("look_and_tell", "record_meeting", "make_call", "get_date_and_time")


def test_registry_exposes_every_tool_plus_provide_input() -> None:
    assert set(build_registry().names()) == {*TOOL_NAMES, PROVIDE_INPUT}


def test_provide_input_key_enum_is_built_from_the_declared_keys() -> None:
    spec = build_registry().get(PROVIDE_INPUT)
    assert spec is not None
    enum = spec.input_schema["properties"]["key"]["enum"]
    assert enum == [CAMERA_CONSENT, RECORDING_CONSENT, PHONE_NUMBER]


def test_instruction_carries_the_protocol_block_verbatim() -> None:
    assert PROVIDE_INPUT_INSTRUCTION in SYSTEM_INSTRUCTION
    for name in TOOL_NAMES:
        assert name in SYSTEM_INSTRUCTION


async def test_consent_granted_answers_the_original_question() -> None:
    wire = demo.Wire()
    await wire.calls_tool("look_and_tell", question="what does this sign say?")
    assert wire.sent[0]["payload"]["status"] == "input_required"
    assert wire.sent[0]["payload"]["key"] == CAMERA_CONSENT

    await wire.calls_tool(
        "provide_input", for_tool="look_and_tell", key=CAMERA_CONSENT, bool_value=True
    )
    payload = wire.sent[1]["payload"]
    assert payload["status"] == "success"
    # The question survived the wait — that is the whole point of the run outliving
    # the call that asked for consent.
    assert payload["data"]["question"] == "what does this sign say?"
    assert "NO ENTRY" in payload["data"]["answer"]


def test_instruction_tells_the_model_a_refusal_is_an_answer() -> None:
    """The false path below is only reachable if the instruction authorises it.

    An earlier wording ("if they refuse ... do not call provide_input") conflated a
    refusal with no answer, so the model heard "no", said "okay, I won't", and called
    nothing — the run sat blocked until it expired. Every submitted answer in a live
    session was ``true``. The mechanics were never at fault, so the guard belongs here.
    """
    text = PROVIDE_INPUT_INSTRUCTION.lower()
    assert "a refusal is an answer" in text
    assert "bool_value: false" in text


async def test_consent_refused_does_not_record() -> None:
    wire = demo.Wire()
    await wire.calls_tool("record_meeting", title="standup")
    await wire.calls_tool(
        "provide_input", for_tool="record_meeting", key=RECORDING_CONSENT,
        bool_value=False,
    )
    assert wire.sent[1]["payload"] == {
        "status": "blocked", "reason": "the user did not consent to recording"
    }


async def test_call_with_a_number_never_blocks() -> None:
    """Whether a run blocks is a runtime fact, not a property of the tool."""
    wire = demo.Wire()
    await wire.calls_tool("make_call", number="(555) 123 4567")
    assert len(wire.sent) == 1
    assert wire.sent[0]["payload"] == {
        "status": "success",
        "data": {"calling": "5551234567", "call": "call://mock/5551234567"},
    }


async def test_call_without_a_number_asks_for_one_as_a_string() -> None:
    wire = demo.Wire()
    await wire.calls_tool("make_call")
    ask = wire.sent[0]["payload"]
    assert ask["status"] == "input_required"
    assert (ask["key"], ask["expects"]) == (PHONE_NUMBER, "string")

    # expects=string → the answer rides text_value; bool_value would be invalid_args.
    await wire.calls_tool(
        "provide_input", for_tool="make_call", key=PHONE_NUMBER,
        text_value="+91 98765-43210",
    )
    assert wire.sent[1]["payload"]["data"]["calling"] == "+919876543210"


async def test_a_non_number_is_refused_rather_than_dialled() -> None:
    wire = demo.Wire()
    await wire.calls_tool("make_call")
    await wire.calls_tool(
        "provide_input", for_tool="make_call", key=PHONE_NUMBER, text_value="um, dunno",
    )
    assert wire.sent[1]["payload"]["status"] == "blocked"


async def test_direct_tool_needs_no_round_trip() -> None:
    wire = demo.Wire()
    await wire.calls_tool("get_date_and_time")
    assert len(wire.sent) == 1
    assert wire.sent[0]["payload"]["status"] == "success"


async def test_walkthrough_runs_clean(capsys) -> None:
    await demo.main()
    out = capsys.readouterr().out
    assert "input_required" in out and "no_blocked_run" in out
