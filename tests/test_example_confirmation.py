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
    RECORDING_CONSENT,
    build_registry,
)

from snail.tools import PROVIDE_INPUT, PROVIDE_INPUT_INSTRUCTION  # noqa: E402


def test_registry_exposes_the_three_tools_plus_provide_input() -> None:
    registry = build_registry()
    assert set(registry.names()) == {
        "look_and_tell", "record_meeting", "get_date_and_time", PROVIDE_INPUT
    }


def test_provide_input_key_enum_is_built_from_the_declared_consents() -> None:
    spec = build_registry().get(PROVIDE_INPUT)
    assert spec is not None
    enum = spec.input_schema["properties"]["key"]["enum"]
    assert enum == [CAMERA_CONSENT, RECORDING_CONSENT]


def test_instruction_carries_the_protocol_block_verbatim() -> None:
    assert PROVIDE_INPUT_INSTRUCTION in SYSTEM_INSTRUCTION
    for name in ("look_and_tell", "record_meeting", "get_date_and_time"):
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


async def test_direct_tool_needs_no_round_trip() -> None:
    wire = demo.Wire()
    await wire.calls_tool("get_date_and_time")
    assert len(wire.sent) == 1
    assert wire.sent[0]["payload"]["status"] == "success"


async def test_walkthrough_runs_clean(capsys) -> None:
    await demo.main()
    out = capsys.readouterr().out
    assert "input_required" in out and "no_blocked_run" in out
