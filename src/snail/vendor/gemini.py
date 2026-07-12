"""GeminiAdapter — Gemini Live translation for both backends (see docs 07).

Pure translation (neutral ↔ ``google-genai`` types); the live socket lives in the
connection layer, which calls ``client.aio.live.connect(model=adapter.model,
config=adapter.build_setup(...))`` and feeds ``adapter.parse_event`` each
``LiveServerMessage``.

Backends (verified, docs 07):

* **Developer API** (``GEMINI_DEV``, ``api_key``): native ``NON_BLOCKING`` async tools,
  session resumption.
* **Vertex AI** (``GEMINI_VERTEX``, ADC / ``project``+``location``): async tools **not**
  wired (#1739) → the framework emulates; session resumption available.

Both target ``gemini-2.5-flash-live`` only. Schema uses ``parameters_json_schema`` so
our neutral lowercase JSON-schema passes through without hand-upcasing to Gemini's
OpenAPI subset.
"""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types

from snail.context import Item, Role

from .capabilities import Backend, VendorCapabilities
from .events import (
    AgentTranscript,
    GoAway,
    Interrupted,
    ParsedEvent,
    ResumptionUpdate,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
)
from .media import MediaChunk, MediaKind, RealtimeControl
from .params import ResponseModality, SetupParam

_MODALITY = {
    ResponseModality.AUDIO: types.Modality.AUDIO,
    ResponseModality.TEXT: types.Modality.TEXT,
}


