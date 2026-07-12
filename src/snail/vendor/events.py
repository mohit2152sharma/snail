"""Parsed vendor events — the neutral signals an adapter emits (see docs 05/07).

``VendorAdapter.parse_event`` turns raw vendor wire messages into these vendor-neutral
signals. The session layer folds transcripts into the event log; the Router consumes
tool calls, interrupts (barge-in), and deadline signals. Consumers match by type.
"""

from __future__ import annotations

import msgspec


class UserTranscript(msgspec.Struct, frozen=True, kw_only=True):
    """Vendor-supplied transcript of user speech (async, may be partial/late)."""

    text: str
    is_final: bool = False


class AgentTranscript(msgspec.Struct, frozen=True, kw_only=True):
    """Transcript of the agent's own output."""

    text: str
    is_final: bool = False


class ToolCallRequest(msgspec.Struct, frozen=True, kw_only=True):
    """The model asked to call a tool (intent, not command — docs 03)."""

    call_id: str
    name: str
    args: dict = {}


class TurnComplete(msgspec.Struct, frozen=True, kw_only=True):
    """The agent finished its turn (a natural seam boundary)."""


class Interrupted(msgspec.Struct, frozen=True, kw_only=True):
    """Vendor server-VAD detected user speech over agent output → barge-in.

    In v1 this is what triggers the ``CUT_NOW`` seam (local VAD deferred — docs 11).
    """


class GoAway(msgspec.Struct, frozen=True, kw_only=True):
    """Vendor is about to terminate the session (Gemini). Recycle now."""

    time_left_ms: int | None = None


class ResumptionUpdate(msgspec.Struct, frozen=True, kw_only=True):
    """New session-resumption handle (Gemini) to survive a recycle."""

    handle: str


class VendorError(msgspec.Struct, frozen=True, kw_only=True):
    """A vendor-reported error."""

    code: str = ""
    message: str = ""


#: Everything an adapter can surface from the wire.
ParsedEvent = (
    UserTranscript
    | AgentTranscript
    | ToolCallRequest
    | TurnComplete
    | Interrupted
    | GoAway
    | ResumptionUpdate
    | VendorError
)
