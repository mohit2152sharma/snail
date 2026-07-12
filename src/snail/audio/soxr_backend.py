"""SoxrResampleBackend — the real ``ResampleBackend`` (libsoxr via the ``soxr`` package).

Plugs into :class:`~snail.audio.LazyResampler` as its injected backend: the framework
keeps the lazy/no-op/memoize-per-rate *policy*, this supplies the DSP kernel. Kept in its
own module behind a **guarded import** so the core audio layer stays importable without
the native lib present (same contract the cleaner/codec backends follow); constructing
this class is the point where ``soxr`` becomes required.

``soxr.ResampleStream`` is **stateful** (carries filter history across chunks) and has
warmup latency — the first chunks may return **fewer or zero** output samples while the
polyphase filter fills. That is correct streaming behaviour: downstream the interior
rechunker (ingress) and the jitter buffer (egress) absorb the variable chunk sizes, so
callers must not assume a fixed in→out ratio per chunk. One stream per ``(from, to)``
pair, created lazily by :class:`LazyResampler` and retained for the session.

Interior audio is int16 mono (docs 11), so streams are built with ``num_channels=1``,
``dtype="int16"``. Quality defaults to soxr ``HQ`` — high quality, low CPU per 10ms
frame (the offload-threshold analysis, 09§E: inline small DSP on the loop).
"""

from __future__ import annotations

import numpy as np

try:
    import soxr
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without soxr
    raise ModuleNotFoundError(
        "SoxrResampleBackend needs the 'soxr' package (libsoxr). Install it, or inject a "
        "different ResampleBackend into LazyResampler."
    ) from exc


class _SoxrStream:
    """One stateful ``(from_rate, to_rate)`` int16-mono streaming converter."""

    __slots__ = ("_stream",)

    def __init__(self, from_rate: int, to_rate: int, quality: str) -> None:
        self._stream = soxr.ResampleStream(
            from_rate, to_rate, 1, dtype="int16", quality=quality
        )

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        return self._stream.resample_chunk(samples)


class SoxrResampleBackend:
    """Factory of per-rate-pair soxr streams (the injected :class:`ResampleBackend`)."""

    def __init__(self, *, quality: str = "HQ") -> None:
        self._quality = quality

    def stream(self, from_rate: int, to_rate: int) -> _SoxrStream:
        return _SoxrStream(from_rate, to_rate, self._quality)
