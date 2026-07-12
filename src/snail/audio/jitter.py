"""JitterBuffer — smooth bursty vendor output to the speaker clock (see docs 11).

Vendor audio arrives in **bursts** (a turn's worth in a few large messages), but the
speaker consumes at a steady sample-clock. The jitter buffer bridges the two: it
**prebuffers** a target amount before releasing anything, then hands out fixed-size
frames on each paced drain, so brief gaps between vendor messages don't underrun the
speaker. Its drained frames feed the :class:`~snail.router.OutputGate` (the token-guarded
GATE-2 ring, docs 11 §Buffers) — jitter smooths *timing*, the gate decides *whose* audio
reaches the user.

State machine (one stream): **PREBUFFERING** until fill ≥ ``prefill``, then **PLAYING**
— ``pop`` yields ``frame_size`` samples per call. On **underrun** (a drain with < one
frame buffered) it counts the event and drops back to PREBUFFERING, so playout re-arms
cleanly rather than emitting choppy partial frames. Interior is 48k int16 mono, so sizes
are in samples at 48k (10ms = 480).

Internally a deque of int16 chunks + a head offset — no per-push concatenation, and a
frame is stitched across chunk boundaries only when one actually straddles them.
"""

from __future__ import annotations

import enum
from collections import deque

import numpy as np

FRAME_LEN = 480  # 10ms @ 48kHz mono


class JitterState(enum.Enum):
    PREBUFFERING = "prebuffering"  # accumulating toward prefill; drain returns None
    PLAYING = "playing"  # releasing frames on the paced drain


class JitterBuffer:
    """Prebuffering, underrun-aware smoothing buffer for one output stream."""

    __slots__ = (
        "_frame",
        "_prefill",
        "_chunks",
        "_head",
        "_total",
        "_state",
        "_underruns",
    )

    def __init__(self, *, frame_size: int = FRAME_LEN, prefill_frames: int = 3) -> None:
        if frame_size < 1:
            raise ValueError("frame_size must be >= 1")
        self._frame = frame_size
        self._prefill = frame_size * max(1, prefill_frames)
        self._chunks: deque[np.ndarray] = deque()
        self._head = 0  # consumed offset into chunks[0]
        self._total = 0  # buffered samples not yet drained
        self._state = JitterState.PREBUFFERING
        self._underruns = 0

    @property
    def state(self) -> JitterState:
        return self._state

    @property
    def buffered(self) -> int:
        return self._total

    def push(self, samples: np.ndarray) -> None:
        """Add a vendor burst. Once fill reaches ``prefill``, playout arms."""
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        if len(samples) == 0:
            return
        self._chunks.append(samples)
        self._total += len(samples)
        if self._state is JitterState.PREBUFFERING and self._total >= self._prefill:
            self._state = JitterState.PLAYING

    def pop(self) -> np.ndarray | None:
        """Paced drain: one ``frame_size`` frame, or ``None`` if not ready.

        ``None`` while PREBUFFERING, or on underrun (< one frame buffered) — which counts
        the event and re-arms prebuffering so playout resumes smoothly.
        """
        if self._state is not JitterState.PLAYING:
            return None
        if self._total < self._frame:
            self._underruns += 1
            self._state = JitterState.PREBUFFERING
            return None
        return self._take(self._frame)

    def drain_partial(self) -> np.ndarray | None:
        """Flush the tail (< one frame) at end-of-turn, zero-padded, or ``None``."""
        if self._total == 0:
            return None
        n = min(self._frame, self._total)
        frame = self._take(n)
        if n < self._frame:
            frame = np.concatenate(
                [frame, np.zeros(self._frame - n, dtype=np.int16)]
            )
        return frame

    def flush(self) -> None:
        """Discard everything (barge-in / cut) and re-arm prebuffering."""
        self._chunks.clear()
        self._head = 0
        self._total = 0
        self._state = JitterState.PREBUFFERING

    @property
    def stats(self) -> dict[str, int]:
        return {
            "buffered": self._total,
            "underruns_total": self._underruns,
            "state": self._state.value,
        }

    # --- internals --------------------------------------------------------

    def _take(self, n: int) -> np.ndarray:
        """Pull exactly ``n`` buffered samples, stitching across chunk boundaries."""
        head = self._chunks[0]
        # Fast path: the current head chunk alone satisfies the request.
        if len(head) - self._head >= n:
            out = head[self._head : self._head + n].copy()
            self._head += n
            if self._head == len(head):
                self._chunks.popleft()
                self._head = 0
            self._total -= n
            return out
        # Slow path: the frame straddles chunk boundaries — stitch pieces.
        parts: list[np.ndarray] = []
        need = n
        while need > 0:
            chunk = self._chunks[0]
            avail = len(chunk) - self._head
            take = min(avail, need)
            parts.append(chunk[self._head : self._head + take])
            self._head += take
            need -= take
            if self._head == len(chunk):
                self._chunks.popleft()
                self._head = 0
        self._total -= n
        return np.concatenate(parts)
