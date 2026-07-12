"""Translate snail neutral events → the frontend's timeline JSON schema.

The frontend (examples/frontend) renders 9 event types; only a subset comes straight
from the vendor stream (transcripts, tool calls, turn/interrupt/goaway). The rest
(active-agent changes, tool results, errors) are emitted by the bridge from its own
hooks via the helper builders below.
"""

from __future__ import annotations

import time

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
        return {"type": "turn_complete", "ts": _ts()}
    if isinstance(ev, Interrupted):
        return {"type": "interrupted", "ts": _ts()}
    if isinstance(ev, GoAway):
        return {"type": "go_away", "time_left_ms": ev.time_left_ms, "ts": _ts()}
    return None  # ResumptionUpdate, VendorError handled elsewhere / skipped


def active_agent_changed(agent_id: str) -> dict:
    return {"type": "active_agent_changed", "agent_id": agent_id, "ts": _ts()}


def tool_result(*, agent_id: str, tool_name: str, call_id: str, status: str, content: str) -> dict:
    return {
        "type": "tool_result",
        "agent_id": agent_id,
        "tool_name": tool_name,
        "call_id": call_id,
        "status": status,
        "content": content,
        "ts": _ts(),
    }


def error(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message, "ts": _ts()}
