"""Integration test: MultiAgentBridge server-side VAD endpointing.

No live Gemini. A fake WebSocket feeds opus-encoded mic frames through the real
``_pump_client`` path; a fake connection records the activity markers the bridge emits.
Asserts a silence→speech→silence arc is bracketed by exactly one ACTIVITY_START … audio
… ACTIVITY_END, and that a mid-turn pause shorter than the hangover emits no END.
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
from snail.vendor import RealtimeControl, UserSpeechEnd, UserSpeechStart  # noqa: E402


class FakeCaps:
    input_sample_rate = 16000
    output_sample_rate = 24000


class FakeAdapter:
    capabilities = FakeCaps()
    manual_activity = True  # exercise the manual-VAD marker path

    def parse_event(self, raw):
        return []


class FakeConn:
    def __init__(self, cid):
        self._id = cid
        self.adapter = FakeAdapter()
        self.realtime_controls: list = []
        self.realtime_audio_chunks: list = []

    @property
    def id(self):
        return self._id

    def activate(self):
        pass

    async def send_realtime(self, chunk):
        self.realtime_audio_chunks.append(chunk)

    async def send_realtime_control(self, control):
        self.realtime_controls.append(control)

    async def send_turns(self, items, *, complete):
        pass

    async def send_tool_result(self, payload):
        pass


class FakePool:
    def __init__(self, conns):
        self._conns = conns

    async def acquire(self, spec):
        return self._conns[spec.id]

    async def release(self, conn):
        pass


class FakeSocket:
    def __init__(self):
        self.inbox: list = []
        self.sent_text: list = []
        self.sent_bytes: list = []

    async def accept(self):
        pass

    async def receive(self):
        if self.inbox:
            return self.inbox.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)


def _arc(codec, segments):
    """Build opus-encoded 480-sample frames for a sequence of ("sil"|"speech", n_frames).

    Speech is a continuous 440Hz sine (sustained AC energy — opus preserves it, unlike a
    constant-DC block which decays); silence is digital zero. Phase is continuous across
    the whole arc so segment joins don't inject clicks.
    """
    total = sum(n for _, n in segments) * 480
    t = np.arange(total)
    sine = (8000 * np.sin(2 * np.pi * 440 * t / 48000)).astype(np.int16)
    pcm = np.zeros(total, dtype=np.int16)
    pos = 0
    for kind, n in segments:
        span = n * 480
        if kind == "speech":
            pcm[pos : pos + span] = sine[pos : pos + span]
        pos += span
    return [codec.encode(pcm[i : i + 480]) for i in range(0, total, 480)]


async def _make_bridge(hangover_frames: int = 30):
    # Pin the hangover so these tests are independent of the shipped (aggressive) default.
    import os

    os.environ["SNAIL_VAD_HANGOVER_FRAMES"] = str(hangover_frames)
    conn = FakeConn(HOST_ID)
    sock = FakeSocket()
    bridge = MultiAgentBridge(
        socket=sock, pools={"main": FakePool({HOST_ID: conn})}, agent_ids=[HOST_ID]
    )
    await bridge._setup()
    return bridge, sock, conn


@pytest.mark.asyncio
async def test_endpointing_brackets_audio_with_markers():
    bridge, sock, conn = await _make_bridge()
    seq = _arc(
        OpusCodec(),
        [("sil", 15), ("speech", 22), ("sil", 35)],  # warm-up · START · >hangover → END
    )
    sock.inbox = [{"type": "x", "bytes": b} for b in seq]
    await bridge._pump_client()

    ctrls = conn.realtime_controls
    assert ctrls.count(RealtimeControl.ACTIVITY_START) == 1
    assert ctrls.count(RealtimeControl.ACTIVITY_END) == 1
    assert ctrls.index(RealtimeControl.ACTIVITY_START) < ctrls.index(
        RealtimeControl.ACTIVITY_END
    )
    assert conn.realtime_audio_chunks  # audio forwarded in-speech


@pytest.mark.asyncio
async def test_brief_pause_does_not_end_turn():
    bridge, sock, conn = await _make_bridge()
    seq = _arc(
        OpusCodec(),
        [("sil", 15), ("speech", 22), ("sil", 10), ("speech", 22), ("sil", 5)],
    )  # pause of 10 < hangover(30) → no END
    sock.inbox = [{"type": "x", "bytes": b} for b in seq]
    await bridge._pump_client()

    assert RealtimeControl.ACTIVITY_START in conn.realtime_controls
    assert RealtimeControl.ACTIVITY_END not in conn.realtime_controls


@pytest.mark.asyncio
async def test_ttfb_armed_at_end_speech():
    bridge, sock, conn = await _make_bridge()
    seq = _arc(OpusCodec(), [("sil", 15), ("speech", 22), ("sil", 35)])
    sock.inbox = [{"type": "x", "bytes": b} for b in seq]
    await bridge._pump_client()
    assert bridge._ttfb_pending is True
    assert bridge._ttfb_t0 is not None


@pytest.mark.asyncio
async def test_auto_vad_ignores_the_local_endpointer():
    """Under vendor VAD the local VAD must not stamp the TTFB clock.

    It endpoints on a different rule than the one that actually ended the turn — at the
    shipped 10ms hangover it fires on any inter-word gap, freezing t0 seconds before the
    user stopped. Live logs showed 2.4-6.0s of inflation from exactly this.
    """
    bridge, sock, conn = await _make_bridge()
    conn.adapter.manual_activity = False
    seq = _arc(OpusCodec(), [("sil", 15), ("speech", 22), ("sil", 35)])
    sock.inbox = [{"type": "x", "bytes": b} for b in seq]
    await bridge._pump_client()

    assert bridge._ttfb_pending is False
    assert bridge._ttfb_t0 is None
    assert not conn.realtime_controls  # and no markers into an auto-VAD session
    assert "speech_end" not in "".join(sock.sent_text)


@pytest.mark.asyncio
async def test_vendor_speech_end_arms_ttfb():
    """The vendor's own boundary is what arms the clock under auto VAD."""
    bridge, sock, conn = await _make_bridge()
    conn.adapter.manual_activity = False

    on_msg = bridge._make_on_msg(HOST_ID)
    conn.adapter.parse_event = lambda raw: [UserSpeechStart()]
    await on_msg(object())
    assert bridge._ttfb_pending is False  # start alone arms nothing

    conn.adapter.parse_event = lambda raw: [UserSpeechEnd()]
    await on_msg(object())
    assert bridge._ttfb_pending is True
    assert bridge._ttfb_t0 is not None

    sent = "".join(sock.sent_text)
    assert "speech_start" in sent and "speech_end" in sent
