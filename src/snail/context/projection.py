"""Declarative projection spec — Mode 1 (see docs 01-context-model).

A projection is a filter/transform over the log producing a vendor-neutral
``list[Item]``. Mode 1 (this file) is the safe, cacheable, declarative default that
covers ~90% of cases; Mode 2 is any ``ProjectionBuilder`` callable (see
:mod:`snail.context.log`). Both stop at ``Item[]`` — the adapter serializes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

from .events import Event, EventType, Item, Role

if TYPE_CHECKING:
    from .log import EventLog


def _event_to_item(event: Event) -> Item | None:
    """Map one log event to its neutral conversation item.

    Returns ``None`` for events that are not conversation turns (e.g. ``handoff`` is
    a control event, not something a vendor should see as context).
    """
    t = event.type
    if t is EventType.USER_SPEECH:
        return Item(role=Role.USER, text=event.content)
    if t is EventType.AGENT_SPEECH:
        return Item(role=Role.MODEL, text=event.content)
    if t is EventType.EXTERNAL_CONTEXT:
        # Neutral SYSTEM item; the adapter down-converts for vendors that forbid
        # system content turns (Gemini — docs 07).
        return Item(role=Role.SYSTEM, text=event.content)
    if t is EventType.TOOL_CALL:
        meta = event.meta or {}
        return Item(
            role=Role.MODEL,
            name=meta.get("tool_name"),
            tool_call_id=meta.get("tool_call_id"),
            args=meta.get("args"),
        )
    if t is EventType.TOOL_RESULT:
        meta = event.meta or {}
        return Item(
            role=Role.TOOL,
            text=event.content,
            name=meta.get("tool_name"),
            tool_call_id=meta.get("tool_call_id"),
        )
    # HANDOFF and any future control-only events: not conversation context.
    return None


class Projection(msgspec.Struct, frozen=True, kw_only=True):
    """A declarative filter/transform over the log → ``Item[]`` (Mode 1).

    Safe, declarative, cacheable, vendor-neutral by construction. For total control
    (custom redaction, summarization, reordering) use a Mode-2 builder instead.
    """

    #: Event types to include. ``None`` = all conversation types.
    include: frozenset[EventType] | None = None
    #: Which agents this projection may see. ``None`` = all.
    agents: tuple[str, ...] | None = None
    #: Recency window: keep only the last N matching events. ``None`` = unbounded.
    #: NOTE: this is truncation, not a compaction policy (docs 01/09§E).
    last_n: int | None = None
    #: System-instruction text, prepended as a leading SYSTEM item when set.
    instructions: str | None = None
    #: External context items appended after the conversation (e.g. account docs).
    extra: tuple[Item, ...] = ()

    def apply(self, log: "EventLog") -> list[Item]:
        """Project ``log`` into a point-in-time ``list[Item]``."""
        events = list(log.filter(types=self.include, agents=self.agents))
        if self.last_n is not None:
            events = events[-self.last_n :]

        items: list[Item] = []
        if self.instructions:
            items.append(Item(role=Role.SYSTEM, text=self.instructions))
        for e in events:
            item = _event_to_item(e)
            if item is not None:
                items.append(item)
        items.extend(self.extra)
        return items
