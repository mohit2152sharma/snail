"""Tests for built-in routing policies + the default chain (docs 05, 09§E)."""

from __future__ import annotations

from snail.router import (
    ChainPolicy,
    ControlToolPolicy,
    F,
    ProgrammaticPolicy,
    RoutingAction,
    RoutingDecision,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
    Rule,
    RulePolicy,
    Seam,
    default_chain,
)


def _sig(**event_kw) -> RoutingSignal:
    return RoutingSignal(event=RoutingEvent(**event_kw))


def test_control_tool_policy() -> None:
    p = ControlToolPolicy()
    d = p.decide(_sig(kind=RoutingEventKind.TRANSFER_TO, target="billing"))
    assert d.action is RoutingAction.HANDOFF
    assert d.target == "billing"
    assert d.seam is Seam.AT_TURN_END
    assert p.decide(_sig(kind=RoutingEventKind.USER_SPEECH_FINAL)) is None


def test_programmatic_policy_fifo() -> None:
    p = ProgrammaticPolicy()
    assert p.decide(_sig(kind=RoutingEventKind.PROGRAMMATIC)) is None
    d1 = RoutingDecision(action=RoutingAction.HANDOFF, target="a")
    d2 = RoutingDecision(action=RoutingAction.HANDOFF, target="b")
    p.push(d1)
    p.push(d2)
    assert p.decide(_sig(kind=RoutingEventKind.PROGRAMMATIC)) is d1
    assert p.decide(_sig(kind=RoutingEventKind.PROGRAMMATIC)) is d2
    assert p.decide(_sig(kind=RoutingEventKind.PROGRAMMATIC)) is None


def test_rule_policy_first_match_wins() -> None:
    rules = RulePolicy(
        [
            Rule(
                when=F("event.status") == "escalate",
                then=RoutingDecision(action=RoutingAction.HANDOFF, target="human"),
            ),
            Rule(
                when=F("event.status") == "blocked",
                then=RoutingDecision(
                    action=RoutingAction.HANDOFF, target="security", seam=Seam.CUT_NOW
                ),
            ),
        ]
    )
    d = rules.decide(_sig(kind=RoutingEventKind.TOOL_RESULT, status="blocked"))
    assert d.target == "security" and d.seam is Seam.CUT_NOW
    assert rules.decide(_sig(kind=RoutingEventKind.TOOL_RESULT, status="ok")) is None


def test_chain_first_non_none() -> None:
    empty = RulePolicy()
    control = ControlToolPolicy()
    chain = ChainPolicy([empty, control])
    d = chain.decide(_sig(kind=RoutingEventKind.TRANSFER_TO, target="x"))
    assert d.target == "x"


def test_default_chain_programmatic_beats_control_tool() -> None:
    # 09§E chain-default-order: an explicit app decision must beat the model's transfer_to.
    prog = ProgrammaticPolicy()
    prog.push(RoutingDecision(action=RoutingAction.HANDOFF, target="app_choice"))
    chain = default_chain(programmatic=prog)
    # signal is ALSO a transfer_to (control tool would say "vendor_choice")
    d = chain.decide(_sig(kind=RoutingEventKind.TRANSFER_TO, target="vendor_choice"))
    assert d.target == "app_choice"  # programmatic wins
