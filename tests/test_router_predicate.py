"""Tests for the RulePolicy predicate DSL (docs 05)."""

from __future__ import annotations

from snail.router import (
    AgentRef,
    AgentRole,
    CallablePredicate,
    F,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
    SessionMeta,
    predicate_from_dict,
)


def _sig(**event_kw) -> RoutingSignal:
    return RoutingSignal(
        event=RoutingEvent(**event_kw),
        active_agent=AgentRef(id="main", spec_id="s1", role=AgentRole.ACTIVE),
        session_meta=SessionMeta(turn_count=25),
    )


def test_field_equality_and_enum_coercion() -> None:
    sig = _sig(kind=RoutingEventKind.TOOL_RESULT, status="escalate")
    assert (F("event.status") == "escalate").matches(sig)
    # enum field compared to raw string value
    assert (F("event.kind") == "tool_result").matches(sig)
    assert not (F("event.status") == "ok").matches(sig)


def test_ordering_and_missing_field() -> None:
    sig = _sig(kind=RoutingEventKind.USER_SPEECH_FINAL)
    assert (F("session_meta.turn_count") > 20).matches(sig)
    assert not (F("session_meta.turn_count") > 30).matches(sig)
    # missing field → ordering never matches
    assert not (F("event.duration_ms") > 5).matches(sig)


def test_regex_contains_in() -> None:
    sig = _sig(kind=RoutingEventKind.USER_SPEECH_FINAL, text="I want a refund now")
    assert F("event.text").matches_regex(r"refund|cancel").matches(sig)
    assert F("event.text").contains("refund").matches(sig)
    assert F("event.status").in_(["escalate", "blocked"]).matches(
        _sig(kind=RoutingEventKind.TOOL_RESULT, status="blocked")
    )


def test_boolean_composition() -> None:
    sig = _sig(kind=RoutingEventKind.TOOL_RESULT, status="error", retriable=False)
    p = (F("event.status") == "error") & (F("event.retriable") == False)  # noqa: E712
    assert p.matches(sig)
    assert (~p).matches(sig) is False
    assert ((F("event.status") == "nope") | p).matches(sig)


def test_nested_field_resolution() -> None:
    sig = _sig(kind=RoutingEventKind.PROGRAMMATIC)
    assert (F("active_agent.id") == "main").matches(sig)
    assert (F("active_agent.role") == "active").matches(sig)


def test_serialization_roundtrip() -> None:
    p = (F("event.status") == "escalate") & (F("session_meta.turn_count") >= 10)
    d = p.to_dict()
    assert d["op"] == "and"
    rebuilt = predicate_from_dict(d)
    sig = _sig(kind=RoutingEventKind.TOOL_RESULT, status="escalate")
    assert rebuilt.matches(sig)


def test_callable_escape_hatch() -> None:
    p = CallablePredicate(lambda s: s.session_meta.turn_count % 5 == 0)
    assert p.matches(_sig(kind=RoutingEventKind.PROGRAMMATIC))  # 25 % 5 == 0
