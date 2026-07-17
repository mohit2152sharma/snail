# Server-VAD Endpointing + Hot-Path Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Halve per-turn *end-of-speech → first-byte* latency by replacing Gemini's flat 800ms auto-VAD with a server-side energy VAD + hangover (manual activity markers), and keep TTFB low *and flat under load* by cutting per-frame CPU and event-loop blocking.

**Architecture:** A new pure `EnergyVad` primitive in `src/snail/audio` runs on the decoded 48k mic frames inside `MultiAgentBridge`. On speech end (after a ~300ms hangover) the bridge sends `ACTIVITY_END`, so Gemini (in manual-activity mode via a new `ManualVadGeminiAdapter`) ends the turn ~500ms sooner without cutting the user off. A second workstream removes duplicate parsing, coalesces per-frame sends, preallocates hot-path buffers, and adds loop-lag + load benchmarks.

**Tech Stack:** Python 3.14, asyncio, numpy, google-genai (Gemini Live), soxr, opuslib, pyrnnoise; pytest + pytest-asyncio. Frontend: vanilla JS + vitest (already done — not in this plan).

## Global Constraints

- Python `>=3.14`; do not lower the floor.
- `EnergyVad` lives in `src/snail/audio/vad.py` (core primitive) — pure, no I/O, no network.
- VAD interior frame is **480 samples, int16, mono, 48kHz** (matches `FRAME_LEN`).
- Manual-VAD scope is the **`main` pool only** (host + echo). The **translate** agent keeps its current adapter/auto-VAD — do not touch it.
- Workstream 2 is **measure-first**: land the loop-lag probe and benchmarks before/with each optimization; keep optimizations behind proven hot spots.
- Preserve the frame ownership-transfer / pool-release contracts in `AudioPipeline`.
- All existing suites (210 py / 17 fe) must stay green.
- Use `time.monotonic()` for timing. Do not add per-frame `print`; use the `multiagent` logger.

---

## File Structure

- **Create** `src/snail/audio/vad.py` — `EnergyVad`, `VadEvent`, `VadState`. One responsibility: energy endpointing state machine.
- **Create** `src/snail/util/looplag.py` — `LoopLagProbe`. One responsibility: measure asyncio loop scheduling delay.
- **Modify** `src/snail/audio/__init__.py` — export `EnergyVad`, `VadEvent`, `VadState`.
- **Modify** `src/snail/audio/pipeline.py` — `on_client_audio` returns the published 48k RAW frames; reduce per-frame allocation in `drain`.
- **Modify** `src/snail/session/session.py` — add `on_events(list[ParsedEvent])`; make `on_vendor_raw` delegate to it (single parse).
- **Modify** `examples/multi-agent/backend/adapter.py` — add `ManualVadGeminiAdapter`.
- **Modify** `examples/multi-agent/backend/bridge.py` — wire `EnergyVad`, pre-roll, activity markers, VAD-based TTFB `t0`, coalesced sends, loop-lag stat.
- **Modify** `examples/multi-agent/backend/app.py` — use `ManualVadGeminiAdapter` for the main pool.
- **Create** tests: `tests/test_energy_vad.py`, `tests/test_looplag.py`, `tests/test_pipeline_returns_frames.py`, `tests/test_manual_vad_adapter.py`, `tests/test_bridge_endpointing.py`, `tests/test_session_on_events.py`, `tests/bench/test_hotpath_bench.py`, `tests/bench/test_load_scale.py`.

---

## Task 1: `EnergyVad` core primitive

**Files:**
- Create: `src/snail/audio/vad.py`
- Test: `tests/test_energy_vad.py`
- Modify: `src/snail/audio/__init__.py`

**Interfaces:**
- Produces:
  - `class VadState(enum.Enum): SILENCE; SPEECH`
  - `class VadEvent(enum.Enum): NONE; START; END`
  - `EnergyVad(*, frame_size=480, start_frames=3, hangover_frames=30, margin=3.0, alpha=0.05, warmup_frames=10)`
  - `EnergyVad.push(frame: np.ndarray) -> VadEvent` — one 480-sample int16 frame → transition
  - `EnergyVad.reset() -> None` — back to `SILENCE` (keeps floor estimate)
  - `EnergyVad.state -> VadState`, `EnergyVad.stats -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_energy_vad.py
import numpy as np
import pytest
from snail.audio.vad import EnergyVad, VadEvent, VadState

FRAME = 480

def silence(n, level=5):
    # low-level room tone, deterministic
    return [np.full(FRAME, level, dtype=np.int16) for _ in range(n)]

def speech(n, level=8000):
    return [np.full(FRAME, level, dtype=np.int16) for _ in range(n)]

def run(vad, frames):
    return [vad.push(f) for f in frames]

def test_silence_only_never_starts():
    vad = EnergyVad(warmup_frames=5)
    evs = run(vad, silence(50))
    assert all(e is VadEvent.NONE for e in evs)
    assert vad.state is VadState.SILENCE

def test_start_fires_after_start_frames():
    vad = EnergyVad(warmup_frames=5, start_frames=3)
    run(vad, silence(10))               # seed floor
    evs = run(vad, speech(3))
    assert evs[:2] == [VadEvent.NONE, VadEvent.NONE]
    assert evs[2] is VadEvent.START
    assert vad.state is VadState.SPEECH

def test_end_fires_after_hangover():
    vad = EnergyVad(warmup_frames=5, start_frames=3, hangover_frames=10)
    run(vad, silence(10)); run(vad, speech(5))
    evs = run(vad, silence(10))         # 10 silent frames = hangover
    assert evs[:9] == [VadEvent.NONE] * 9
    assert evs[9] is VadEvent.END
    assert vad.state is VadState.SILENCE

def test_brief_pause_does_not_end_turn():
    # anti-barge-in: a pause shorter than hangover keeps one continuous turn
    vad = EnergyVad(warmup_frames=5, start_frames=3, hangover_frames=10)
    run(vad, silence(10)); run(vad, speech(5))
    evs = run(vad, silence(9)) + run(vad, speech(5)) + run(vad, silence(9))
    assert VadEvent.END not in evs
    assert vad.state is VadState.SPEECH

def test_single_frame_blip_debounced():
    vad = EnergyVad(warmup_frames=5, start_frames=3)
    run(vad, silence(10))
    evs = run(vad, speech(2)) + run(vad, silence(5))
    assert VadEvent.START not in evs

def test_reset_returns_to_silence_keeps_floor():
    vad = EnergyVad(warmup_frames=5, start_frames=3, hangover_frames=10)
    run(vad, silence(10)); run(vad, speech(5))
    assert vad.state is VadState.SPEECH
    floor_before = vad.stats["floor"]
    vad.reset()
    assert vad.state is VadState.SILENCE
    assert vad.stats["floor"] == floor_before

def test_adaptive_floor_tracks_rising_noise():
    # rising room noise below margin must not trigger START
    vad = EnergyVad(warmup_frames=5, start_frames=3, margin=3.0, alpha=0.2)
    frames = [np.full(FRAME, 100 + i * 5, dtype=np.int16) for i in range(60)]
    evs = run(vad, frames)
    assert VadEvent.START not in evs
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_energy_vad.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'snail.audio.vad'`

