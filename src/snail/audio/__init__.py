"""Audio plane primitives (see docs 11-audio).

Phase-1 core: the interior frame and its refcounted pool. The pipeline (opus/RNNoise/
resample), fan-out bus, gates and sinks build on these.
"""

from .clean import (
    FRAME_LEN,
    AudioCleaner,
    DenoiseBackend,
    NullCleaner,
    Rechunker,
    RNNoiseCleaner,
)
from .codec import AudioCodec, PcmCodec
from .fanout import FanoutBus, OverflowPolicy, Subscriber, SubscriberRing
from .frame import AudioFrame, AudioSource, FrameFlags
from .jitter import JitterBuffer, JitterState
from .pipeline import INTERIOR_RATE, AudioPipeline
from .pool import FramePool, FramePoolExhausted
from .resample import LazyResampler, ResampleBackend, Resampler

__all__ = [
    "AudioFrame",
    "AudioSource",
    "FrameFlags",
    "FramePool",
    "FramePoolExhausted",
    "FanoutBus",
    "OverflowPolicy",
    "Subscriber",
    "SubscriberRing",
    "AudioCleaner",
    "DenoiseBackend",
    "NullCleaner",
    "Rechunker",
    "RNNoiseCleaner",
    "FRAME_LEN",
    "LazyResampler",
    "ResampleBackend",
    "Resampler",
    "AudioCodec",
    "PcmCodec",
    "JitterBuffer",
    "JitterState",
    "AudioPipeline",
    "INTERIOR_RATE",
]
