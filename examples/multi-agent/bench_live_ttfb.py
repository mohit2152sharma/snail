"""Live Gemini TTFB benchmark: auto-VAD (800ms) vs bridge manual-VAD (hangover).

Measures the real per-turn *end-of-speech → first-audio-byte* latency against a live
Gemini Live model, A/B:

* **auto** — Gemini automatic VAD, ``silence_duration_ms=800`` (the host/echo baseline).
* **manual** — automatic VAD off; we drive ``activity_start``/``activity_end`` exactly as
  ``ManualVadGeminiAdapter`` + the bridge's ``EnergyVad`` do, ending the turn a fixed
  ``HANGOVER_MS`` after the last speech sample.

Both stream the same real utterance (macOS ``say``) realtime-paced, timestamp the last
speech sample, and record when the first response-audio byte arrives. Reports median of
N trials and the reduction.

Run:  set -a && . examples/multi-agent/.env && set +a &&
      .venv/bin/python examples/multi-agent/bench_live_ttfb.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
import wave

import numpy as np
import soxr
from google import genai
from google.genai import types

MODEL = os.environ.get("SNAIL_BENCH_MODEL", "gemini-2.5-flash-native-audio-latest")
UTT_WAV = os.environ.get("SNAIL_BENCH_WAV", "/tmp/utt_16k.wav")
RATE = 16000
CHUNK_MS = 20
CHUNK = RATE * CHUNK_MS // 1000  # samples per 20ms chunk
BASELINE_SILENCE_MS = 800
HANGOVER_MS = int(os.environ.get("SNAIL_BENCH_HANGOVER_MS", "300"))
TRAIL_SILENCE_MS = 1600  # trailing silence streamed so auto-VAD's 800ms can elapse
TRIALS = int(os.environ.get("SNAIL_BENCH_TRIALS", "3"))
MODES = os.environ.get("SNAIL_BENCH_MODES", "auto,manual").split(",")


def load_utterance() -> np.ndarray:
    w = wave.open(UTT_WAV, "rb")
    raw = w.readframes(w.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16)
    if w.getnchannels() > 1:
        pcm = pcm[:: w.getnchannels()]
    if w.getframerate() != RATE:
        pcm = soxr.resample(pcm.astype(np.float32), w.getframerate(), RATE).astype(np.int16)
    return pcm


def chunks_of(pcm: np.ndarray):
    for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
        yield pcm[i : i + CHUNK]


def _blob(samples: np.ndarray) -> types.Blob:
    return types.Blob(data=samples.astype(np.int16).tobytes(), mime_type=f"audio/pcm;rate={RATE}")


def _has_audio(msg) -> bool:
    sc = getattr(msg, "server_content", None)
    if sc is None or sc.model_turn is None:
        return False
    return any(p.inline_data and p.inline_data.data for p in (sc.model_turn.parts or []))


def _turn_complete(msg) -> bool:
    sc = getattr(msg, "server_content", None)
    return bool(sc and getattr(sc, "turn_complete", None))


async def _trial(client: genai.Client, cfg: types.LiveConnectConfig, *, manual: bool,
                 utterance: np.ndarray) -> float | None:
    state = {"t_end": None, "t_first": None}

    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        async def receiver():
            async for m in s.receive():
                if state["t_first"] is None and _has_audio(m):
                    state["t_first"] = time.monotonic()
                if _turn_complete(m):
                    return

        rx = asyncio.ensure_future(receiver())

        if manual:
            await s.send_realtime_input(activity_start=types.ActivityStart())
        # stream the utterance, realtime-paced
        for ch in chunks_of(utterance):
            await s.send_realtime_input(audio=_blob(ch))
            await asyncio.sleep(CHUNK_MS / 1000)
        state["t_end"] = time.monotonic()  # last speech sample sent

        silence = np.zeros(CHUNK, dtype=np.int16)
        if manual:
            # hold the turn open for the hangover (streaming silence), then end it
            held = 0
            while held < HANGOVER_MS:
                await s.send_realtime_input(audio=_blob(silence))
                await asyncio.sleep(CHUNK_MS / 1000)
                held += CHUNK_MS
            await s.send_realtime_input(activity_end=types.ActivityEnd())
        else:
            # keep streaming silence so the model's 800ms silence timer elapses
            streamed = 0
            while streamed < TRAIL_SILENCE_MS and state["t_first"] is None:
                await s.send_realtime_input(audio=_blob(silence))
                await asyncio.sleep(CHUNK_MS / 1000)
                streamed += CHUNK_MS

        try:
            await asyncio.wait_for(rx, timeout=15)
        except asyncio.TimeoutError:
            rx.cancel()

    if state["t_first"] is None or state["t_end"] is None:
        return None
    return (state["t_first"] - state["t_end"]) * 1000.0


def _auto_cfg() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=300,
                silence_duration_ms=BASELINE_SILENCE_MS,
            )
        ),
    )


def _manual_cfg() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
    )


async def main() -> None:
    key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=key)
    utterance = load_utterance()
    print(f"model={MODEL}  utterance={len(utterance)/RATE*1000:.0f}ms  trials={TRIALS}\n")

    results: dict[str, list[float]] = {"auto": [], "manual": []}
    all_modes = (("auto", _auto_cfg(), False), ("manual", _manual_cfg(), True))
    for mode, cfg, manual in (m for m in all_modes if m[0] in MODES):
        if mode == "manual":
            print(f"(manual hangover={HANGOVER_MS}ms)")
        for i in range(TRIALS):
            try:
                ms = await _trial(client, cfg, manual=manual, utterance=utterance)
            except Exception as e:  # noqa: BLE001
                print(f"  {mode} trial {i}: ERROR {type(e).__name__}: {str(e)[:120]}")
                continue
            if ms is not None:
                results[mode].append(ms)
                print(f"  {mode} trial {i}: end-of-speech→first-byte = {ms:.0f} ms")
            else:
                print(f"  {mode} trial {i}: no response audio captured")
            await asyncio.sleep(1.0)

    print()
    if results["auto"] and results["manual"]:
        a = statistics.median(results["auto"])
        m = statistics.median(results["manual"])
        print(f"MEDIAN  auto(800ms VAD)={a:.0f}ms   manual(hangover {HANGOVER_MS}ms)={m:.0f}ms")
        print(f"REDUCTION = {(a - m) / a * 100:.0f}%  ({a - m:.0f} ms saved per turn)")
    else:
        print("insufficient data:", {k: len(v) for k, v in results.items()})


if __name__ == "__main__":
    asyncio.run(main())
