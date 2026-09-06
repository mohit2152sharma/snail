"""This example's Gemini adapter: the model owns endpointing.

The multi-agent example gives the *bridge* endpointing (an energy VAD sending manual
activity markers) because it is chasing TTFB. This example is about the consent
round-trip, where being cut off mid-question is the expensive failure — so Gemini's own
automatic activity detection runs instead, tuned tighter than the 800ms API default.

``manual_activity`` is left False (the base class default), which is what tells the
bridge to stream audio continuously and send no ``activity_start`` / ``activity_end``
markers — sending those into an auto-VAD session is a 1007 precondition failure that
kills the connection.
"""

from __future__ import annotations

from google.genai import types

from snail.vendor import GeminiAdapter


class GeminiVadAdapter(GeminiAdapter):
    """Automatic activity detection ON, with a configurable trailing-silence window."""

    def __init__(
        self,
        *,
        silence_duration_ms: int = 300,
        prefix_padding_ms: int = 300,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        #: How much trailing silence ends the turn. The dominant per-turn TTFB term,
        #: and the knob that decides whether a pause mid-sentence costs you the turn.
        self._silence_ms = silence_duration_ms
        #: Audio retained *before* detected speech onset, so the first syllable is not
        #: clipped off the front of the turn.
        self._prefix_ms = prefix_padding_ms

    def build_setup(self, setup, *, resumption_handle: str | None = None):
        cfg = super().build_setup(setup, resumption_handle=resumption_handle)
        cfg.realtime_input_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,  # explicitly ON — Gemini decides turn boundaries
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=self._prefix_ms,
                silence_duration_ms=self._silence_ms,
            )
        )
        return cfg
