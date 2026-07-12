"""Canonical, vendor-neutral event + item schema (see docs 01-context-model).

The event log is the single source of truth. Events are audio-free transcripts;
audio never enters the log (docs 01/11). `Item` is the hard vendor-neutral boundary
that projections stop at — the `VendorAdapter` serializes `Item[]` to the wire, so
nothing above this layer ever holds a vendor payload.

Both structs are frozen: the log is append-only and projections must not mutate it.
"""

from __future__ import annotations

import enum

import msgspec


class EventType(enum.Enum):
    """Canonical log event kinds (docs 01)."""

    USER_SPEECH = "user_speech"
    AGENT_SPEECH = "agent_speech"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    EXTERNAL_CONTEXT = "external_context"
    HANDOFF = "handoff"


class Role(enum.Enum):
    """Vendor-neutral conversation roles.

    Gemini forbids ``system`` *content* turns (docs 07); down-converting ``SYSTEM``
    (e.g. folding it into ``system_instruction`` or a leading ``user`` turn) is the
    adapter's job, not the projection's. The neutral surface keeps the distinction.
    """

    USER = "user"
    MODEL = "model"
    SYSTEM = "system"
    TOOL = "tool"


class Event(msgspec.Struct, frozen=True, kw_only=True):
    """One append-only log entry. Slotted, flat, audio-free.

    This is also the replay/persist/debug format — one structure, many jobs (docs 01).
    ``seq`` is assigned by :class:`~snail.context.log.EventLog` on append.
    """

    seq: int
    ts: float
    type: EventType
    agent_id: str | None = None
    content: str = ""
    #: structured extras (tool args/status, handoff target, ...). Kept off the hot
    #: audio path — events are control/transcript only, so a dict here is fine.
    meta: dict | None = None


class Item(msgspec.Struct, frozen=True, kw_only=True):
    """Vendor-neutral conversation item — the HARD boundary (docs 01).

    A projection produces ``list[Item]``; the ``VendorAdapter`` serializes it. The
    moment anything above the adapter returns a vendor dict, vendor-neutrality dies.
    """

    role: Role
    text: str = ""
    #: tool name (for a TOOL result or a MODEL function-call item).
    name: str | None = None
    #: correlates a model function-call with its TOOL result.
    tool_call_id: str | None = None
    #: function-call arguments (MODEL → tool).
    args: dict | None = None