- [ ] **Step 3: Implement `EnergyVad`**

```python
# src/snail/audio/vad.py
"""EnergyVad — energy-based speech endpointing with hangover (server-side VAD).

A pure, I/O-free state machine that classifies one interior audio frame at a time
(480 samples, int16, mono, 48kHz) as speech or silence and emits turn boundaries.
Unlike Gemini's flat trailing-silence timer, the **hangover** holds a turn open through
brief mid-utterance pauses (< ``hangover_frames``), so end-of-speech can be declared
quickly (~300ms) without cutting the user off — the anti-barge-in property.

Threshold is an **adaptive noise floor**: an EMA of frame RMS updated only while in
``SILENCE`` (so it tracks room tone but never ramps up to the speaker's own voice). A
frame is "voiced" when ``rms > floor * margin``. A short warm-up seeds the floor and
suppresses spurious starts. State: ``SILENCE ⇄ SPEECH``; one transition per ``push``.
"""

from __future__ import annotations

import enum

import numpy as np

FRAME_LEN = 480  # 10ms @ 48kHz mono (matches audio interior)


class VadState(enum.Enum):
    SILENCE = "silence"
    SPEECH = "speech"


class VadEvent(enum.Enum):
    NONE = "none"
    START = "start"
    END = "end"


class EnergyVad:
    """Adaptive-threshold energy VAD with start-debounce + hangover endpointing."""

    __slots__ = (
        "_frame", "_start_frames", "_hangover", "_margin", "_alpha", "_warmup",
        "_floor", "_seen", "_state", "_above", "_below", "_starts", "_ends",
    )

    def __init__(
        self,
        *,
        frame_size: int = FRAME_LEN,
        start_frames: int = 3,
        hangover_frames: int = 30,
        margin: float = 3.0,
        alpha: float = 0.05,
        warmup_frames: int = 10,
    ) -> None:
        if frame_size < 1:
            raise ValueError("frame_size must be >= 1")
        self._frame = frame_size
        self._start_frames = max(1, start_frames)
        self._hangover = max(1, hangover_frames)
        self._margin = margin
        self._alpha = alpha
        self._warmup = warmup_frames
        self._floor = 0.0
        self._seen = 0
        self._state = VadState.SILENCE
        self._above = 0
        self._below = 0
        self._starts = 0
        self._ends = 0

    def push(self, frame: np.ndarray) -> VadEvent:
        """Classify one frame → transition. Call once per interior frame in order."""
        rms = self._rms(frame)
        self._seen += 1
        warming = self._seen <= self._warmup
        # Seed on first frame; afterwards track room tone only while not speaking.
        if self._floor == 0.0:
            self._floor = rms
        elif self._state is VadState.SILENCE:
            self._floor = (1.0 - self._alpha) * self._floor + self._alpha * rms
        voiced = (not warming) and rms > self._floor * self._margin

        if self._state is VadState.SILENCE:
            if voiced:
                self._above += 1
                if self._above >= self._start_frames:
                    self._state = VadState.SPEECH
                    self._above = 0
                    self._below = 0
                    self._starts += 1
                    return VadEvent.START
            else:
                self._above = 0
            return VadEvent.NONE

        # SPEECH
        if not voiced:
            self._below += 1
            if self._below >= self._hangover:
                self._state = VadState.SILENCE
                self._below = 0
                self._above = 0
                self._ends += 1
                return VadEvent.END
        else:
            self._below = 0
        return VadEvent.NONE

    def reset(self) -> None:
        """Drop any in-flight speech back to SILENCE (keeps the floor estimate)."""
        self._state = VadState.SILENCE
        self._above = 0
        self._below = 0

    @property
    def state(self) -> VadState:
        return self._state

    @property
    def stats(self) -> dict:
        return {
            "state": self._state.value,
            "floor": self._floor,
            "starts": self._starts,
            "ends": self._ends,
        }

    @staticmethod
    def _rms(frame: np.ndarray) -> float:
        if frame.size == 0:
            return 0.0
        f = frame.astype(np.float32, copy=False)
        return float(np.sqrt(np.mean(f * f, dtype=np.float64)))
```

