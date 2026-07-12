"""Context model: append-only event log, snapshot projections, neutral items.

See docs 01-context-model. Public surface:

- :class:`Event`, :class:`EventType`, :class:`Item`, :class:`Role` — the frozen schema
- :class:`EventLog` — append-only single source of truth
- :class:`Projection` — declarative Mode-1 projection; ``ProjectionBuilder`` = Mode-2
"""

from .events import Event, EventType, Item, Role
from .log import EventLog, ProjectionBuilder
from .projection import Projection

__all__ = [
    "Event",
    "EventType",
    "Item",
    "Role",
    "EventLog",
    "Projection",
    "ProjectionBuilder",
]
