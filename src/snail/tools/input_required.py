"""``InputRequired`` — one pending question a :class:`ToolRun` blocks on (see docs 14).

A tool that cannot finish without something from outside the process declares (or
raises at runtime) an ``InputRequired``. It names the thing, fixes the answer's type,
carries the wording the model should use to ask, and bounds how long we wait.

The type names match :mod:`snail.tools.schema` exactly, so :meth:`schema` feeds the
same validator the tool layer already uses — no second dialect.
"""

from __future__ import annotations

import msgspec

#: Answer types an ``InputRequired`` may declare. Deliberately primitive: a structured
#: answer has no portable representation in the common-denominator dialect (docs 14 O3).
EXPECTS: tuple[str, ...] = ("boolean", "string", "number", "integer")


class InputRequired(msgspec.Struct, frozen=True, kw_only=True):
    """What a blocked run is waiting for."""

    #: Stable name for the thing being asked about; echoed by the model verbatim.
    key: str
    #: One of :data:`EXPECTS`. The authority on how the answer is validated.
    expects: str = "boolean"
    #: Model-facing wording — all runtime dynamism lives here, never in a schema.
    ask: str = ""
    #: How long to wait for a human. Distinct from ``Tool.timeout_s``, which bounds
    #: execution: different clocks (docs 14).
    budget_s: float = 60.0

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("InputRequired.key is required")
        if self.expects not in EXPECTS:
            raise ValueError(
                f"InputRequired {self.key!r}: expects must be one of {EXPECTS}, "
                f"got {self.expects!r}"
            )

    @property
    def schema(self) -> dict:
        """The neutral schema the submitted value is validated against."""
        return {"type": self.expects}
