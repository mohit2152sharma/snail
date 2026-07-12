"""Resample — lazy, per-target-rate rate conversion (see docs 11 §pipeline).

Resampling is the CPU lever the pipeline is built around (docs 11): the 48k interior is
copy-free, and a consumer's leg converts **only when its vendor rate differs from 48k**,
**memoized per distinct target rate** so N same-rate legs (e.g. two Gemini legs, both
48k→16k) share one converter. A target already at the interior rate pays nothing.

That policy — *no-op at equal rates, one stateful converter per distinct ``(from,
to)``* — is the framework value here and lives in :class:`LazyResampler`. The actual DSP
kernel (``libsoxr`` via the ``soxr`` package) is an injected :class:`ResampleBackend`, so
this module and its tests stay dependency-free (same pattern as the cleaner's
``DenoiseBackend``). Converters are **stateful** across chunks (a streaming resampler
carries filter history), so one instance is retained per rate-pair for the session.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Resampler(Protocol):
    """A stateful streaming converter for one fixed ``(from_rate, to_rate)`` pair."""

    def process(self, samples: np.ndarray) -> np.ndarray: ...


@runtime_checkable
class ResampleBackend(Protocol):
    """Factory for per-rate-pair converters (e.g. wrapping ``soxr.ResampleStream``)."""

    def stream(self, from_rate: int, to_rate: int) -> Resampler: ...


class LazyResampler:
    """Per-distinct-rate memoized resampler with an equal-rate fast path (docs 11).

    One instance serves a whole session's legs: it holds at most one converter per
    distinct ``(from_rate, to_rate)`` seen, so same-rate legs reuse it. ``resample`` at
    equal rates returns the input **untouched** (no copy, no backend call) — the "target
    already at interior rate pays nothing" guarantee.
    """

    def __init__(self, backend: ResampleBackend) -> None:
        self._backend = backend
        self._streams: dict[tuple[int, int], Resampler] = {}

    def resample(
        self, samples: np.ndarray, *, from_rate: int, to_rate: int
    ) -> np.ndarray:
        if from_rate == to_rate:
            return samples  # no-op: already at the target rate
        key = (from_rate, to_rate)
        stream = self._streams.get(key)
        if stream is None:
            stream = self._backend.stream(from_rate, to_rate)
            self._streams[key] = stream
        return stream.process(samples)

    @property
    def rate_pairs(self) -> list[tuple[int, int]]:
        """Distinct converter rate-pairs currently memoized (for stats/tests)."""
        return list(self._streams)

    def reset(self) -> None:
        """Drop all converters (e.g. session end). Fresh state on next use."""
        self._streams.clear()