- [ ] **Step 4: Export from the package**

```python
# src/snail/audio/__init__.py — add to the existing imports and __all__
from .vad import EnergyVad, VadEvent, VadState
# ...and add "EnergyVad", "VadEvent", "VadState" to __all__
```

Run: `.venv/bin/python -c "from snail.audio import EnergyVad, VadEvent, VadState; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_energy_vad.py -q`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/snail/audio/vad.py src/snail/audio/__init__.py tests/test_energy_vad.py
git commit -m "feat(audio): EnergyVad energy endpointing primitive with hangover"
```

---

## Task 2: `AudioPipeline.on_client_audio` returns published frames

**Files:**
- Modify: `src/snail/audio/pipeline.py` (`on_client_audio`)
- Test: `tests/test_pipeline_returns_frames.py`

**Interfaces:**
- Consumes: existing `AudioPipeline` construction.
- Produces: `AudioPipeline.on_client_audio(data: bytes) -> list[np.ndarray]` — the 480-sample 48k RAW frames it published this call (empty list if the input didn't complete a frame). Return value is the bridge's VAD tap; existing callers that ignore the return are unaffected.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline_returns_frames.py
import numpy as np
from snail.audio import AudioPipeline, FanoutBus, FramePool, JitterBuffer, LazyResampler
from snail.audio.codec import PcmCodec
from snail.audio.soxr_backend import SoxrResampleBackend
from snail.router import OutputGate

def _pipeline():
    frames = FramePool(capacity=64, slab_samples=480)
    return AudioPipeline(
        pool=frames, bus=FanoutBus(frames),
        resampler=LazyResampler(SoxrResampleBackend()),
        gate=OutputGate(depth=32), jitter=JitterBuffer(), codec=PcmCodec(),
        client_rate=48000,
    )

def test_on_client_audio_returns_480_frames():
    p = _pipeline()
    # 960 samples of 48k PCM16 == two 480 frames
    pcm = np.zeros(960, dtype=np.int16).tobytes()
    frames = p.on_client_audio(pcm)
    assert isinstance(frames, list)
    assert len(frames) == 2
    assert all(f.shape == (480,) for f in frames)
    assert all(f.dtype == np.int16 for f in frames)
```

> Note: adjust the `_pipeline()` imports to match `tests/`' existing pipeline-construction helper if one exists (grep `AudioPipeline(` in `tests/`). Use `LazyResampler(SoxrResampleBackend())` if a bare `LazyResampler()` is not constructible.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_returns_frames.py -q`
Expected: FAIL — `on_client_audio` returns `None`, `len(None)` raises / assert fails.

- [ ] **Step 3: Modify `on_client_audio` to return the frames**

```python
# src/snail/audio/pipeline.py — replace the body of on_client_audio
def on_client_audio(self, data: bytes) -> list[np.ndarray]:
    """Decode one client media frame, publish to the bus, and return the RAW 48k
    frames published (the bridge feeds these to its endpointing VAD — no re-decode)."""
    samples = self._codec.decode(data)
    at48 = self._resampler.resample(
        samples, from_rate=self._client_rate, to_rate=INTERIOR_RATE
    )
    frames = self._rechunk_raw(at48)
    for frame480 in frames:
        self._publish(frame480, AudioSource.USER_RAW)
    if self._cleaner is not None and self._wants_clean():
        for cleaned in self._cleaner.process(at48):
            self._publish(cleaned, AudioSource.USER_CLEAN)
    return frames
```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `.venv/bin/python -m pytest tests/test_pipeline_returns_frames.py tests/ -q`
Expected: PASS (new test + full suite green)

- [ ] **Step 5: Commit**

```bash
git add src/snail/audio/pipeline.py tests/test_pipeline_returns_frames.py
git commit -m "feat(audio): on_client_audio returns published 48k frames (VAD tap)"
```

---

## Task 3: `ManualVadGeminiAdapter` (auto-VAD off)

**Files:**
- Modify: `examples/multi-agent/backend/adapter.py`
- Test: `tests/test_manual_vad_adapter.py`

**Interfaces:**
- Consumes: `GeminiAdapter`, `google.genai.types`.
- Produces: `ManualVadGeminiAdapter(GeminiAdapter)` — `build_setup(...)` returns a `LiveConnectConfig` with `realtime_input_config.automatic_activity_detection.disabled == True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_manual_vad_adapter.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path("examples/multi-agent").resolve()))
from backend.adapter import ManualVadGeminiAdapter
from snail.vendor import Backend, ResponseModality, SetupParam

def test_manual_vad_disables_automatic_detection():
    a = ManualVadGeminiAdapter(backend=Backend.GEMINI_DEV, model="gemini-2.5-flash-live")
    setup = SetupParam(model="gemini-2.5-flash-live",
                       response_modality=ResponseModality.AUDIO)
    cfg = a.build_setup(setup)
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_manual_vad_adapter.py -q`
Expected: FAIL — `ImportError: cannot import name 'ManualVadGeminiAdapter'`

