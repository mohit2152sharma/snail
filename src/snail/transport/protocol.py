"""Client ⇄ Snail wire protocol (the transport layer's contract).

The transport layer's job is to **define how client and server talk** (docs 09§E
`client-protocol`). One WebSocket carries two frame kinds:

* **Binary frame → media.** Raw audio payload, v0 = PCM16 little-endian mono at the
  session's negotiated rate. Audio is the only binary type, so there is no header — the
  bytes *are* the payload. (A codec/header can be layered later behind the same seam,
  the way the cleaner injects its denoise backend.)
* **Text frame → control.** A JSON :class:`Control` message. This is the channel that
  makes a real barge-in possible: revoking the server-side token stops *sending*, but the
  client still holds buffered playout — only a client-bound ``flush`` actually cuts it
  (docs 11 TODO client-protocol).

Direction is symmetric on the binary side: client→server = mic audio, server→client =
agent audio. Control messages are defined per direction below.
"""

from __future__ import annotations

import enum

import msgspec


class ControlType(str, enum.Enum):
    """Control message kinds on the text channel."""

    # server → client
    READY = "ready"  # session accepted; start streaming
    FLUSH = "flush"  # barge-in / CUT_NOW: drop buffered playout NOW
    TRANSCRIPT = "transcript"  # optional: agent/user transcript text
    BYE = "bye"  # server closing the session
    # client → server
    PLAYOUT = "playout"  # playout-position report (samples actually played)
    END = "end"  # client finished sending audio (→ audio_stream_end)


class Control(msgspec.Struct, kw_only=True, omit_defaults=True):
    """One control-channel message. Unused fields are omitted on the wire."""

    type: ControlType
    text: str | None = None
    role: str | None = None  # "agent" | "user" (transcript)
    final: bool | None = None  # transcript finality
    samples: int | None = None  # playout position
    reason: str | None = None  # bye reason


_ENC = msgspec.json.Encoder()
_DEC = msgspec.json.Decoder(Control)


def encode_control(control: Control) -> str:
    """Serialize a control message to a WS text frame."""
    return _ENC.encode(control).decode()


def decode_control(text: str) -> Control:
    """Parse a WS text frame into a control message."""
    return _DEC.decode(text)
