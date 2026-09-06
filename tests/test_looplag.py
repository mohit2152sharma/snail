import asyncio
import time

from snail.util.looplag import LoopLagProbe


async def test_probe_collects_samples_and_reports():
    p = LoopLagProbe(interval_s=0.005)
    p.start()
    await asyncio.sleep(0.1)
    await p.stop()
    s = p.stats
    assert s["samples"] >= 5
    assert s["p50_ms"] >= 0.0
    assert s["max_ms"] >= s["p50_ms"]


async def test_probe_detects_blocking():
    p = LoopLagProbe(interval_s=0.005)
    p.start()
    await asyncio.sleep(0.02)
    time.sleep(0.04)  # block the loop for 40ms
    await asyncio.sleep(0.05)
    await p.stop()
    assert p.stats["max_ms"] >= 20.0  # the stall is visible in the lag
