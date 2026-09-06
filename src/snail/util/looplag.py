"""LoopLagProbe — measure asyncio event-loop scheduling delay (saturation signal).

Schedules a wake-up every ``interval_s`` and records how late it actually fires
(actual − scheduled). Under a saturated / blocked loop the lag grows; the percentiles are
the direct signal that CPU-bound work on the loop is inflating every ``await`` (and thus
per-turn TTFB) as concurrent sessions grow. One probe per event loop is enough.
"""

from __future__ import annotations

import asyncio


class LoopLagProbe:
    """Samples event-loop scheduling lag; reports p50/p99/max in milliseconds."""

    def __init__(self, *, interval_s: float = 0.010) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._interval = interval_s
        self._task: asyncio.Task | None = None
        self._lags_ms: list[float] = []

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        nxt = loop.time() + self._interval
        while True:
            await asyncio.sleep(max(0.0, nxt - loop.time()))
            now = loop.time()
            self._lags_ms.append(max(0.0, (now - nxt) * 1000.0))
            nxt += self._interval

    @property
    def stats(self) -> dict:
        xs = sorted(self._lags_ms)
        if not xs:
            return {"p50_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0, "samples": 0}

        def pct(p: float) -> float:
            return xs[min(len(xs) - 1, int(p * len(xs)))]

        return {
            "p50_ms": pct(0.50),
            "p99_ms": pct(0.99),
            "max_ms": xs[-1],
            "samples": len(xs),
        }
