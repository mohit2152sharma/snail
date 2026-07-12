"""Neutral multimodal media for the outbound seams (see docs 07/11).

Gemini Live (and OpenAI Realtime) take multimodal input over two streaming shapes:
a **realtime** channel (continuous audio/image/text, VAD-driven) and an ordered
**turns** channel. The neutral layer converges on the same shape so an app sends
audio, images, or text through one vendor-neutral surface; the adapter maps each to
the right vendor call.

``MediaChunk`` is the realtime unit; ordered turns reuse :class:`~snail.context.Item`.
"""

from __future__ import annotations

import enum

import msgspec


class MediaKind(enum.Enum):
    AUDIO = "audio"  # PCM int16 bytes (+ sample_rate)
    IMAGE = "image"  # encoded image bytes (+ mime_type)
    TEXT = "text"  # realtime text


class RealtimeControl(enum.Enum):
    """Out-of-band control markers on the realtime channel."""

    ACTIVITY_START = "activity_start"  # manual VAD: user speech begins
    ACTIVITY_END = "activity_end"  # manual VAD: user speech ends
    AUDIO_STREAM_END = "audio_stream_end"  # no more audio coming


class MediaChunk(msgspec.Struct, frozen=True, kw_only=True):
    """One realtime multimodal chunk. Exactly one of audio/image/text is set."""

    kind: MediaKind
    data: bytes | None = None  # audio PCM or image bytes
    text: str | None = None  # realtime text
    mime_type: str | None = None  # for IMAGE (e.g. image/jpeg)
    sample_rate: int | None = None  # for AUDIO (Hz)

    @classmethod
    def audio(cls, pcm: bytes, *, sample_rate: int) -> "MediaChunk":
        return cls(kind=MediaKind.AUDIO, data=pcm, sample_rate=sample_rate)

    @classmethod
    def image(cls, data: bytes, *, mime_type: str = "image/jpeg") -> "MediaChunk":
        return cls(kind=MediaKind.IMAGE, data=data, mime_type=mime_type)

    @classmethod
    def text_(cls, text: str) -> "MediaChunk":
        return cls(kind=MediaKind.TEXT, text=text)
