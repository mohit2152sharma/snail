"""The ``provide_input`` framework tool — how an answer gets back in (see docs 14).

**One declaration, typed slots.** A single tool carries every kind of answer, with one
optional field per primitive type; the model fills the slot named by ``expects`` in the
``input_required`` result it just received. The alternative — one tool per type — was
weighed and dropped: this keeps the exposed surface at one tool.

The vendor's function declarations are bound at **setup**, so the schema cannot be
minted per question. It does not need to be: the runtime-varying part of a question is
its *wording*, which rides in the result payload and never touches a schema. Only the
*shape* of the answer lives here, and the shapes are bounded by :data:`EXPECTS`.

When keys are declared on tools, ``key`` carries an ``enum`` built by walking the
registry, which steers the model toward keys that exist. With no declarations it is a
plain string — the executor validates it against the blocked run either way, so
behaviour is identical and only the steering is lost (docs 14, O4/O5).

Dispatch never reaches the fallback handler: the session intercepts by name, because it
alone knows which agent the call arrived on. The handler exists so that a missed
interception still closes the call correctly rather than raising.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .input_required import EXPECTS
from .result import ToolResult
from .tool import Tool

#: The reserved name. The session matches on this before any registry lookup.
PROVIDE_INPUT = "provide_input"

#: ``expects`` → the argument the model puts the answer in. ``integer`` shares the
#: numeric slot; :attr:`InputRequired.schema` is what rejects a non-integral value.
SLOT_BY_EXPECTS: dict[str, str] = {
    "boolean": "bool_value",
    "string": "text_value",
    "number": "number_value",
    "integer": "number_value",
}

#: Splice into an agent's system instruction. Deliberately short — long instructions
#: drift in live models. Step 5 is the only guard against the model reporting a value
#: the user never gave; it reduces that risk, it does not remove it.
#:
#: Steps 4 and 5 are split because collapsing them silently loses every "no". Told only
#: that a refusal is a reason *not* to answer, a model hears "no, don't take a photo",
#: says "okay, I won't", and calls nothing — so the run sits blocked for its whole budget
#: and dies by expiry, and the tool's denial branch never runs. Live logs showed six
#: submitted answers, all ``true``: consent was unrefusable in practice. A refusal to a
#: yes/no question *is* an answer; only silence is not.
PROVIDE_INPUT_INSTRUCTION = """\
Some tool results ask for input instead of giving an answer. When a result has
status "input_required":
  1. Ask the user the question in "ask", in your own words and voice.
  2. Wait for their answer.
  3. Call provide_input, copying "for_tool" and "key" exactly from that result,
     and putting the user's answer in the slot named by "expects":
       boolean -> bool_value, string -> text_value, number -> number_value.
  4. A refusal is an answer. If they say no, decline, or withhold permission,
     call provide_input with the negative value (boolean -> bool_value: false).
  5. Only skip provide_input when they gave no answer at all — they changed the
     subject, asked something else, or ignored the question. Never invent a value
     they did not give.

Some tool results have status "skipped". Say nothing about them and carry on.\
"""


def provide_input_schema(keys: Iterable[str] = ()) -> dict:
    """The input schema, with ``key`` constrained to ``keys`` when any are declared."""
    key_schema: dict[str, Any] = {
        "type": "string",
        "description": "copy exactly from the tool result",
    }
    known = tuple(dict.fromkeys(keys))  # de-duped, order preserved
    if known:
        key_schema["enum"] = list(known)
    return {
        "type": "object",
        "properties": {
            "for_tool": {
                "type": "string",
                "description": "copy exactly from the tool result",
            },
            "key": key_schema,
            "bool_value": {"type": "boolean"},
            "text_value": {"type": "string"},
            "number_value": {"type": "number"},
        },
        "required": ["for_tool", "key"],
    }


def _unreached(args: dict) -> ToolResult:
    """Only runs if the session failed to intercept — close cleanly, don't raise."""
    return ToolResult.skipped("no input was expected")


def build_provide_input_tool(keys: Iterable[str] = ()) -> Tool:
    """The framework tool to register once per session."""
    return Tool(
        PROVIDE_INPUT,
        _unreached,
        description=(
            "Supply a value that a tool asked for. Only call this after a tool "
            "result asked for input."
        ),
        input_schema=provide_input_schema(keys),
        output_schema={"type": "object"},
        is_framework=True,
    )


def declared_keys(tools: Iterable[Tool]) -> tuple[str, ...]:
    """Every ``InputRequired`` key across ``tools`` — the enum for setup binding."""
    seen: dict[str, None] = {}
    for tool in tools:
        for required in tool.requires:
            seen.setdefault(required.key, None)
    return tuple(seen)


def extract_value(args: dict, expects: str) -> tuple[Any, str | None]:
    """Read the answer out of the slot named by ``expects``.

    Returns ``(value, error)``. The named slot is authoritative — a model that fills
    several is not an error, the others are simply ignored. A missing slot is reported
    with the name it should have used, so the retry can be correct (``invalid_args`` is
    retriable, and the run stays blocked meanwhile).
    """
    slot = SLOT_BY_EXPECTS.get(expects)
    if slot is None:  # unreachable via InputRequired, which validates expects
        return None, f"unsupported expects {expects!r}; must be one of {EXPECTS}"
    if slot not in args or args[slot] is None:
        return None, f"{slot} is required for an answer of type {expects}"
    return args[slot], None
