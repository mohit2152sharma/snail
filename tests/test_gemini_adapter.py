"""Tests for GeminiAdapter translation (docs 07). No network — SDK objects only."""

from __future__ import annotations

from google.genai import types

from snail.context import Item, Role
from snail.vendor import (
    MIN_SILENCE_DURATION_MS,
    AgentTranscript,
    Backend,
    GeminiAdapter,
    GoAway,
    Interrupted,
    MediaChunk,
    MediaKind,
    RealtimeControl,
    ResponseModality,
    ResumptionUpdate,
    SetupParam,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    TurnDetectionParam,
    UserTranscript,
    VendorAdapter,
)
from snail.vendor.gemini import _parse_duration_ms, clamp_silence_ms


def _dev() -> GeminiAdapter:
    return GeminiAdapter(backend=Backend.GEMINI_DEV)


def _vertex() -> GeminiAdapter:
    return GeminiAdapter(backend=Backend.GEMINI_VERTEX)


def test_satisfies_adapter_protocol() -> None:
    assert isinstance(_dev(), VendorAdapter)
    assert _dev().name == "gemini"


def test_capabilities_differ_by_backend() -> None:
    dev, vertex = _dev().capabilities, _vertex().capabilities
    assert dev.native_async_tools is True  # Dev API native NON_BLOCKING
    assert vertex.native_async_tools is False  # Vertex emulates (#1739)
    assert dev.session_resumption is vertex.session_resumption is True
    assert dev.system_content_turn is False
    assert dev.mid_session_config_update is False
    assert (dev.input_sample_rate, dev.output_sample_rate) == (16000, 24000)


def test_build_setup_modality_and_transcription() -> None:
    cfg = _dev().build_setup(
        SetupParam(model="gemini-2.5-flash-live", response_modality=ResponseModality.TEXT)
    )
    assert cfg.response_modalities == [types.Modality.TEXT]
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None
    assert cfg.session_resumption is not None  # resumption enabled


def test_build_setup_voice_and_system_instruction() -> None:
    cfg = _dev().build_setup(
        SetupParam(model="m", voice="Puck", system_instruction="be nice")
    )
    assert cfg.system_instruction == "be nice"
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"


def test_build_setup_defaults_to_floored_server_vad() -> None:
    """Per-turn TTFB: server-VAD is on by default, at the 500ms floor — no example hack."""
    cfg = _dev().build_setup(SetupParam(model="m"))
    vad = cfg.realtime_input_config.automatic_activity_detection
    assert vad.disabled is False
    assert vad.silence_duration_ms == MIN_SILENCE_DURATION_MS == 500


def test_build_setup_honors_a_higher_configured_silence() -> None:
    cfg = _dev().build_setup(
        SetupParam(model="m", turn_detection=TurnDetectionParam(silence_duration_ms=900))
    )
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 900


def test_build_setup_clamps_a_too_low_configured_silence() -> None:
    """Never below the floor, whatever a caller (mis)configures — a correctness floor."""
    cfg = _dev().build_setup(
        SetupParam(model="m", turn_detection=TurnDetectionParam(silence_duration_ms=100))
    )
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 500


def test_clamp_silence_ms_helper() -> None:
    assert clamp_silence_ms(100) == 500
    assert clamp_silence_ms(500) == 500
    assert clamp_silence_ms(900) == 900


def test_tool_behavior_native_on_dev_only() -> None:
    spec = ToolSpec(
        name="get_balance",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        non_blocking=True,
    )
    setup = SetupParam(model="m", tools=(spec,))
    dev_decl = _dev().build_setup(setup).tools[0].function_declarations[0]
    vtx_decl = _vertex().build_setup(setup).tools[0].function_declarations[0]
    assert dev_decl.behavior is types.Behavior.NON_BLOCKING
    assert vtx_decl.behavior is None  # BLOCKING default on Vertex
    # neutral lowercase JSON schema passes through untouched
    assert dev_decl.parameters_json_schema == spec.parameters


