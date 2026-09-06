"""drain() fast-path for 48k subscribers returns the exact bytes (no extra copy)."""

from __future__ import annotations

import numpy as np

from snail.audio.frame import AudioSource

from test_pipeline_returns_frames import _pipeline


def test_drain_returns_same_bytes_for_48k_subscriber():
    p = _pipeline()
    p.attach_consumer("a", source=AudioSource.USER_RAW, target_rate=48000, depth=8)
    p.on_client_audio(np.full(480, 1234, dtype=np.int16).tobytes())
    out = p.drain()
    assert "a" in out and len(out["a"]) == 1
    assert np.frombuffer(out["a"][0], dtype=np.int16).tolist() == [1234] * 480