- [ ] **Step 3: Implement the adapter**

```python
# examples/multi-agent/backend/adapter.py — add below VadGeminiAdapter
class ManualVadGeminiAdapter(GeminiAdapter):
    """GeminiAdapter with automatic VAD OFF — the bridge owns endpointing.

    In manual-activity mode the model does not detect turn boundaries itself; the caller
    must bracket the user's audio with ``activity_start`` / ``activity_end`` markers. The
    ``MultiAgentBridge``'s :class:`~snail.audio.EnergyVad` sends those, so end-of-speech
    is declared after a short hangover instead of Gemini's flat 800ms silence wait.
    """

    def build_setup(self, setup, *, resumption_handle: str | None = None):
        cfg = super().build_setup(setup, resumption_handle=resumption_handle)
        cfg.realtime_input_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        )
        return cfg
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_manual_vad_adapter.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/multi-agent/backend/adapter.py tests/test_manual_vad_adapter.py
git commit -m "feat(examples): ManualVadGeminiAdapter (automatic VAD off)"
```

---

## Task 4: Wire endpointing into the bridge (markers + pre-roll + VAD-based TTFB)

**Files:**
- Modify: `examples/multi-agent/backend/bridge.py`
- Modify: `examples/multi-agent/backend/app.py` (use `ManualVadGeminiAdapter` for main pool)
- Test: `tests/test_bridge_endpointing.py`

**Interfaces:**
- Consumes: `EnergyVad`, `VadEvent` (Task 1); `on_client_audio -> list` (Task 2); `ManualVadGeminiAdapter` (Task 3); `RealtimeControl` from `snail.vendor`.
- Produces: bridge forwards mic audio only between VAD `START`/`END`, bracketed by `ACTIVITY_START`/`ACTIVITY_END`, with a ~200ms pre-roll flush on `START`; TTFB `t0` taken at last speech frame (`END_time − hangover`).

Behaviour contract (what the integration test asserts):
1. A silence→speech→silence arc emits, to the active connection, exactly one
   `ACTIVITY_START`, then ≥1 audio chunk, then exactly one `ACTIVITY_END`, in that order.
2. A mid-turn pause shorter than the hangover emits **no** `ACTIVITY_END`.
3. `vad.reset()` runs on promote.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_bridge_endpointing.py
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path("examples/multi-agent").resolve()))
import numpy as np
import pytest
from backend.bridge import MultiAgentBridge
from snail.vendor import RealtimeControl

# Reuse the repo's existing bridge test harness if present (grep tests/ for a fake
# socket + fake pool the multi-agent bridge tests already use). The assertions below
# describe the contract; adapt the harness wiring to the existing fakes.

@pytest.mark.asyncio
async def test_endpointing_brackets_audio_with_markers(bridge_harness):
    br, active_conn, feed_mic = bridge_harness  # harness: built bridge + capture conn
    # 12 silent frames (seed floor) then 6 speech frames then 35 silent (> hangover)
    await feed_mic(silence_frames=12, speech_frames=6, trailing_silence=35)
    ctrls = active_conn.realtime_controls  # list[RealtimeControl] captured by the fake
    assert ctrls.count(RealtimeControl.ACTIVITY_START) == 1
    assert ctrls.count(RealtimeControl.ACTIVITY_END) == 1
    assert ctrls.index(RealtimeControl.ACTIVITY_START) < ctrls.index(RealtimeControl.ACTIVITY_END)
    assert active_conn.realtime_audio_chunks  # audio was forwarded in-speech

@pytest.mark.asyncio
async def test_brief_pause_no_end_marker(bridge_harness):
    br, active_conn, feed_mic = bridge_harness
    await feed_mic(silence_frames=12, speech_frames=6,
                   pause_frames=10, more_speech_frames=6, trailing_silence=5)  # pause<hangover(30)
    assert RealtimeControl.ACTIVITY_END not in active_conn.realtime_controls