def test_serialize_items() -> None:
    a = _dev()
    user = a.serialize_item(Item(role=Role.USER, text="hi"))
    assert user.role == "user" and user.parts[0].text == "hi"
    model = a.serialize_item(Item(role=Role.MODEL, text="hello"))
    assert model.role == "model"
    # SYSTEM folds into a user turn (no system content turns on Gemini)
    sysc = a.serialize_item(Item(role=Role.SYSTEM, text="be brief"))
    assert sysc.role == "user" and sysc.parts[0].text == "[system] be brief"
    # model function call
    call = a.serialize_item(
        Item(role=Role.MODEL, name="f", tool_call_id="c1", args={"x": 1})
    )
    assert call.parts[0].function_call.name == "f"
    assert call.parts[0].function_call.id == "c1"
    # tool result
    res = a.serialize_item(Item(role=Role.TOOL, text="42", name="f", tool_call_id="c1"))
    assert res.role == "tool"
    assert res.parts[0].function_response.response == {"result": "42"}


def test_serialize_tool_result() -> None:
    fr = _dev().serialize_tool_result(call_id="c1", name="f", content="ok")
    assert fr.id == "c1" and fr.name == "f"
    assert fr.response == {"result": "ok"}


def test_serialize_realtime_multimodal() -> None:
    a = _dev()
    audio = a.serialize_realtime(MediaChunk.audio(b"pcm", sample_rate=16000))
    assert audio["audio"].mime_type == "audio/pcm;rate=16000"
    assert audio["audio"].data == b"pcm"
    img = a.serialize_realtime(MediaChunk.image(b"jpg", mime_type="image/png"))
    assert img["media"].mime_type == "image/png"
    txt = a.serialize_realtime(MediaChunk.text_("hi"))
    assert txt == {"text": "hi"}


def test_serialize_realtime_audio_defaults_rate_to_capabilities() -> None:
    a = _dev()  # input_sample_rate = 16000
    out = a.serialize_realtime(MediaChunk(kind=MediaKind.AUDIO, data=b"x"))
    assert out["audio"].mime_type == "audio/pcm;rate=16000"


def test_serialize_realtime_control() -> None:
    a = _dev()
    assert "activity_start" in a.serialize_realtime_control(RealtimeControl.ACTIVITY_START)
    assert a.serialize_realtime_control(RealtimeControl.AUDIO_STREAM_END) == {
        "audio_stream_end": True
    }


def test_serialize_turns() -> None:
    a = _dev()
    kw = a.serialize_turns(
        [Item(role=Role.USER, text="hi"), Item(role=Role.MODEL, text="yo")],
        complete=False,
    )
    assert kw["turn_complete"] is False
    assert [c.role for c in kw["turns"]] == ["user", "model"]


def test_parse_event_transcripts_and_control() -> None:
    a = _dev()
    msg = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="hello", finished=True),
            output_transcription=types.Transcription(text="hi", finished=False),
            interrupted=True,
            turn_complete=True,
        )
    )
    evs = a.parse_event(msg)
    assert UserTranscript(text="hello", is_final=True) in evs
    assert AgentTranscript(text="hi", is_final=False) in evs
    assert any(isinstance(e, Interrupted) for e in evs)
    assert any(isinstance(e, TurnComplete) for e in evs)


def test_parse_event_tool_call_goaway_resumption() -> None:
    a = _dev()
    tc = types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id="c1", name="f", args={"x": 1})]
        )
    )
    assert a.parse_event(tc) == [ToolCallRequest(call_id="c1", name="f", args={"x": 1})]

    ga = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s"))
    assert a.parse_event(ga) == [GoAway(time_left_ms=5000)]

    sru = types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle="h1", resumable=True
        )
    )
    assert a.parse_event(sru) == [ResumptionUpdate(handle="h1")]


def test_extract_output_audio() -> None:
    a = _dev()
    msg = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(data=b"abc", mime_type="audio/pcm")
                    ),
                    types.Part(text="ignored"),
                ]
            )
        )
    )
    assert a.extract_output_audio(msg) == b"abc"
    assert a.extract_output_audio(types.LiveServerMessage()) is None


def test_parse_duration_ms_units() -> None:
    assert _parse_duration_ms("5s") == 5000
    assert _parse_duration_ms("250ms") == 250
    assert _parse_duration_ms("2") == 2000
    assert _parse_duration_ms(None) is None
    assert _parse_duration_ms("weird") is None


def test_build_client_dev() -> None:
    client = GeminiAdapter.build_client(Backend.GEMINI_DEV, api_key="dummy")
    assert client is not None
