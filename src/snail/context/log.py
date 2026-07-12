"""Append-only event log (see docs 01-context-model).

The log is the single source of truth. It is append-only: no event is ever mutated
or removed, which is what makes concurrent projections lock-free and torn-read-free
(two vendor streams run at once — docs 01/06). Projections are point-in-time
snapshots taken at turn/handoff boundaries.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

from .events import Event, EventType, Item

#: A Mode-2 imperative projection: total control, but MUST still return ``Item[]``
#: (docs 01). The adapter serializes; a builder that returns vendor dicts breaks
#: vendor-neutrality.
ProjectionBuilder = Callable[["EventLog"], list[Item]]


class EventLog:
    """Ordered, append-only sequence of :class:`Event`.

    ``seq`` is a monotonic counter assigned here, so callers never set it. Reads
    return the live tuple view; because events are frozen, sharing them is copy-free.
    """

    __slots__ = ("_events", "_seq")

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seq: int = 0

    def append(
        self,
        type: EventType,
        *,
        agent_id: str | None = None,
        content: str = "",
        meta: dict | None = None,
        ts: float | None = None,
    ) -> Event:
        """Append a new event, assigning it the next ``seq``. Returns the event."""
        event = Event(
            seq=self._seq,
            ts=time.time() if ts is None else ts,
            type=type,
            agent_id=agent_id,
            content=content,
            meta=meta,
        )
        self._events.append(event)
        self._seq += 1
        return event

    def filter(
        self,
        *,
        types: Iterable[EventType] | None = None,
        agents: Iterable[str] | None = None,
    ) -> Iterator[Event]:
        """Yield events matching the given type / agent constraints (in order).

        ``None`` for a dimension means "no constraint". This is the read primitive
        both projection modes build on.
        """
        type_set = frozenset(types) if types is not None else None
        agent_set = frozenset(agents) if agents is not None else None
        for e in self._events:
            if type_set is not None and e.type not in type_set:
                continue
            if agent_set is not None and e.agent_id not in agent_set:
                continue
            yield e

    def project(self, projection: "Projection | ProjectionBuilder") -> list[Item]:
        """Compute a point-in-time ``Item[]`` snapshot from this log.

        Accepts a declarative :class:`~snail.context.projection.Projection` (Mode 1)
        or an imperative builder callable (Mode 2). Both stop at ``Item[]``.
        """
        if callable(projection):
            return projection(self)
        return projection.apply(self)

    @property
    def events(self) -> tuple[Event, ...]:
        """Immutable snapshot of the log so far."""
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)


# Imported at end to avoid a cycle: projection.py imports Item/Event from events,
# and EventLog only needs the Projection type for annotations.
from .projection import Projection  # noqa: E402
