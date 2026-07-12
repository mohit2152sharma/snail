"""OutputGate — GATE 2: single-producer output ring + atomic ownership token (docs 05).

Only the **token holder** (the active agent) may write audio that reaches the user;
everyone else's write is suppressed. Promotion = atomic token transfer. This single
primitive delivers overlap-avoidance (product) + low barge-in/handoff latency (perf):
with one token, **overlap is structurally impossible** — worst case is a gap or a
clipped tail, never two voices.

``on_drop`` is called for every frame flushed/evicted so the caller can return pooled
output frames (mirrors the FramePool release contract); default no-op.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

OnDrop = Callable[[Any], None]


class OutputGate:
    """Token-guarded, bounded, single-writer output ring."""

    __slots__ = ("_holder", "_ring", "_depth", "_on_drop", "_suppressed", "_dropped")

    def __init__(self, depth: int = 32, on_drop: OnDrop | None = None) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        self._holder: str | None = None
        self._ring: deque[Any] = deque()
        self._depth = depth
        self._on_drop = on_drop
        self._suppressed = 0  # writes rejected for not holding the token
        self._dropped = 0  # frames evicted/flushed

    @property
    def holder(self) -> str | None:
        return self._holder

    def grant(self, agent_id: str) -> None:
        """Give the token to ``agent_id``. Must be free (use :meth:`transfer` to move)."""
        if self._holder is not None and self._holder != agent_id:
            raise RuntimeError(
                f"token held by {self._holder!r}; use transfer() to move it"
            )
        self._holder = agent_id

    def revoke(self) -> str | None:
        """Drop the token (user-facing drain stops). Ring is left intact. Returns old
        holder. For a hard cut, follow with :meth:`flush`."""
        old, self._holder = self._holder, None
        return old

    def transfer(self, new_agent_id: str) -> str | None:
        """Atomically move the token to ``new_agent_id``. Returns the old holder.

        Overlap is impossible across the swap: there is only ever one holder.
        """
        old, self._holder = self._holder, new_agent_id
        return old

    def write(self, agent_id: str, frame: Any) -> bool:
        """Enqueue ``frame`` for the user — only if ``agent_id`` holds the token.

        Non-holder writes are suppressed (dropped via ``on_drop``); a full ring evicts
        oldest (playout: newest matters most). Returns whether the frame was enqueued.
        """
        if agent_id != self._holder:
            self._suppressed += 1
            self._drop(frame)
            return False
        if len(self._ring) >= self._depth:
            self._drop(self._ring.popleft())
        self._ring.append(frame)
        return True

    def pop(self) -> Any | None:
        """Paced drain to the speaker (session pulls). ``None`` if empty."""
        if not self._ring:
            return None
        return self._ring.popleft()

    def flush(self) -> int:
        """Drop all queued audio (the CUT_NOW hard cut). Returns the count dropped."""
        n = 0
        while self._ring:
            self._drop(self._ring.popleft())
            n += 1
        return n

    def _drop(self, frame: Any) -> None:
        self._dropped += 1
        if self._on_drop is not None:
            self._on_drop(frame)

    def __len__(self) -> int:
        return len(self._ring)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "queued": len(self._ring),
            "suppressed_total": self._suppressed,
            "dropped_total": self._dropped,
        }
