"""ToolRegistry — the static catalog (see docs 03).

``name → Tool``. Session/global, reusable across agents ("what tools exist"). Distinct
from the live in-flight :class:`~snail.registry.ToolCallRegistry` ("what calls are
happening now") — do not merge the two.
"""

from __future__ import annotations

from collections.abc import Iterator

from snail.vendor.params import ToolSpec

from .tool import Tool


class ToolRegistry:
    """A catalog of tools, keyed by name."""

    __slots__ = ("_tools",)

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self, names: list[str] | None = None) -> tuple[ToolSpec, ...]:
        """Vendor-neutral declarations for exposure (setup binding).

        ``names`` restricts to a per-agent exposure subset (``AgentSpec.tools``);
        ``None`` = the whole catalog. Unknown names are skipped.
        """
        selected = self._tools.values() if names is None else (
            self._tools[n] for n in names if n in self._tools
        )
        return tuple(t.to_spec() for t in selected)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())
