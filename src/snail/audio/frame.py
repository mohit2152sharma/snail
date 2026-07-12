"""AudioFrame — the interior audio unit (see docs 11-audio).

Deliberately lean vs pipecat's fat frame: no name/id strings, no metadata dict, no
channel count (mono forced). Header is a handful of fixed fields; the samples view is
the only real memory. Frames are interior-only — audio never enters the event log and
frames are never serialized, so they carry a live numpy view, not owned bytes.

Canonical interior invariant (docs 11): ``samples`` is always PCM int16, mono, 48kHz.
Codec / encoding / vendor rates live only at the edges.
"""

from __future__ import annotations

import enum

import msgspec
import numpy as np


class AudioSource(enum.IntEnum):
    """Provenance of a frame's audio."""

    USER_RAW = 0  # post-decode, pre-clean
    USER_CLEAN = 1  # post-RNNoise
    AGENT = 2  # from a vendor


class FrameFlags(enum.IntFlag):
    """Bitfield flags carried on a frame."""

    NONE = 0
    IS_SPEECH = 1  # VAD marked this frame as speech
    IS_FINAL = 2  # last frame of an utterance/turn


class AudioFrame(msgspec.Struct, kw_only=True):
    """One chunk of interior audio. Fixed fields; no dict, no strings.

    ``samples`` is a numpy int16 **view** into a :class:`~snail.audio.pool.FramePool`
    slab — not owned bytes. It is valid only until the frame's last owner releases it
    back to the pool (see the pool's ownership protocol). ``slab_id`` is pool
    bookkeeping (interior only, never part of any wire format).
    """

    samples: np.ndarray  # int16 view, mono
    sample_rate: int
    n_samples: int
    source: AudioSource
    seq: int
    t_start: int = 0  # presentation timestamp, sample-clock
    flags: FrameFlags = FrameFlags.NONE
    slab_id: int = -1  # owning pool slab index; -1 = not pool-backed / released