```

> **No `MultiAgentBridge` test harness exists yet** — `tests/test_bridge_pipeline.py`
> covers the *core* `snail.transport.ClientBridge`, not the example bridge. This task must
> **build a new harness**: a fake WebSocket (see the socket/transport fakes in
> `tests/test_transport.py`) and a fake `ConnectionPool`/`AgentConnection` (model on
> `tests/test_connections.py`). Extend the fake `AgentConnection` to record
> `send_realtime_control` calls into `realtime_controls` and `send_realtime` audio into
> `realtime_audio_chunks`. `feed_mic` encodes low-amplitude (silence) and high-amplitude
> (speech) PCM frames through the client leg (48k `PcmCodec`) and drives
> `_pump_client`-equivalent handling. Building this harness is part of Task 4's deliverable
> and is reused by Tasks 7 and 10 — budget for it.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bridge_endpointing.py -q`
Expected: FAIL — no markers recorded (bridge doesn't send activity markers yet).

- [ ] **Step 3: Add imports + VAD state to the bridge**

```python
# examples/multi-agent/backend/bridge.py
# add to imports:
from collections import deque
from snail.audio import EnergyVad, VadEvent
from snail.vendor import (
    Interrupted, MediaChunk, RealtimeControl, ResponseModality, TurnComplete, UserTranscript,
)
```

In `__init__`, replace the TTFB block added earlier with VAD-driven endpointing state:

```python
        # --- endpointing (server-side VAD in manual-activity mode) --------------
        import os as _os
        self._vad = EnergyVad(
            hangover_frames=int(_os.environ.get("SNAIL_VAD_HANGOVER_FRAMES", "30")),
            start_frames=int(_os.environ.get("SNAIL_VAD_START_FRAMES", "3")),
            margin=float(_os.environ.get("SNAIL_VAD_MARGIN", "3.0")),
        )
        self._hangover_s = self._vad_hangover_seconds()
        self._in_speech = False
        self._sent_start = False
        # pre-roll: ~200ms (20 frames) of drained vendor-rate chunks captured while
        # silent, flushed on START so the word onset isn't clipped.
        self._preroll: deque[bytes] = deque(maxlen=20)
        # --- per-turn TTFB instrumentation (t0 = last speech frame) -------------
        self._ttfb_t0: float | None = None
        self._ttfb_pending = False

    def _vad_hangover_seconds(self) -> float:
        import os as _os
        return int(_os.environ.get("SNAIL_VAD_HANGOVER_FRAMES", "30")) * 0.010
```

- [ ] **Step 4: Drive the VAD from the mic and gate forwarding**

Replace `_pump_client`'s audio branch and `_forward_drained`:

```python
    async def _pump_client(self) -> None:
        while True:
            msg = await self._socket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                if not self._muted:
                    frames48 = self._pipeline.on_client_audio(data)
                    for f in frames48:
                        ev = self._vad.push(f)
                        if ev is VadEvent.START:
                            self._in_speech = True
                        elif ev is VadEvent.END:
                            await self._end_speech()
                    await self._forward_drained()
                continue
            text = msg.get("text")
            if text is not None:
                await self._handle_control(text)
                if self._closing:
                    return

    async def _forward_drained(self) -> None:
        active = self._router.active_id
        for cid, chunks in self._pipeline.drain().items():
            conn = self._conns.get(cid)
            if conn is None or cid != active:
                continue
            rate = conn.adapter.capabilities.input_sample_rate
            if not self._in_speech:
                # not speaking: retain as pre-roll, do not forward
                self._preroll.extend(chunks)
                continue
            if not self._sent_start:
                await conn.send_realtime_control(RealtimeControl.ACTIVITY_START)
                self._sent_start = True
                self._ttfb_t0 = None  # (re)armed on END
                # flush pre-roll first so the onset isn't clipped
                for pre in list(self._preroll):
                    await conn.send_realtime(MediaChunk.audio(pre, sample_rate=rate))
                self._preroll.clear()
            n = 0
            for ch in chunks:
                await conn.send_realtime(MediaChunk.audio(ch, sample_rate=rate))
                n += len(ch)
            self._mic_bytes += n
            if self._mic_bytes - self._mic_logged > 96000:
                log.info("mic→%s: %d bytes total", cid, self._mic_bytes)
                self._mic_logged = self._mic_bytes

    async def _end_speech(self) -> None:
        """VAD END: close the user turn with an ACTIVITY_END marker + arm TTFB."""
        self._in_speech = False
        active = self._router.active_id
        conn = self._conns.get(active)
        if conn is not None and self._sent_start:
            await conn.send_realtime_control(RealtimeControl.ACTIVITY_END)
            # t0 = last speech frame ≈ now − hangover (END fires hangover after it)
            self._ttfb_t0 = time.monotonic() - self._hangover_s
            self._ttfb_pending = True
        self._sent_start = False
```

- [ ] **Step 5: Fire TTFB on first audio byte; reset VAD on promote**

Replace the TTFB hook in `_make_on_audio` (the `_ttfb_pending` block stays, unchanged in shape) — it already logs on first audio byte. Then in `_on_promote`, add the reset:

```python
    def _on_promote(self, agent_id: str, needs_flip: bool) -> None:
        self._active_ev[agent_id].set()
        self._pipeline.cut()
        self._vad.reset()              # drop in-flight speech for the new agent
        self._in_speech = False
        self._sent_start = False
        self._preroll.clear()
        log.info("promote → %s", agent_id)
        asyncio.create_task(self._emit(active_agent_changed(agent_id)))
        reanchor = REANCHOR.get(agent_id)
        if reanchor is not None:
            asyncio.create_task(self._reanchor(agent_id, reanchor))
```

Also remove the old `UserTranscript`-based arming in `_make_on_msg` (the `if isinstance(ev, UserTranscript) and ev.is_final:` block) — TTFB is now armed by `_end_speech`. Keep the `TurnComplete` disarm.

- [ ] **Step 6: Point the main pool at the manual adapter**

```python
# examples/multi-agent/backend/app.py:70 — swap the adapter
main_adapter = ManualVadGeminiAdapter(backend=BACKEND, model=MODEL)  # bridge owns VAD
```
And update the import on line 24:
```python
from .adapter import ManualVadGeminiAdapter, TranslateGeminiAdapter
```
(Leave the `VadGeminiAdapter.build_client(...)` calls — `build_client` is a staticmethod inherited unchanged; or switch them to `ManualVadGeminiAdapter.build_client(...)`. Keep whichever class name is imported.)

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -m pytest tests/test_bridge_endpointing.py tests/ -q`
Expected: PASS (endpointing tests + full suite green)

- [ ] **Step 8: Commit**

```bash
git add examples/multi-agent/backend/bridge.py examples/multi-agent/backend/app.py tests/test_bridge_endpointing.py
git commit -m "feat(examples): server-VAD endpointing — activity markers + pre-roll + VAD-based TTFB"
```

---

## Task 5: Loop-lag probe (event-loop saturation signal)

**Files:**
- Create: `src/snail/util/looplag.py`
- Create: `src/snail/util/__init__.py` (if absent)
- Test: `tests/test_looplag.py`

**Interfaces:**
- Produces: `LoopLagProbe(interval_s=0.010)` with `start()`, `async stop()`, `stats -> {"p50_ms","p99_ms","max_ms","samples"}`. Measures actual−scheduled callback delay.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_looplag.py
import asyncio
import pytest
from snail.util.looplag import LoopLagProbe

@pytest.mark.asyncio
async def test_probe_collects_samples_and_reports():
    p = LoopLagProbe(interval_s=0.005)
    p.start()
    await asyncio.sleep(0.1)
    await p.stop()
    s = p.stats
    assert s["samples"] >= 5
    assert s["p50_ms"] >= 0.0
    assert s["max_ms"] >= s["p50_ms"]

@pytest.mark.asyncio
async def test_probe_detects_blocking():
    p = LoopLagProbe(interval_s=0.005)
    p.start()
    time_block_ms = 40
    import time as _t
    _t.sleep(time_block_ms / 1000)  # block the loop
    await asyncio.sleep(0.05)
    await p.stop()
    assert p.stats["max_ms"] >= 20.0  # saturation visible
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_looplag.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the probe**

```python
# src/snail/util/looplag.py
"""LoopLagProbe — measure asyncio event-loop scheduling delay (saturation signal).

Schedules a wake-up every ``interval_s`` and records how late it actually fires
(actual − scheduled). Under a saturated / blocked loop the lag grows; the percentiles
are the direct signal that CPU-bound work on the loop is inflating every await (and thus
per-turn TTFB) under load.
"""

from __future__ import annotations

import asyncio


class LoopLagProbe:
    def __init__(self, *, interval_s: float = 0.010) -> None:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_looplag.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/snail/util/looplag.py src/snail/util/__init__.py tests/test_looplag.py
git commit -m "feat(util): LoopLagProbe — event-loop saturation measurement"
```

---

## Task 6: Single-parse — remove duplicate `parse_event` per message

**Files:**
- Modify: `src/snail/session/session.py` (add `on_events`)
- Modify: `examples/multi-agent/backend/bridge.py` (`_make_on_msg` parses once, passes list)
- Test: `tests/test_session_on_events.py`

**Interfaces:**
- Produces: `Session.on_events(events: list[ParsedEvent]) -> None`; `Session.on_vendor_raw` delegates to it. Bridge parses once and calls `session.on_events(parsed)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_on_events.py
import pytest
from snail.session import Session
# Build a Session with the existing test doubles (grep tests/ for how Session is
# constructed in current session tests — reuse that MockVendorAdapter + fakes).

@pytest.mark.asyncio
async def test_on_events_dispatches_without_reparsing(session_fixture):
    session, adapter_spy = session_fixture  # adapter_spy counts parse_event calls
    from snail.vendor import UserTranscript
    await session.on_events([UserTranscript(text="hi", is_final=True)])
    # on_events must NOT call adapter.parse_event again (already parsed)
    assert adapter_spy.parse_calls == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_on_events.py -q`
Expected: FAIL — `Session` has no `on_events`.

- [ ] **Step 3: Add `on_events`; delegate `on_vendor_raw`**

```python
# src/snail/session/session.py — replace on_vendor_raw and add on_events
    async def on_vendor_raw(self, raw: dict) -> None:
        """Parse one raw vendor message and dispatch its neutral events."""
        await self.on_events(self._adapter.parse_event(raw))

    async def on_events(self, events: list[ParsedEvent]) -> None:
        """Dispatch already-parsed neutral events (caller parsed once)."""
        for ev in events:
            await self.handle_event(ev)
```

- [ ] **Step 4: Bridge parses once, reuses the list**

```python
# examples/multi-agent/backend/bridge.py — _make_on_msg
        async def on_msg(raw) -> None:
            parsed = conn.adapter.parse_event(raw)
            for ev in parsed:
                if isinstance(ev, Interrupted):
                    self._pipeline.cut()
                if self._router.active_id == cid and isinstance(ev, TurnComplete):
                    self._ttfb_pending = False
                j = to_client_json(ev, agent_id=cid)
                if j is not None:
                    txt = j.get("text")
                    if txt is not None:
                        log.info("event %s from %s: %r", j["type"], cid, txt[:80])
                    else:
                        log.info("event %s from %s", j["type"], cid)
                    await self._emit(j)
            await session.on_events(parsed)   # was: session.on_vendor_raw(raw) — no reparse
```

- [ ] **Step 5: Run tests + full suite**

Run: `.venv/bin/python -m pytest tests/test_session_on_events.py tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/snail/session/session.py examples/multi-agent/backend/bridge.py tests/test_session_on_events.py
git commit -m "perf(session): parse each vendor message once (Session.on_events)"
```

---

## Task 7: Coalesce per-frame egress sends

**Files:**
- Modify: `examples/multi-agent/backend/bridge.py` (`_make_on_audio`)
- Test: extend `tests/test_bridge_endpointing.py`

**Interfaces:**
- Produces: `_make_on_audio` drains all ready jittered frames and sends them as **one** `socket.send_bytes` per vendor burst instead of one send per 10ms frame.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bridge_endpointing.py — add
@pytest.mark.asyncio
async def test_egress_coalesces_frames(bridge_harness_with_vendor_audio):
    br, sock_spy, push_vendor = bridge_harness_with_vendor_audio
    await push_vendor(ms=60)  # 6 frames of 24k vendor audio in one burst
    # coalesced: a burst becomes a single send_bytes, not 6
    assert sock_spy.send_bytes_calls == 1
    assert sock_spy.total_bytes > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bridge_endpointing.py::test_egress_coalesces_frames -q`
Expected: FAIL — currently one send per frame (`send_bytes_calls == 6`).

- [ ] **Step 3: Coalesce the drain**

```python
# examples/multi-agent/backend/bridge.py — _make_on_audio inner on_audio
        async def on_audio(pcm: bytes) -> None:
            if self._router.active_id != cid:
                return
            if self._ttfb_pending and self._ttfb_t0 is not None:
                self._ttfb_pending = False
                ttfb_ms = (time.monotonic() - self._ttfb_t0) * 1000.0
                log.info("TTFB %s end-of-speech→first-byte: %.0f ms", cid, ttfb_ms)
            self._pipeline.on_vendor_audio(pcm, vendor_rate=rate)
            parts: list[bytes] = []
            while True:
                frame = self._pipeline.playout(cid)
                if frame is None:
                    break
                parts.append(frame)
            if parts:
                payload = parts[0] if len(parts) == 1 else b"".join(parts)
                self._out_bytes += len(payload)
                await self._socket.send_bytes(payload)
```

> Opus frames are self-delimited per frame on the wire; the client downlink decodes each
> `EncodedAudioChunk` independently. If the frontend expects one WS message == one opus
> frame, gate coalescing behind `client_rate`/codec (PCM interior can always coalesce).
> Verify against `examples/frontend/src/audio/downlink.js` before shipping; if opus needs
> per-frame messages, coalesce only the PcmCodec path. (Downlink currently decodes one
> chunk per `pushFrame`; confirm it tolerates a concatenated payload or keep opus 1:1.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_bridge_endpointing.py tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/multi-agent/backend/bridge.py tests/test_bridge_endpointing.py
git commit -m "perf(examples): coalesce egress frames into one send per burst"
```

---

## Task 8: Reduce per-frame allocation in `AudioPipeline.drain`

**Files:**
- Modify: `src/snail/audio/pipeline.py` (`drain`, add a per-instance scratch)
- Test: `tests/test_pipeline_alloc.py`

**Interfaces:**
- Produces: `drain()` avoids `np.ascontiguousarray(...).tobytes()` allocation when the resample is a no-op (48k subscriber) by using `frame.samples.tobytes()` directly; behaviour unchanged.

- [ ] **Step 1: Write the failing/guarding test**

```python
# tests/test_pipeline_alloc.py
import numpy as np
from tests.test_pipeline_returns_frames import _pipeline  # reuse builder
from snail.audio.frame import AudioSource

def test_drain_returns_same_bytes_for_48k_subscriber():
    p = _pipeline()
    p.attach_consumer("a", source=AudioSource.USER_RAW, target_rate=48000, depth=8)
    p.on_client_audio(np.full(480, 1234, dtype=np.int16).tobytes())
    out = p.drain()
    assert "a" in out and len(out["a"]) == 1
    assert np.frombuffer(out["a"][0], dtype=np.int16).tolist() == [1234] * 480
```

- [ ] **Step 2: Run to verify it passes/fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_alloc.py -q`
Expected: PASS on current code (behaviour guard) — this test locks behaviour before the perf edit.

- [ ] **Step 3: Fast-path the no-resample case**

```python
# src/snail/audio/pipeline.py — drain(): inside the while loop, replace the
# resample+tobytes with a no-op-aware fast path
            while True:
                frame = sub.ring.pop()
                if frame is None:
                    break
                if sub.target_rate == INTERIOR_RATE:
                    chunks.append(frame.samples.tobytes())  # no resample, no extra copy
                else:
                    resampled = self._resampler.resample(
                        frame.samples, from_rate=INTERIOR_RATE, to_rate=sub.target_rate
                    )
                    chunks.append(
                        np.ascontiguousarray(resampled, dtype=np.int16).tobytes()
                    )
                self._pool.release(frame)
```

- [ ] **Step 4: Run tests + full suite**

Run: `.venv/bin/python -m pytest tests/test_pipeline_alloc.py tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/snail/audio/pipeline.py tests/test_pipeline_alloc.py
git commit -m "perf(audio): drain fast-path avoids extra copy for 48k subscribers"
```

---

## Task 9: Codec/resampler GIL audit + conditional offload (measure-gated)

**Files:**
- Create: `tests/bench/test_codec_gil.py` (measurement)
- Modify (conditional): `src/snail/audio/opus_codec.py` / `resample.py` only if the audit proves a stall.

**Interfaces:**
- Produces: a measurement that reports whether opus/soxr/rnnoise release the GIL under concurrency; a documented decision. Offload is applied **only** if the audit shows loop stalls.

- [ ] **Step 1: Measure GIL-release under concurrency**

```python
# tests/bench/test_codec_gil.py
import asyncio, time
import numpy as np
import pytest
from snail.audio.opus_codec import OpusCodec

@pytest.mark.asyncio
async def test_opus_encode_does_not_starve_loop():
    codec = OpusCodec()
    frame = np.zeros(480, dtype=np.int16)
    lags = []
    async def probe():
        loop = asyncio.get_running_loop()
        nxt = loop.time() + 0.005
        for _ in range(60):
            await asyncio.sleep(max(0, nxt - loop.time()))
            lags.append((loop.time() - nxt) * 1000)
            nxt += 0.005
    async def encode_load():
        for _ in range(2000):
            codec.encode(frame)
            if _ % 50 == 0:
                await asyncio.sleep(0)  # yield points
    await asyncio.gather(probe(), encode_load())
    p99 = sorted(lags)[int(0.99 * len(lags))]
    # Report; do not hard-fail — this is a measurement to inform the offload decision.
    print(f"opus encode loop p99 lag: {p99:.1f} ms")
    assert p99 < 50.0  # generous ceiling; tighten after baseline
```

- [ ] **Step 2: Run and record the baseline**

Run: `.venv/bin/python -m pytest tests/bench/test_codec_gil.py -q -s`
Record the printed p99 for opus, then repeat the pattern for `SoxrResampleBackend.resample` and `RNNoiseCleaner.process`.

- [ ] **Step 3: Decide + (conditionally) offload**

If a codec's p99 loop-lag is high (loop-starving), wrap its hot call in a bounded executor. **Only then** apply:

```python
# pattern — do NOT apply unless Step 2 proved a stall for that codec
import asyncio, concurrent.futures
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)  # thread OK iff GIL released
async def encode_off_loop(codec, frame):
    return await asyncio.get_running_loop().run_in_executor(_POOL, codec.encode, frame)
```

If the extension does **not** release the GIL, a thread pool won't help — document that a
process pool or a native batch API is the follow-up, and leave the synchronous call
in place (offloading to a GIL-bound thread only adds overhead). Record the decision in
the plan's Task 9 checkbox notes.

- [ ] **Step 4: Commit the measurement (and any proven offload)**

```bash
git add tests/bench/test_codec_gil.py
git commit -m "bench(audio): codec GIL-release audit (offload decision input)"
```

---

## Task 10: Micro + load benchmarks (scale regression guard)

**Files:**
- Create: `tests/bench/test_hotpath_bench.py`
- Create: `tests/bench/test_load_scale.py`

**Interfaces:**
- Consumes: `LoopLagProbe` (Task 5), pipeline, bridge fakes.
- Produces: a per-frame cost micro-benchmark and an N-session load test reporting loop-lag + TTFB percentiles vs N.

- [ ] **Step 1: Micro-benchmark the hot path**

```python
# tests/bench/test_hotpath_bench.py
import time
import numpy as np
from tests.test_pipeline_returns_frames import _pipeline
from snail.audio.frame import AudioSource

def test_ingress_egress_per_frame_cost():
    p = _pipeline()
    p.attach_consumer("a", source=AudioSource.USER_RAW, target_rate=16000, depth=64)
    pcm = np.random.randint(-1000, 1000, 480, dtype=np.int16).tobytes()
    N = 2000
    t0 = time.perf_counter()
    for _ in range(N):
        p.on_client_audio(pcm)
        p.drain()
    per_frame_us = (time.perf_counter() - t0) / N * 1e6
    print(f"ingress per-frame: {per_frame_us:.1f} us")
    assert per_frame_us < 500.0  # generous guard; tighten to measured baseline*1.5
```

- [ ] **Step 2: Load test — TTFB stays flat as N grows**

```python
# tests/bench/test_load_scale.py
import asyncio
import pytest
from snail.util.looplag import LoopLagProbe

@pytest.mark.asyncio
@pytest.mark.parametrize("n_sessions", [1, 10, 50])
async def test_loop_lag_bounded_under_n_sessions(n_sessions, load_harness):
    # load_harness spins up n fake bridge sessions each pumping 20ms audio ticks
    probe = LoopLagProbe(interval_s=0.010)
    probe.start()
    sessions = [load_harness() for _ in range(n_sessions)]
    await asyncio.gather(*(s.run_for(seconds=1.0) for s in sessions))
    await probe.stop()
    stats = probe.stats
    print(f"N={n_sessions} loop p99={stats['p99_ms']:.1f}ms")
    # p99 loop lag must stay bounded — the flat-under-load success criterion
    assert stats["p99_ms"] < 25.0
```

> `load_harness` builds a minimal in-process session that drives the ingress→forward→egress
> path against fake transports at real-time-ish cadence (a 20ms `asyncio.sleep` tick).
> Reuse the bridge fakes from Task 4. Mark these `@pytest.mark.bench` and exclude from the
> default `-q` run if they're slow (add a `bench` marker to `pyproject.toml` `[tool.pytest]`).

- [ ] **Step 3: Run benchmarks**

Run: `.venv/bin/python -m pytest tests/bench/ -q -s`
Expected: PASS; record printed per-frame cost + per-N loop p99 as the baseline.

- [ ] **Step 4: Commit**

```bash
git add tests/bench/test_hotpath_bench.py tests/bench/test_load_scale.py pyproject.toml
git commit -m "bench: hot-path per-frame cost + N-session loop-lag scale guard"
```

---

## Final verification

- [ ] Run the whole suite: `.venv/bin/python -m pytest tests/ -q` → all green.
- [ ] Frontend unchanged this plan; confirm `examples/frontend` vitest still green.
- [ ] Manually sanity-check env knobs: `SNAIL_VAD_HANGOVER_FRAMES`, `SNAIL_VAD_START_FRAMES`, `SNAIL_VAD_MARGIN`.
- [ ] With a live key (out-of-band), watch logs for `TTFB … end-of-speech→first-byte` and confirm the median drops ~500ms vs the 800ms-silence baseline.
