import numpy as np

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
    run(vad, silence(10))  # seed floor
    evs = run(vad, speech(3))
    assert evs[:2] == [VadEvent.NONE, VadEvent.NONE]
    assert evs[2] is VadEvent.START
    assert vad.state is VadState.SPEECH


def test_end_fires_after_hangover():
    vad = EnergyVad(warmup_frames=5, start_frames=3, hangover_frames=10)
    run(vad, silence(10))
    run(vad, speech(5))
    evs = run(vad, silence(10))  # 10 silent frames = hangover
    assert evs[:9] == [VadEvent.NONE] * 9
    assert evs[9] is VadEvent.END
    assert vad.state is VadState.SILENCE


def test_brief_pause_does_not_end_turn():
    # anti-barge-in: a pause shorter than hangover keeps one continuous turn
    vad = EnergyVad(warmup_frames=5, start_frames=3, hangover_frames=10)
    run(vad, silence(10))
    run(vad, speech(5))
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
    run(vad, silence(10))
    run(vad, speech(5))
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
