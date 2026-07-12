"""RulePolicy predicate surface — declarative default + callable escape hatch (docs 05).

A predicate reads **only** the :class:`~snail.router.signals.RoutingSignal` (no sockets,
no live vendor state). The declarative form is a serializable ``{field, op, value}``
tree — safe, inspectable, no ``eval``. The callable form is full power for the rare
complex case. Text matches are a coarse *intent hint* only — never gate authority on
one (docs 05); that belongs in a deterministic ``tool_result.status``.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from typing import Any

_COMPARE_OPS = {"==", "!=", ">", "<", ">=", "<=", "~=", "contains", "in"}


def resolve_field(signal: Any, path: str) -> Any:
    """Walk a dotted ``path`` (e.g. ``"event.status"``) against the signal.

    Attribute access first, then mapping lookup; a missing link yields ``None``.
    """
    obj: Any = signal
    for part in path.split("."):
        if obj is None:
            return None
        if hasattr(obj, part):
            obj = getattr(obj, part)
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _coerce(actual: Any, expected: Any) -> Any:
    """Let a raw ``expected`` (str/num) compare against an Enum ``actual`` by value."""
    if isinstance(actual, enum.Enum) and not isinstance(expected, enum.Enum):
        return actual.value
    return actual


class Predicate:
    """Base: composable with ``&`` / ``|`` / ``~``; evaluated via :meth:`matches`."""

    def matches(self, signal: Any) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_dict(self) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def __and__(self, other: "Predicate") -> "BoolOp":
        return BoolOp("and", (self, other))

    def __or__(self, other: "Predicate") -> "BoolOp":
        return BoolOp("or", (self, other))

    def __invert__(self) -> "Not":
        return Not(self)


class Comparison(Predicate):
    """``resolve(field) <op> value``."""

    __slots__ = ("field", "op", "value")

    def __init__(self, field: str, op: str, value: Any) -> None:
        if op not in _COMPARE_OPS:
            raise ValueError(f"unknown op {op!r}")
        self.field = field
        self.op = op
        self.value = value

    def matches(self, signal: Any) -> bool:
        actual = _coerce(resolve_field(signal, self.field), self.value)
        op, expected = self.op, self.value
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if actual is None:
            return False  # ordering / membership on a missing field never matches
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
        if op == "~=":
            return re.search(expected, str(actual)) is not None
        if op == "contains":
            return expected in actual
        if op == "in":
            return actual in expected
        return False  # pragma: no cover

    def to_dict(self) -> dict:
        return {"field": self.field, "op": self.op, "value": self.value}


class BoolOp(Predicate):
    """``and`` / ``or`` over child predicates (short-circuiting)."""

    __slots__ = ("op", "children")

    def __init__(self, op: str, children: tuple[Predicate, ...]) -> None:
        if op not in ("and", "or"):
            raise ValueError(f"bad bool op {op!r}")
        self.op = op
        self.children = children

    def matches(self, signal: Any) -> bool:
        if self.op == "and":
            return all(c.matches(signal) for c in self.children)
        return any(c.matches(signal) for c in self.children)

    def to_dict(self) -> dict:
        return {"op": self.op, "args": [c.to_dict() for c in self.children]}


class Not(Predicate):
    __slots__ = ("child",)

    def __init__(self, child: Predicate) -> None:
        self.child = child

    def matches(self, signal: Any) -> bool:
        return not self.child.matches(signal)

    def to_dict(self) -> dict:
        return {"op": "not", "arg": self.child.to_dict()}


class CallablePredicate(Predicate):
    """Escape hatch: ``fn(signal) -> bool``. Opaque, unsandboxed — rare cases only."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[Any], bool]) -> None:
        self.fn = fn

    def matches(self, signal: Any) -> bool:
        return bool(self.fn(signal))

    def to_dict(self) -> dict:
        raise TypeError("CallablePredicate is not serializable (escape hatch)")


class F:
    """Fluent builder for a declarative :class:`Comparison`.

    ``F("event.status") == "escalate"`` → ``Comparison``. Combine with ``& | ~``.
    """

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def __eq__(self, value: Any) -> Comparison:  # type: ignore[override]
        return Comparison(self.path, "==", value)

    def __ne__(self, value: Any) -> Comparison:  # type: ignore[override]
        return Comparison(self.path, "!=", value)

    def __gt__(self, value: Any) -> Comparison:
        return Comparison(self.path, ">", value)

    def __lt__(self, value: Any) -> Comparison:
        return Comparison(self.path, "<", value)

    def __ge__(self, value: Any) -> Comparison:
        return Comparison(self.path, ">=", value)

    def __le__(self, value: Any) -> Comparison:
        return Comparison(self.path, "<=", value)

    def matches_regex(self, pattern: str) -> Comparison:
        return Comparison(self.path, "~=", pattern)

    def contains(self, value: Any) -> Comparison:
        return Comparison(self.path, "contains", value)

    def in_(self, values: Any) -> Comparison:
        return Comparison(self.path, "in", values)

    __hash__ = None  # type: ignore[assignment]  # builder, not a value


def predicate_from_dict(d: dict) -> Predicate:
    """Reconstruct a declarative predicate tree from its serialized form."""
    op = d.get("op")
    if op in ("and", "or"):
        return BoolOp(op, tuple(predicate_from_dict(c) for c in d["args"]))
    if op == "not":
        return Not(predicate_from_dict(d["arg"]))
    return Comparison(d["field"], d["op"], d["value"])
