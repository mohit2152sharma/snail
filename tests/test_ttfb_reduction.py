"""Measured per-turn TTFB reduction from server-side VAD endpointing.

No live Gemini. Drives the **real** ``MultiAgentBridge`` endpointing path frame-by-frame
and measures the quantity the bridge actually controls: the latency from the last speech
frame to the turn-end signal it emits (``ACTIVITY_END``). Compares it to the auto-VAD
baseline (``silence_duration_ms=800`` — the value host/echo used before this change) and
asserts the controllable end-of-speech→turn-end term is cut ≥ 50%.

This is the dominant term of end-of-speech→first-byte: everything downstream (inference,
network, pipeline) is unchanged, so a ≥50% cut here is a ≥50% cut of the TTFB window's
only movable-in-code component.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path("examples/multi-agent").resolve()))

from backend.bridge import MultiAgentBridge  # noqa: E402
from backend.agents import HOST_ID  # noqa: E402

from snail.audio.opus_codec import OpusCodec  # noqa: E402
from snail.audio.vad import VadEvent  # noqa: E402
from snail.vendor import RealtimeControl  # noqa: E402

# Import the fakes from the endpointing integration test (same harness).
from test_bridge_endpointing import FakePool, FakeConn, FakeSocket  # noqa: E402

FRAME_MS = 10.0  # one interior frame = 10ms @ 48kHz
BASELINE_SILENCE_MS = 800.0  # auto-VAD silence_duration_ms host/echo used before


async def _make_bridge():
    import os

    os.environ["SNAIL_VAD_HANGOVER_FRAMES"] = "30"  # pin: measure the 300ms-hangover cut
    conn = FakeConn(HOST_ID)
    sock = FakeSocket()
    bridge = MultiAgentBridge(
        socket=sock, pools={"main": FakePool({HOST_ID: conn})}, agent_ids=[HOST_ID]
    )
    await bridge._setup()
    return bridge, conn


def _sine_frame(codec, phase):
    t = np.arange(phase, phase + 480)
    return codec.encode((8000 * np.sin(2 * np.pi * 440 * t / 48000)).astype(np.int16))


def _silence_frame(codec):
    return codec.encode(np.zeros(480, dtype=np.int16))


async def _feed(bridge, opus_bytes):
    """One mic frame through the real bridge endpointing path (mirrors _pump_client)."""
    for f in bridge._pipeline.on_client_audio(opus_bytes):
        ev = bridge._vad.push(f)
        if ev is VadEvent.START:
            bridge._in_speech = True
        elif ev is VadEvent.END:
            await bridge._end_speech()
    await bridge._forward_drained()


@pytest.mark.asyncio
async def test_endpoint_latency_at_least_halves_vs_800ms_baseline(capsys):
    bridge, conn = await _make_bridge()
    codec = OpusCodec()

    # warm-up + floor seed
    for _ in range(15):
        await _feed(bridge, _silence_frame(codec))
    # speech (drive START)
    phase = 0
    for _ in range(25):
        await _feed(bridge, _sine_frame(codec, phase))
        phase += 480
    assert RealtimeControl.ACTIVITY_START in conn.realtime_controls

    # feed silence one frame at a time; count frames until turn-end is declared
    frames_to_end = 0
    for _ in range(200):
        await _feed(bridge, _silence_frame(codec))
        frames_to_end += 1
        if RealtimeControl.ACTIVITY_END in conn.realtime_controls:
            break

    endpoint_ms = frames_to_end * FRAME_MS
    reduction = (BASELINE_SILENCE_MS - endpoint_ms) / BASELINE_SILENCE_MS

    with capsys.disabled():
        print(
            f"\n  end-of-speech→turn-end: baseline(auto-VAD) {BASELINE_SILENCE_MS:.0f}ms"
            f"  →  endpointing {endpoint_ms:.0f}ms"
            f"  ({reduction * 100:.0f}% reduction)"
        )

    assert RealtimeControl.ACTIVITY_END in conn.realtime_controls
    assert reduction >= 0.50, f"only {reduction*100:.0f}% cut (endpoint {endpoint_ms}ms)"
