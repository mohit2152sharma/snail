"""JitterBuffer — smooth bursty vendor output to the speaker clock (see docs 11).

Vendor audio arrives in **bursts** (a turn's worth in a few large messages), but the
speaker consumes at a steady sample-clock. The jitter buffer bridges the two: it
**prebuffers** a target amount before releasing anything, then hands out fixed-size
frames on each paced drain, so brief gaps between vendor messages don't underrun the
speaker. Its drained frames feed the :class:`~snail.router.OutputGate` (the token-guarded
GATE-2 ring, docs 11 §Buffers) — jitter smooths *timing*, the gate decides *whose* audio
reaches the user.

State machine (one stream): **PREBUFFERING** until fill ≥ the current target, then
**PLAYING** — ``pop`` yields ``frame_size`` samples per call. On **underrun** (a drain
with < one frame buffered) it counts the event and drops back to PREBUFFERING, so
playout re-arms cleanly rather than emitting choppy partial frames. Interior is 48k
int16 mono, so sizes are in samples at 48k (10ms = 480).

**Adaptive target — the per-turn TTFB lever.** Every re-arm (turn start, post-underrun,
post-flush) pays its prebuffer as pure added latency before the *first* byte of that
turn reaches the client — a fixed ``prefill_frames`` constant is a static tax nobody
measured. Instead the target is a feedback-controlled variable, AIMD-style (the same
control law TCP uses for congestion windows, applied here to buffer depth):

* the **first** arm of a fresh buffer stays conservative — ``prefill_frames`` (the
  ceiling), since there is no jitter evidence yet;
* every underrun is direct evidence the current target is too small: **additive
  increase** by one frame, capped at the ceiling;
* a streak of ``decay_after`` consecutive clean pops (no underrun) is evidence the
  channel is stable: **decay** the target down by one frame, floored at
  ``min_prefill_frames``.

So a session's first turn pays the safe, conservative prefill; every later turn on a
healthy connection re-arms at the learned-down floor — most of the fixed 11 audio-plane
tax disappears exactly where it matters (subsequent per-turn TTFB), while a genuinely
jittery link grows back to the safe ceiling automatically, never below the floor.

Internally a deque of int16 chunks + a head offset — no per-push concatenation, and a
frame is stitched across chunk boundaries only when one actually straddles them. The
adaptive control adds two ``int`` counters — no new data structure, O(1) per push/pop.
"""

from __future__ import annotations

import enum
from collections import deque

import numpy as np

FRAME_LEN = 480  # 10ms @ 48kHz mono


class JitterState(enum.Enum):
    PREBUFFERING = "prebuffering"  # accumulating toward the current target; pop → None
    PLAYING = "playing"  # releasing frames on the paced drain


class JitterBuffer:
    """Prebuffering, underrun-aware smoothing buffer with an AIMD-adaptive target."""

    __slots__ = (
        "_frame",
        "_min_frames",
        "_max_frames",
        "_target_frames",
        "_decay_after",
        "_stable_pops",
        "_chunks",
        "_head",
        "_total",
        "_state",
        "_underruns",
    )

    def __init__(
        self,
        *,
        frame_size: int = FRAME_LEN,
        prefill_frames: int = 3,
        min_prefill_frames: int = 1,
        decay_after: int = 50,
    ) -> None:
        """``prefill_frames`` is both the ceiling and the conservative first-arm target
        (no jitter evidence yet); ``min_prefill_frames`` is the floor the target decays
        toward after ``decay_after`` consecutive underrun-free pops."""
        if frame_size < 1:
            raise ValueError("frame_size must be >= 1")
        if min_prefill_frames < 1:
            raise ValueError("min_prefill_frames must be >= 1")
        self._frame = frame_size
        self._max_frames = max(1, prefill_frames)
        self._min_frames = min(min_prefill_frames, self._max_frames)
        self._target_frames = self._max_frames  # first arm: conservative (no evidence)
        self._decay_after = max(1, decay_after)
        self._stable_pops = 0
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

    @property
    def target_frames(self) -> int:
        """Current adaptive prefill target, in frames (observable for metrics/tests)."""
        return self._target_frames

    def push(self, samples: np.ndarray) -> None:
        """Add a vendor burst. Once fill reaches the current target, playout arms."""
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        if len(samples) == 0:
            return
        self._chunks.append(samples)
        self._total += len(samples)
        if (
            self._state is JitterState.PREBUFFERING
            and self._total >= self._frame * self._target_frames
        ):
            self._state = JitterState.PLAYING

    def pop(self) -> np.ndarray | None:
        """Paced drain: one ``frame_size`` frame, or ``None`` if not ready.

        ``None`` while PREBUFFERING, or on underrun (< one frame buffered) — which counts
        the event, grows the adaptive target (additive increase, capped), and re-arms
        prebuffering so playout resumes smoothly. A clean pop instead counts toward the
        decay streak that eventually lowers the target back down.
        """
        if self._state is not JitterState.PLAYING:
            return None
        if self._total < self._frame:
            self._underruns += 1
            self._state = JitterState.PREBUFFERING
            self._target_frames = min(self._max_frames, self._target_frames + 1)
            self._stable_pops = 0
            return None
        frame = self._take(self._frame)
        self._stable_pops += 1
        if self._stable_pops >= self._decay_after:
            self._target_frames = max(self._min_frames, self._target_frames - 1)
            self._stable_pops = 0
        return frame

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
            "target_frames": self._target_frames,
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
