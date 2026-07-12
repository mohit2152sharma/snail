"""VadGeminiAdapter — GeminiAdapter with server-side (Gemini) VAD enabled.

The package's ``GeminiAdapter.build_setup`` doesn't touch ``realtime_input_config``, so
this example subclasses it to switch on Gemini's automatic activity detection (VAD)
explicitly and tune it, without modifying the snail package. With automatic VAD on, the
model detects speech start/end itself — the client just streams audio; no manual
activity markers needed.
"""

from __future__ import annotations

from google.genai import types

from snail.vendor import GeminiAdapter


class VadGeminiAdapter(GeminiAdapter):
    """GeminiAdapter that enables Gemini's automatic VAD on every connection."""

    def build_setup(self, setup, *, resumption_handle: str | None = None):
        cfg = super().build_setup(setup, resumption_handle=resumption_handle)
        cfg.realtime_input_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,  # explicitly ON
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=300,
                silence_duration_ms=800,
            )
        )
        return cfg
