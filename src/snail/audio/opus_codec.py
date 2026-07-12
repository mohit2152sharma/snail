"""OpusCodec — the client-leg opus (de)compressor (libopus via ``opuslib``).

The real :class:`~snail.audio.AudioCodec` for the browser↔Snail hop, where opus is the
bandwidth lever (~16–32 kbps vs PCM16's 768 kbps @48k — docs 11 §codec). Drops in exactly
where :class:`~snail.audio.PcmCodec` sits — ``decode`` on ingress, ``encode`` on egress —
so swapping it in is a factory change, no pipeline edit.

**Opus is native 48kHz** (docs 11 §Why 48kHz): decode lands directly at the interior
rate, encode consumes interior frames — no resample surrounds the codec.

Two properties that shape the interface:

* **Stateful.** The encoder and decoder each carry state across packets, so a codec
  instance is **per-connection** (one encoder + one decoder per stream), like the soxr
  resample stream — not a shared singleton.
* **Framed.** ``encode`` needs **exactly** one opus frame of samples. At 48k the valid
  frame sizes are 120/240/480/960/1920/2880 (2.5–60 ms); the default **480 = 10 ms**
  matches the pipeline's interior frame (:data:`snail.audio.jitter.FRAME_LEN`), so egress
  ``playout`` frames encode 1:1. ``decode`` is duration-agnostic — an opus packet
  self-describes its length, so it decodes with a generous max and returns the actual
  samples (a client sending 20 ms packets just works).

Guarded import: constructing this class is where ``opuslib``/libopus becomes required;
the core audio layer stays importable without it (same contract as the soxr backend).
"""

from __future__ import annotations

import numpy as np

try:
    import opuslib
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without opuslib
    raise ModuleNotFoundError(
        "OpusCodec needs the 'opuslib' package (libopus). Install it, or use PcmCodec "
        "for a raw-PCM client leg."
    ) from exc

_MAX_FRAME = 5760  # 120 ms @48k — the largest opus frame; decode upper bound


class OpusCodec:
    """Per-stream opus codec: opus packet ⇄ int16 mono PCM @48k."""

    def __init__(
        self,
        *,
        rate: int = 48000,
        frame_size: int = 480,
        application: int = opuslib.APPLICATION_VOIP,
    ) -> None:
        self._rate = rate
        self._frame = frame_size
        self._enc = opuslib.Encoder(rate, 1, application)
        self._dec = opuslib.Decoder(rate, 1)

    @property
    def frame_size(self) -> int:
        """Samples per encode frame (egress must hand exactly this many)."""
        return self._frame

    def encode(self, samples: np.ndarray) -> bytes:
        """Encode exactly one ``frame_size`` int16 frame to an opus packet."""
        if len(samples) != self._frame:
            raise ValueError(
                f"opus encode needs exactly {self._frame} samples, got {len(samples)}"
            )
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        return self._enc.encode(np.ascontiguousarray(samples).tobytes(), self._frame)

    def decode(self, data: bytes) -> np.ndarray:
        """Decode one opus packet to int16 PCM (actual length per the packet)."""
        return np.frombuffer(self._dec.decode(data, _MAX_FRAME), dtype=np.int16)
