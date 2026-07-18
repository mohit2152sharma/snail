"""TranslateGeminiAdapter — GeminiAdapter for Gemini 3.5 Live Translate.

Server-side (Gemini) VAD with a floored, tunable end-of-speech silence is now a base
``GeminiAdapter`` default (``SetupParam.turn_detection``, see ``snail.vendor.params`` /
``snail.vendor.gemini``) — host + echo (``agents.py``) get it for free via plain
``GeminiAdapter``. Only the translate model needs a subclass, because it forbids
tools/system-instruction and takes a ``translation_config`` instead.
"""

from __future__ import annotations

from google.genai import types

from snail.vendor import GeminiAdapter, realtime_input_config


class TranslateGeminiAdapter(GeminiAdapter):
    """GeminiAdapter for Gemini 3.5 Live Translate.

    Translation mode forbids tools + system instructions and instead takes a
    ``translation_config`` (target language). We build a **minimal** LiveConnectConfig
    from scratch (not the base ``build_setup``, which would attach tools / instruction /
    resumption the translate model rejects): response modality + transcriptions (for the
    timeline) + the translation target + the same floored server-VAD tuning as every
    other agent (``setup.turn_detection`` — TTFB matters here too).
    """

    def __init__(self, *, target_language_code: str = "hi", **kwargs) -> None:
        super().__init__(**kwargs)
        self._target = target_language_code

    def build_setup(self, setup, *, resumption_handle: str | None = None):
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=self._target,
                echo_target_language=False,
            ),
            realtime_input_config=realtime_input_config(setup.turn_detection),
        )
