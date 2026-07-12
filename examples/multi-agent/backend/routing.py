"""Handoff policy for the host+echo example.

Tool-result driven: when the host's ``start_echo`` tool resolves successfully, hand off
to the echo agent; when the echo's ``stop`` resolves, hand back to the host. The seam is
``AT_TURN_END`` so the current agent finishes its sentence before the switch.

Built on the shipped ``default_chain`` (Programmatic → ControlTool → Rule): the returned
``ProgrammaticPolicy`` lets the app force a handoff (the UI's manual "hand off" button)
ahead of the model, while the rules below are the tool-driven default.
"""

from __future__ import annotations

from snail.router import (
    ChainPolicy,
    ProgrammaticPolicy,
    Rule,
    RoutingAction,
    RoutingDecision,
    RoutingEventKind,
    RulePolicy,
    Seam,
    default_chain,
)
from snail.router.predicate import F

from .agents import ECHO_ID, HOST_ID, TRANSLATE_ID


def _tool_success(tool_name: str):
    return (
        (F("event.kind") == RoutingEventKind.TOOL_RESULT)
        & (F("event.tool_name") == tool_name)
        & (F("event.status") == "success")
    )


def build_policy() -> tuple[ChainPolicy, ProgrammaticPolicy]:
    """Return the routing chain and the programmatic hook for manual handoffs."""
    rules = RulePolicy(
        [
            Rule(
                when=_tool_success("start_echo"),
                then=RoutingDecision(
                    action=RoutingAction.HANDOFF, target=ECHO_ID, seam=Seam.AT_TURN_END
                ),
            ),
            Rule(
                when=_tool_success("start_translation"),
                then=RoutingDecision(
                    action=RoutingAction.HANDOFF, target=TRANSLATE_ID, seam=Seam.AT_TURN_END
                ),
            ),
            Rule(
                when=_tool_success("stop"),
                then=RoutingDecision(
                    action=RoutingAction.HANDOFF, target=HOST_ID, seam=Seam.AT_TURN_END
                ),
            ),
        ]
    )
    programmatic = ProgrammaticPolicy()
    chain = default_chain(programmatic=programmatic, rules=rules)
    return chain, programmatic
