"""Stand-ins for the camera, the microphone and the clock.

``goal.md``: "We are not really going to provide any images or any audio, so just mock
those inputs." Everything here is deterministic and side-effect free, so the example
runs anywhere and the transcript is reproducible.
"""

from __future__ import annotations

from datetime import datetime

#: What the mock camera "sees". Change it to change the answer the agent gives.
SIGN_TEXT = "ZUTRITT VERBOTEN"
SIGN_MEANING = "NO ENTRY"


def capture_photo() -> str:
    """Pretend to open the camera and grab a frame. Returns an opaque handle."""
    return "photo://mock/frame-0001"


def describe_photo(photo: str, question: str) -> str:
    """Pretend to send ``photo`` + ``question`` to a vision model.

    A real implementation posts the frame to Gemini and returns its answer. The mock
    always sees the same sign, so the example has one stable thing to talk about.
    """
    return (
        f"The sign reads '{SIGN_TEXT}', which in English means '{SIGN_MEANING}'. "
        f"(mock vision answer for: {question})"
    )


def start_microphone() -> str:
    """Pretend to turn the mic on. Returns the device the recording is running on."""
    return "mic://mock/default"


def now_local() -> datetime:
    return datetime.now()
