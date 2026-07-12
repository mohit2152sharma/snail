"""Audio cleaner — per-consumer denoise stage (see docs 11 §Cleaner).

The clean stage sits on the user-input leg *after* the RAW fan-out: it runs **only when
at least one consumer wants CLEAN** (docs 11), so a session of self-denoising /
raw-wanting agents pays nothing here. It emits **cleaned 48kHz mono int16** onto the
CLEAN fan-out.

Two swappable pieces, mirroring the doc:

* :class:`AudioCleaner` — ``process(samples) -> [chunks]``. Ships :class:`NullCleaner`
  (bypass) and :class:`RNNoiseCleaner` (default denoiser).
* :class:`DenoiseBackend` — the actual per-frame denoise kernel. Injected, so the
  framework and its tests stay free of the native ``librnnoise`` dependency (wired as an
  optional guarded import elsewhere). Same injection pattern as the connection layer's
  ``Connector``.

**The one framing constraint the interior imposes** (docs 11 §Constraints): RNNoise
wants exactly **480-sample (10ms)** frames. Client 10ms frames already decode to 480
samples at 48k, so the common path is 1-in-1-out; the :class:`Rechunker` is the
defensive re-aligner for any other input size, using a **preallocated** 480-sample
accumulator (no per-frame allocation).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

FRAME_LEN = 480  # 10ms @ 48kHz mono — the RNNoise frame size (docs 11)


@runtime_checkable
class DenoiseBackend(Protocol):
    """The per-frame denoise kernel (e.g. an ``librnnoise`` binding).

    ``process_480`` takes one 480-sample int16 mono frame @48k and returns a cleaned
    480-sample int16 frame. Stateful across calls (RNNoise carries filter state) — one
    backend instance per stream.
    """

    def process_480(self, frame: np.ndarray) -> np.ndarray: ...


@runtime_checkable
class AudioCleaner(Protocol):
    """Swappable denoise stage. ``process`` may emit 0+ cleaned frames per call."""

    def process(self, samples: np.ndarray) -> list[np.ndarray]: ...

    def flush(self) -> list[np.ndarray]: ...

    def reset(self) -> None: ...


class NullCleaner:
    """Bypass — pass audio through untouched (no rechunk, no denoise).

    Used when cleaning is disabled for a consumer. Not the same as *skipping* the stage
    entirely (that happens upstream when no consumer wants CLEAN); this is an explicit
    no-op cleaner kept behind the same interface for uniform wiring.
    """

    def process(self, samples: np.ndarray) -> list[np.ndarray]:
        return [samples] if len(samples) else []

    def flush(self) -> list[np.ndarray]:
        return []

    def reset(self) -> None:
        pass


class Rechunker:
    """Re-aligns an arbitrary-length int16 stream to fixed 480-sample frames.

    Holds a **preallocated** 480-sample accumulator and a fill index — copies incoming
    samples into it and emits a fresh 480-frame each time it fills, carrying the
    remainder (< 480) to the next call. No growing buffers, no per-frame allocation on
    the steady path beyond the emitted copy (the backend may retain/mutate a frame, so
    each emitted frame is its own array).
    """

    __slots__ = ("_buf", "_fill")

    def __init__(self) -> None:
        self._buf = np.empty(FRAME_LEN, dtype=np.int16)
        self._fill = 0

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        """Feed samples; return every complete 480-frame now available."""
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        out: list[np.ndarray] = []
        pos = 0
        n = len(samples)
        while pos < n:
            take = min(FRAME_LEN - self._fill, n - pos)
            self._buf[self._fill : self._fill + take] = samples[pos : pos + take]
            self._fill += take
            pos += take
            if self._fill == FRAME_LEN:
                out.append(self._buf.copy())
                self._fill = 0
        return out

    def flush(self) -> np.ndarray | None:
        """Emit the final partial frame zero-padded to 480, or ``None`` if empty."""
        if self._fill == 0:
            return None
        frame = np.zeros(FRAME_LEN, dtype=np.int16)
        frame[: self._fill] = self._buf[: self._fill]
        self._fill = 0
        return frame

    def reset(self) -> None:
        self._fill = 0


class RNNoiseCleaner:
    """Default cleaner: rechunk to 480 frames, denoise each via the backend (docs 11).

    Rate-native: input and output are 48k mono int16, so no resample surrounds it (the
    RNNoise + opus-48k synergy, docs 11 §Why 48kHz). The backend abstracts the native
    denoise call and is injected — keeping this class, and the whole audio layer's tests,
    dependency-free.
    """

    def __init__(self, backend: DenoiseBackend) -> None:
        self._backend = backend
        self._rechunk = Rechunker()

    def process(self, samples: np.ndarray) -> list[np.ndarray]:
        return [self._backend.process_480(f) for f in self._rechunk.push(samples)]

    def flush(self) -> list[np.ndarray]:
        """Drain the rechunker's tail (utterance end) through the backend."""
        tail = self._rechunk.flush()
        return [] if tail is None else [self._backend.process_480(tail)]

    def reset(self) -> None:
        self._rechunk.reset()
