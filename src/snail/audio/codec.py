"""Codec — client-leg encode/decode (see docs 11 §codec).

The **codec is the real latency/bandwidth lever** on the client leg (docs 11): opus is
~10–20× smaller than PCM. Snail owns the client transport, so it can send binary opus;
the vendor leg is base64-PCM regardless (vendor-forced), so codec applies only to the
client edge.

Kept behind an injected :class:`AudioCodec` seam (like the cleaner's ``DenoiseBackend``):

* :class:`PcmCodec` — v0 default, **no dependency**. int16 mono ⇄ PCM16 little-endian
  bytes. This is exactly what :mod:`snail.transport` sends today (binary = raw PCM16LE),
  so it is a real, usable codec, not a stub.
* An ``OpusCodec`` (wrapping ``opuslib``/``pyogg`` → ``libopus``) satisfies the same
  interface and drops in later without touching the pipeline.

Interior is always 48k int16 mono (docs 11 §Why 48kHz — the opus/RNNoise synergy), so a
codec here neither resamples nor changes channel count; it only (de)compresses.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class AudioCodec(Protocol):
    """Client-leg (de)compression. Frames are 48k int16 mono; bytes are the wire form."""

    def encode(self, samples: np.ndarray) -> bytes: ...

    def decode(self, data: bytes) -> np.ndarray: ...


class PcmCodec:
    """Passthrough codec: int16 mono ⇄ PCM16 little-endian bytes (v0 default).

    No compression — the raw client-leg format the transport uses today. Assumes little-
    endian samples (the client sends LE), matching the transport's stated wire contract.
    """

    def encode(self, samples: np.ndarray) -> bytes:
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        return samples.tobytes()

    def decode(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.int16)
