"""Tests for the vendor boundary + MockVendorAdapter (docs 07, 09§E)."""

from __future__ import annotations

import msgspec

from snail.context import Item, Role
from snail.vendor import (
    AgentTranscript,
    Backend,
    GoAway,
    InputSource,
    Interrupted,
    MockVendorAdapter,
    ResponseModality,
    ResumptionUpdate,
    SetupParam,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    UserTranscript,
    VendorAdapter,
    VendorCapabilities,
)


def test_mock_satisfies_adapter_protocol() -> None:
    assert isinstance(MockVendorAdapter(), VendorAdapter)


def test_default_capabilities_are_gemini_dev_profile() -> None:
    caps = MockVendorAdapter().capabilities
    assert caps.native_async_tools is True
    assert caps.session_resumption is True
    assert caps.system_content_turn is False
    assert caps.mid_session_config_update is False
    assert (caps.input_sample_rate, caps.output_sample_rate) == (16000, 24000)


def test_build_setup_serializes_and_records() -> None:
    a = MockVendorAdapter()
    setup = SetupParam(
        model="mock-live",
        voice="alto",
        system_instruction="be nice",
        tools=(ToolSpec(name="get_balance", parameters={"type": "object"}, non_blocking=True),),
        response_modality=ResponseModality.TEXT,
    )
    msg = a.build_setup(setup)
    assert msg["model"] == "mock-live"
    assert msg["response_modality"] == "text"
    assert msg["tools"][0] == {
        "name": "get_balance",
        "description": "",
        "parameters": {"type": "object"},
        "non_blocking": True,
    }
    assert a.sent_setups == [msg]


def test_input_source_defaults_clean_and_serializes() -> None:
    a = MockVendorAdapter()
    assert SetupParam(model="m").input_source is InputSource.CLEAN
    msg = a.build_setup(SetupParam(model="m", input_source=InputSource.RAW))
    assert msg["input_source"] == "raw"


def test_self_denoise_capability_defaults_off() -> None:
    assert MockVendorAdapter().capabilities.self_denoise is False
    caps = VendorCapabilities(
        vendor="mock", model="m", backend=Backend.MOCK, self_denoise=True
    )
    assert MockVendorAdapter(capabilities=caps).capabilities.self_denoise is True


def test_system_item_downconverted_when_unsupported() -> None:
    a = MockVendorAdapter()  # system_content_turn=False
    out = a.serialize_item(Item(role=Role.SYSTEM, text="you are billing"))
    assert out["role"] == "user"
    assert out["_downconverted"] is True
    assert out["text"] == "[system] you are billing"


def test_system_item_kept_when_supported() -> None:
    caps = VendorCapabilities(
        vendor="mock",
        model="m",
        backend=Backend.MOCK,
        system_content_turn=True,
    )
    a = MockVendorAdapter(capabilities=caps)
    out = a.serialize_item(Item(role=Role.SYSTEM, text="sys"))
    assert out == {"role": "system", "text": "sys"}


def test_serialize_history_preserves_order_and_records() -> None:
    a = MockVendorAdapter()
    items = [
        Item(role=Role.USER, text="hi"),
        Item(role=Role.MODEL, text="hello"),
    ]
    out = a.serialize_history(items)
    assert [o["text"] for o in out] == ["hi", "hello"]
    assert len(a.sent_items) == 2


def test_serialize_tool_result_records() -> None:
    a = MockVendorAdapter()
    out = a.serialize_tool_result(call_id="c1", name="get_balance", content="42")
    assert out["call_id"] == "c1"
    assert out["content"] == "42"
    assert a.sent_tool_results == [out]


def test_parse_event_all_kinds() -> None:
    a = MockVendorAdapter()
    assert a.parse_event({"type": "user_transcript", "text": "hi", "final": True}) == [
        UserTranscript(text="hi", is_final=True)
    ]
    assert a.parse_event({"type": "agent_transcript", "text": "yo"}) == [
        AgentTranscript(text="yo", is_final=False)
    ]
    assert a.parse_event(
        {"type": "tool_call", "call_id": "c1", "name": "f", "args": {"x": 1}}
    ) == [ToolCallRequest(call_id="c1", name="f", args={"x": 1})]
    assert a.parse_event({"type": "turn_complete"}) == [TurnComplete()]
    assert a.parse_event({"type": "interrupted"}) == [Interrupted()]
    assert a.parse_event({"type": "go_away", "time_left_ms": 500}) == [
        GoAway(time_left_ms=500)
    ]
    assert a.parse_event({"type": "resumption", "handle": "h1"}) == [
        ResumptionUpdate(handle="h1")
    ]


def test_parse_event_unknown_is_ignored() -> None:
    assert MockVendorAdapter().parse_event({"type": "keepalive_ack"}) == []


def test_mock_realtime_and_turns_seams_recorded() -> None:
    from snail.vendor import MediaChunk, RealtimeControl

    a = MockVendorAdapter()
    a.serialize_realtime(MediaChunk.audio(b"pcm", sample_rate=16000))
    a.serialize_realtime(MediaChunk.image(b"img"))
    a.serialize_realtime_control(RealtimeControl.AUDIO_STREAM_END)
    a.serialize_turns([Item(role=Role.USER, text="hi")], complete=True)
    assert [m["kind"] for m in a.sent_realtime] == ["audio", "image"]
    assert a.sent_realtime[0]["sample_rate"] == 16000
    assert a.sent_controls == ["audio_stream_end"]
    assert a.sent_turns[0]["turn_complete"] is True


def test_setup_and_join_params_are_frozen() -> None:
    setup = SetupParam(model="m")
    try:
        setup.model = "other"  # type: ignore[misc]
    except (AttributeError, TypeError, msgspec.ValidationError):
        pass
    else:  # pragma: no cover
        raise AssertionError("SetupParam should be frozen")
