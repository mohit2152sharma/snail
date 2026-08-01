"""Translate snail neutral events → the frontend's timeline JSON schema.

Three sources feed the client, and they are kept disjoint on purpose:

* **the vendor stream** — transcripts, tool calls, turn/interrupt/goaway. Straight
  translation in :func:`to_client_json`.
* **the event log** — tool *results* and run transitions (docs 14). These never appear
  as vendor events: the framework produces them, so :func:`from_log_event` reads them
  off the shared :class:`~snail.context.EventLog` instead. Only the two types the
  vendor stream cannot supply are mapped, so nothing is emitted twice.
* **the bridge's own instrumentation** — setup stages, VAD speech boundaries, per-turn
  TTFB. Built here, emitted by the bridge at the point it measures them.
"""

from __future__ import annotations

import time

from snail.context import Event, EventType
from snail.vendor import (
    AgentTranscript,
    GoAway,
    Interrupted,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
)


def _ts() -> int:
    return int(time.time() * 1000)


def to_client_json(ev, *, agent_id: str) -> dict | None:
    """Map one neutral ParsedEvent to a client event dict, or None to skip."""
    if isinstance(ev, UserTranscript):
        return {"type": "user_transcript", "text": ev.text, "is_final": ev.is_final, "ts": _ts()}
    if isinstance(ev, AgentTranscript):
        return {
            "type": "agent_transcript",
            "agent_id": agent_id,
            "text": ev.text,
            "is_final": ev.is_final,
            "ts": _ts(),
        }
    if isinstance(ev, ToolCallRequest):
        return {
            "type": "tool_call",
            "agent_id": agent_id,
            "tool_name": ev.name,
            "call_id": ev.call_id,
            "args": ev.args,
            "ts": _ts(),
        }
    if isinstance(ev, TurnComplete):
        return {"type": "turn_complete", "agent_id": agent_id, "ts": _ts()}
    if isinstance(ev, Interrupted):
        return {"type": "interrupted", "agent_id": agent_id, "ts": _ts()}
    if isinstance(ev, GoAway):
        return {"type": "go_away", "time_left_ms": ev.time_left_ms, "ts": _ts()}
    # ResumptionUpdate / VendorError are handled elsewhere. UserSpeechStart /
    # UserSpeechEnd are emitted by the bridge itself, which has to stamp the TTFB clock
    # at the same instant and so cannot route them through here.
    return None


#: Log types the vendor stream cannot supply. TOOL_CALL / speech are excluded because
#: they already reached the client from the parsed stream (see module docstring).
_LOG_TYPES = frozenset({EventType.TOOL_RESULT, EventType.TOOL_RUN})


def from_log_event(e: Event) -> dict | None:
    """Map one :class:`~snail.context.Event` to a client event, or None to skip."""
    if e.type not in _LOG_TYPES:
        return None
    meta = dict(e.meta or {})
    if e.type is EventType.TOOL_RESULT:
        return {
            "type": "tool_result",
            "agent_id": e.agent_id,
            "tool_name": meta.get("tool_name"),
            "call_id": meta.get("tool_call_id"),
            "status": meta.get("status"),
            "content": e.content,
            "ts": int(e.ts * 1000),
        }
    # TOOL_RUN: the run-level state machine — started / blocked / submit / displaced /
    # expired / finished. Control-only for the model, but the whole story for a human.
    return {"type": "tool_run", "agent_id": e.agent_id, "ts": int(e.ts * 1000), **meta}


# --- bridge instrumentation ------------------------------------------------


def active_agent_changed(agent_id: str) -> dict:
    return {"type": "active_agent_changed", "agent_id": agent_id, "ts": _ts()}


def setup_stage(stage: str, ms: float, *, agent_id: str | None = None, detail: str = "") -> dict:
    """One measured step of bringing the session up (per-agent vendor connect, etc.)."""
    return {
        "type": "setup_stage",
        "stage": stage,
        "agent_id": agent_id,
        "ms": round(ms, 1),
        "detail": detail,
        "ts": _ts(),
    }


def setup_complete(total_ms: float, agents: list[str]) -> dict:
    return {
        "type": "setup_complete",
        "total_ms": round(total_ms, 1),
        "agents": agents,
        "ts": _ts(),
    }


def speech(kind: str) -> dict:
    """VAD boundary: ``speech_start`` / ``speech_end``. ``speech_end`` starts the TTFB clock."""
    return {"type": kind, "ts": _ts()}


def ttfb(agent_id: str, ms: float) -> dict:
    """End-of-speech → first audio byte, measured server-side (the target metric)."""
    return {"type": "ttfb", "agent_id": agent_id, "ms": round(ms, 1), "ts": _ts()}


def error(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message, "ts": _ts()}
