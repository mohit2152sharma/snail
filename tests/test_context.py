"""Tests for the context model (docs 01)."""

from __future__ import annotations

import msgspec
import pytest

from snail.context import (
    Event,
    EventLog,
    EventType,
    Item,
    Projection,
    Role,
)


def test_append_assigns_monotonic_seq() -> None:
    log = EventLog()
    e0 = log.append(EventType.USER_SPEECH, content="hi")
    e1 = log.append(EventType.AGENT_SPEECH, content="hello", agent_id="a")
    assert (e0.seq, e1.seq) == (0, 1)
    assert len(log) == 2
    assert [e.seq for e in log] == [0, 1]


def test_events_are_frozen() -> None:
    log = EventLog()
    e = log.append(EventType.USER_SPEECH, content="hi")
    with pytest.raises((AttributeError, msgspec.ValidationError, TypeError)):
        e.content = "mutated"  # type: ignore[misc]


def test_filter_by_type_and_agent() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="u")
    log.append(EventType.AGENT_SPEECH, content="a1", agent_id="a")
    log.append(EventType.AGENT_SPEECH, content="b1", agent_id="b")

    by_type = list(log.filter(types=[EventType.AGENT_SPEECH]))
    assert [e.content for e in by_type] == ["a1", "b1"]

    by_agent = list(log.filter(agents=["a"]))
    assert [e.content for e in by_agent] == ["a1"]

    both = list(log.filter(types=[EventType.AGENT_SPEECH], agents=["b"]))
    assert [e.content for e in both] == ["b1"]


def test_projection_basic_roles_and_instructions() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="hello")
    log.append(EventType.AGENT_SPEECH, content="hi there", agent_id="main")

    items = log.project(Projection(instructions="Be concise."))
    assert items[0] == Item(role=Role.SYSTEM, text="Be concise.")
    assert items[1] == Item(role=Role.USER, text="hello")
    assert items[2] == Item(role=Role.MODEL, text="hi there")


def test_projection_last_n_and_extra_ordering() -> None:
    log = EventLog()
    for i in range(5):
        log.append(EventType.USER_SPEECH, content=str(i))
    extra = (Item(role=Role.SYSTEM, text="account doc"),)
    items = log.project(Projection(last_n=2, extra=extra))
    # last_n keeps the last two user turns; extra is appended after.
    assert [i.text for i in items] == ["3", "4", "account doc"]


def test_projection_include_and_agents_filter() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="u")
    log.append(EventType.AGENT_SPEECH, content="main-says", agent_id="main")
    log.append(EventType.AGENT_SPEECH, content="other-says", agent_id="other")

    proj = Projection(
        include=frozenset({EventType.AGENT_SPEECH}),
        agents=("main",),
    )
    items = log.project(proj)
    assert [i.text for i in items] == ["main-says"]
    assert all(i.role is Role.MODEL for i in items)


def test_projection_tool_call_and_result_mapping() -> None:
    log = EventLog()
    log.append(
        EventType.TOOL_CALL,
        agent_id="main",
        meta={"tool_name": "get_balance", "tool_call_id": "c1", "args": {"acct": 7}},
    )
    log.append(
        EventType.TOOL_RESULT,
        content="balance=42",
        meta={"tool_name": "get_balance", "tool_call_id": "c1"},
    )
    items = log.project(Projection())
    call, result = items
    assert call.role is Role.MODEL
    assert call.name == "get_balance"
    assert call.tool_call_id == "c1"
    assert call.args == {"acct": 7}
    assert result.role is Role.TOOL
    assert result.text == "balance=42"
    assert result.tool_call_id == "c1"


def test_projection_excludes_handoff_control_events() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="u")
    log.append(EventType.HANDOFF, meta={"target": "billing"})
    items = log.project(Projection())
    assert [i.text for i in items] == ["u"]  # handoff not a conversation turn


def test_mode2_builder_callable() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="refund please")

    def builder(lg: EventLog) -> list[Item]:
        out = [Item(role=Role.SYSTEM, text="custom")]
        out += [
            Item(role=Role.USER, text=e.content.upper())
            for e in lg.filter(types=[EventType.USER_SPEECH])
        ]
        return out

    items = log.project(builder)
    assert items == [
        Item(role=Role.SYSTEM, text="custom"),
        Item(role=Role.USER, text="REFUND PLEASE"),
    ]


def test_events_snapshot_is_immutable_tuple() -> None:
    log = EventLog()
    log.append(EventType.USER_SPEECH, content="u")
    snap = log.events
    assert isinstance(snap, tuple)
    log.append(EventType.USER_SPEECH, content="v")
    # snapshot taken earlier is unaffected by later appends
    assert len(snap) == 1
    assert isinstance(snap[0], Event)