def _parse_duration_ms(s: str | None) -> int | None:
    """Parse a Gemini duration string (e.g. ``"10s"``, ``"250ms"``) to milliseconds."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("ms"):
            return int(float(s[:-2]))
        if s.endswith("s"):
            return int(float(s[:-1]) * 1000)
        return int(float(s) * 1000)  # bare seconds
    except ValueError:
        return None


def gemini_capabilities(
    backend: Backend, model: str, *, self_denoise: bool = False
) -> VendorCapabilities:
    """Capability cells per Gemini backend (docs 07)."""
    if backend not in (Backend.GEMINI_DEV, Backend.GEMINI_VERTEX):
        raise ValueError(f"{backend} is not a Gemini backend")
    return VendorCapabilities(
        vendor="gemini",
        model=model,
        backend=backend,
        # NON_BLOCKING async tools are Dev-API only; Vertex emulates (#1739).
        native_async_tools=backend is Backend.GEMINI_DEV,
        session_resumption=True,
        system_content_turn=False,  # no role="system" content turns
        mid_session_config_update=False,  # forbidden → modality flip needs reconnect
        item_truncate=False,  # Gemini has no item.truncate
        self_denoise=self_denoise,
        input_sample_rate=16000,
        output_sample_rate=24000,
    )


class GeminiAdapter:
    """Translate the neutral surface to/from Gemini Live for one (model, backend)."""

    def __init__(
        self,
        *,
        backend: Backend = Backend.GEMINI_DEV,
        model: str = "gemini-2.5-flash-live",
        self_denoise: bool = False,
    ) -> None:
        self._backend = backend
        self._model = model
        self._caps = gemini_capabilities(backend, model, self_denoise=self_denoise)

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> VendorCapabilities:
        return self._caps

    # --- client factory (used by the connection layer) -------------------

    @staticmethod
    def build_client(
        backend: Backend,
        *,
        api_key: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> "genai.Client":
        """Create the SDK client for a backend.

        Dev API uses ``api_key``; Vertex uses ADC (``GOOGLE_APPLICATION_CREDENTIALS``)
        plus ``project``/``location``.
        """
        if backend is Backend.GEMINI_DEV:
            return genai.Client(api_key=api_key)
        if backend is Backend.GEMINI_VERTEX:
            return genai.Client(vertexai=True, project=project, location=location)
        raise ValueError(f"{backend} is not a Gemini backend")

    # --- setup / config ---------------------------------------------------

    def build_setup(
        self, setup: SetupParam, *, resumption_handle: str | None = None
    ) -> types.LiveConnectConfig:
        """Serialize the static identity to a ``LiveConnectConfig`` (docs 02/07)."""
        cfg = types.LiveConnectConfig(
            response_modalities=[_MODALITY[setup.response_modality]],
            # enable transcripts so the session can log user + agent turns.
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # enable resumption; pass a handle to resume (docs 02).
            session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
        )
        if setup.system_instruction:
            cfg.system_instruction = setup.system_instruction
        if setup.voice:
            cfg.speech_config = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=setup.voice
                    )
                )
            )
        if setup.tools:
            cfg.tools = [
                types.Tool(
                    function_declarations=[
                        self._function_declaration(t) for t in setup.tools
                    ]
                )
            ]
        return cfg

    def _function_declaration(self, spec) -> types.FunctionDeclaration:
        decl = types.FunctionDeclaration(
            name=spec.name,
            description=spec.description or None,
            parameters_json_schema=spec.parameters,
        )
        # NON_BLOCKING is Dev-API only; on Vertex leave BLOCKING and emulate (#1739).
        if spec.non_blocking and self._caps.native_async_tools:
            decl.behavior = types.Behavior.NON_BLOCKING
        return decl

    # --- items / history --------------------------------------------------

    def serialize_item(self, item: Item) -> types.Content:
        """Serialize one neutral ``Item`` to a Gemini ``Content`` turn."""
        # A model function-call item.
        if item.role is Role.MODEL and item.name and item.args is not None:
            return types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id=item.tool_call_id, name=item.name, args=item.args
                        )
                    )
                ],
            )
        # A tool result item.
        if item.role is Role.TOOL:
            return types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=item.tool_call_id,
                            name=item.name,
                            response={"result": item.text},
                        )
                    )
                ],
            )
        # SYSTEM content turns are forbidden on Gemini (docs 07) → fold into a user turn.
        if item.role is Role.SYSTEM:
            return types.Content(
                role="user", parts=[types.Part(text=f"[system] {item.text}")]
            )
        role = "model" if item.role is Role.MODEL else "user"
        return types.Content(role=role, parts=[types.Part(text=item.text)])

    def serialize_history(self, items: list[Item]) -> list[types.Content]:
        return [self.serialize_item(i) for i in items]

    # --- outbound seams (→ kwargs for the matching AsyncSession method) ---

    def serialize_realtime(self, chunk: MediaChunk) -> dict:
        """→ kwargs for ``session.send_realtime_input`` (streaming multimodal).

        Audio is assumed to be 16-bit PCM little-endian bytes already (the client sends
        LE); Gemini wants ``audio/pcm;rate=<rate>``.
        """
        if chunk.kind is MediaKind.AUDIO:
            rate = chunk.sample_rate or self._caps.input_sample_rate
            return {
                "audio": types.Blob(data=chunk.data, mime_type=f"audio/pcm;rate={rate}")
            }
        if chunk.kind is MediaKind.IMAGE:
            return {
                "media": types.Blob(
                    data=chunk.data, mime_type=chunk.mime_type or "image/jpeg"
                )
            }
        return {"text": chunk.text}  # MediaKind.TEXT

    def serialize_realtime_control(self, control: RealtimeControl) -> dict:
        """→ kwargs for ``session.send_realtime_input`` control markers."""
        if control is RealtimeControl.ACTIVITY_START:
            return {"activity_start": types.ActivityStart()}
        if control is RealtimeControl.ACTIVITY_END:
            return {"activity_end": types.ActivityEnd()}
        return {"audio_stream_end": True}  # AUDIO_STREAM_END

    def serialize_turns(self, items: list[Item], *, complete: bool) -> dict:
        """→ kwargs for ``session.send_client_content`` (ordered turns)."""
        return {
            "turns": [self.serialize_item(i) for i in items],
            "turn_complete": complete,
        }

    def serialize_tool_result(
        self, *, call_id: str, name: str, content: str, meta: dict | None = None
    ) -> types.FunctionResponse:
        """Serialize a tool result to a Gemini ``FunctionResponse``.

        The connection layer sends it via ``session.send_tool_response(...)``.
        """
        return types.FunctionResponse(
            id=call_id, name=name, response={"result": content}
        )

    # --- inbound event parsing -------------------------------------------

    def parse_event(self, msg: Any) -> list[ParsedEvent]:
        """Parse one ``LiveServerMessage`` into neutral events.

        Audio is **not** returned here — the connection layer extracts it with
        :meth:`extract_output_audio`; this handles the control/transcript/tool plane.
        """
        out: list[ParsedEvent] = []
        sc = getattr(msg, "server_content", None)
        if sc is not None:
            it = getattr(sc, "input_transcription", None)
            if it is not None and it.text:
                out.append(
                    UserTranscript(text=it.text, is_final=bool(it.finished))
                )
            ot = getattr(sc, "output_transcription", None)
            if ot is not None and ot.text:
                out.append(
                    AgentTranscript(text=ot.text, is_final=bool(ot.finished))
                )
            if getattr(sc, "interrupted", None):
                out.append(Interrupted())
            if getattr(sc, "turn_complete", None):
                out.append(TurnComplete())

        tc = getattr(msg, "tool_call", None)
        if tc is not None and tc.function_calls:
            for fc in tc.function_calls:
                out.append(
                    ToolCallRequest(
                        call_id=fc.id or "", name=fc.name or "", args=fc.args or {}
                    )
                )

        ga = getattr(msg, "go_away", None)
        if ga is not None:
            out.append(GoAway(time_left_ms=_parse_duration_ms(ga.time_left)))

        sru = getattr(msg, "session_resumption_update", None)
        if sru is not None and sru.resumable and sru.new_handle:
            out.append(ResumptionUpdate(handle=sru.new_handle))

        return out

    def extract_output_audio(self, msg: Any) -> bytes | None:
        """Extract agent audio (PCM bytes) from a message's model turn, if any."""
        sc = getattr(msg, "server_content", None)
        if sc is None or sc.model_turn is None:
            return None
        chunks = [
            p.inline_data.data
            for p in (sc.model_turn.parts or [])
            if p.inline_data is not None and p.inline_data.data
        ]
        return b"".join(chunks) if chunks else None
