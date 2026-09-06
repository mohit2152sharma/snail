"""EnergyVad — energy-based speech endpointing with hangover (server-side VAD).

A pure, I/O-free state machine that classifies one interior audio frame at a time
(480 samples, int16, mono, 48kHz) as speech or silence and emits turn boundaries.
Unlike Gemini's flat trailing-silence timer, the **hangover** holds a turn open through
brief mid-utterance pauses (< ``hangover_frames``), so end-of-speech can be declared
quickly (~300ms) without cutting the user off — the anti-barge-in property.

Threshold is an **adaptive noise floor**: an EMA of frame RMS updated only while in
``SILENCE`` (so it tracks room tone but never ramps up to the speaker's own voice). A
frame is "voiced" when ``rms > floor * margin``. A short warm-up seeds the floor and
suppresses spurious starts. State: ``SILENCE ⇄ SPEECH``; one transition per ``push``.
"""

from __future__ import annotations

import enum

import numpy as np

FRAME_LEN = 480  # 10ms @ 48kHz mono (matches audio interior)


class VadState(enum.Enum):
    SILENCE = "silence"
    SPEECH = "speech"


class VadEvent(enum.Enum):
    NONE = "none"
    START = "start"
    END = "end"


class EnergyVad:
    """Adaptive-threshold energy VAD with start-debounce + hangover endpointing."""

    __slots__ = (
        "_frame", "_start_frames", "_hangover", "_margin", "_alpha", "_warmup",
        "_floor", "_seen", "_state", "_above", "_below", "_starts", "_ends",
    )

    def __init__(
        self,
        *,
        frame_size: int = FRAME_LEN,
        start_frames: int = 3,
        hangover_frames: int = 30,
        margin: float = 3.0,
        alpha: float = 0.05,
        warmup_frames: int = 10,
    ) -> None:
        if frame_size < 1:
            raise ValueError("frame_size must be >= 1")
        self._frame = frame_size
        self._start_frames = max(1, start_frames)
        self._hangover = max(1, hangover_frames)
        self._margin = margin
        self._alpha = alpha
        self._warmup = warmup_frames
        self._floor = 0.0
        self._seen = 0
        self._state = VadState.SILENCE
        self._above = 0
        self._below = 0
        self._starts = 0
        self._ends = 0

    def push(self, frame: np.ndarray) -> VadEvent:
        """Classify one frame → transition. Call once per interior frame in order."""
        rms = self._rms(frame)
        self._seen += 1
        warming = self._seen <= self._warmup
        # Seed on first frame; afterwards track room tone only while not speaking.
        if self._floor == 0.0:
            self._floor = rms
        elif self._state is VadState.SILENCE:
            self._floor = (1.0 - self._alpha) * self._floor + self._alpha * rms
        voiced = (not warming) and rms > self._floor * self._margin

        if self._state is VadState.SILENCE:
            if voiced:
                self._above += 1
                if self._above >= self._start_frames:
                    self._state = VadState.SPEECH
                    self._above = 0
                    self._below = 0
                    self._starts += 1
                    return VadEvent.START
            else:
                self._above = 0
            return VadEvent.NONE

        # SPEECH
        if not voiced:
            self._below += 1
            if self._below >= self._hangover:
                self._state = VadState.SILENCE
                self._below = 0
                self._above = 0
                self._ends += 1
                return VadEvent.END
        else:
            self._below = 0
        return VadEvent.NONE

    def reset(self) -> None:
        """Drop any in-flight speech back to SILENCE (keeps the floor estimate)."""
        self._state = VadState.SILENCE
        self._above = 0
        self._below = 0

    @property
    def state(self) -> VadState:
        return self._state

    @property
    def stats(self) -> dict:
        return {
            "state": self._state.value,
            "floor": self._floor,
            "starts": self._starts,
            "ends": self._ends,
        }

    @staticmethod
    def _rms(frame: np.ndarray) -> float:
        if frame.size == 0:
            return 0.0
        f = frame.astype(np.float32, copy=False)
        return float(np.sqrt(np.mean(f * f, dtype=np.float64)))
