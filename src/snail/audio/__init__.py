"""Audio plane primitives (see docs 11-audio).

Phase-1 core: the interior frame and its refcounted pool. The pipeline (opus/RNNoise/
resample), fan-out bus, gates and sinks build on these.
"""

from .fanout import FanoutBus, OverflowPolicy, Subscriber, SubscriberRing
from .frame import AudioFrame, AudioSource, FrameFlags
from .pool import FramePool, FramePoolExhausted

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
]
