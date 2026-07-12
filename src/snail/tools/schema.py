"""Common-denominator schema validator (see docs 03, schema policy A).

The neutral schema dialect is the **common denominator** of both vendors — portable,
constrained, no per-vendor branching. Internally it is lowercase JSON-Schema-ish; the
adapter upcases types for Gemini (STRING/NUMBER/OBJECT) at serialize time.

Supported keywords: ``type`` (object/array/string/number/integer/boolean/null),
``properties``, ``required``, ``items``, ``enum``, ``nullable``. Returns ``None`` when
valid, else a short human-readable error string (used to build the model-facing
``invalid_args`` reason so it can self-correct).
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int — exclude it from number/integer.
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict | None, path: str = "") -> str | None:
    """Validate ``value`` against ``schema``. Return ``None`` if valid, else an error."""
    if schema is None:
        return None
    where = path or "value"

    if value is None:
        if schema.get("nullable") or schema.get("type") == "null":
            return None
        return f"{where}: null not allowed"

    t = schema.get("type")
    if t is not None:
        check = _TYPE_CHECKS.get(t)
        if check is None:
            return f"{where}: unknown schema type {t!r}"
        if not check(value):
            return f"{where}: expected {t}"

    if "enum" in schema and value not in schema["enum"]:
        return f"{where}: {value!r} not in enum"

    if t == "object":
        for req in schema.get("required", ()):
            if req not in value:
                return f"{where}.{req}: required field missing"
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                err = validate(value[key], sub, f"{path}.{key}" if path else key)
                if err:
                    return err

    if t == "array":
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, el in enumerate(value):
                err = validate(el, item_schema, f"{where}[{i}]")
                if err:
                    return err

    return None
