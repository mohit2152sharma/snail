"""ClientBridge — wires one client socket to one live agent (see docs 09§E / 11).

This is where the transport layer's **default behaviour** lives: *whatever the
multi-agent generates is passed to the client*. Concretely it runs two pumps over one
:class:`~snail.connections.AgentConnection`:

* **client → agent:** binary frames become ``send_realtime`` audio; the ``end`` control
  becomes ``audio_stream_end``; the ``playout`` control feeds the :class:`PlayoutClock`.
* **agent → client:** the connection's inbound loop hands us output PCM (``on_audio``) →
  straight to the client socket, and each raw vendor message is scanned for
  ``Interrupted`` → we push a ``flush`` control so a barge-in actually cuts the client's
  buffered playout (revoking the server token alone can't, docs 11).

An optional ``on_message`` forwards each raw vendor message onward (e.g. to a
:class:`~snail.session.Session` for logging/tools/routing) — the bridge never *requires*
a full session, so plain passthrough works out of the box.

The socket is the injected :class:`ClientSocket` seam, so the whole bridge runs against a
fake in tests — no web server, no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from snail.connections import AgentConnection
from snail.vendor import Interrupted, MediaChunk, RealtimeControl

from .protocol import Control, ControlType, decode_control, encode_control
from .socket import DISCONNECT, ClientSocket

OnMessage = Callable[[object], Awaitable[None]]


class PlayoutClock:
    """Tracks agent audio sent vs client-reported playout → buffered-ahead (docs 11).

    Enables honest "what the user actually heard" accounting and a correct cut: on
    barge-in the buffered-ahead audio is dropped, so we reset ``sent`` down to the last
    reported ``played`` position. Counts are in samples (PCM16 mono: bytes // 2).
    """

    __slots__ = ("sent", "played")

    def __init__(self) -> None:
        self.sent = 0
        self.played = 0

    def note_sent(self, n_samples: int) -> None:
        self.sent += n_samples

    def note_played(self, position: int) -> None:
        self.played = position

    @property
    def buffered_ahead(self) -> int:
        return max(0, self.sent - self.played)

    def on_flush(self) -> None:
        """Barge-in cut: buffered playout is discarded on the client."""
        self.sent = self.played


class ClientBridge:
    """Bidirectional pump between a client socket and one agent connection."""

    def __init__(
        self,
        *,
        socket: ClientSocket,
        connection: AgentConnection,
        input_sample_rate: int = 16000,
        on_message: OnMessage | None = None,
    ) -> None:
        self._socket = socket
        self._conn = connection
        self._in_rate = input_sample_rate
        self._on_message = on_message
        self._clock = PlayoutClock()
        self._client_gone = False

    @property
    def playout(self) -> PlayoutClock:
        return self._clock

    async def run(self) -> None:
        """Accept the socket and pump until either side ends. Cleans up on exit."""
        await self._socket.accept()
        await self._send_control(Control(type=ControlType.READY))
        client_in = asyncio.create_task(self._pump_client_in())
        agent_out = asyncio.create_task(
            self._conn.run(self._on_vendor_msg, on_audio=self._to_client)
        )
        try:
            await asyncio.wait(
                {client_in, agent_out}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (client_in, agent_out):
                task.cancel()
            await asyncio.gather(client_in, agent_out, return_exceptions=True)
            await self._teardown()

    # --- client → agent ---------------------------------------------------

    async def _pump_client_in(self) -> None:
        while True:
            msg = await self._socket.receive()
            if msg.get("type") == DISCONNECT:
                self._client_gone = True
                return
            data = msg.get("bytes")
            if data is not None:
                await self._conn.send_realtime(
                    MediaChunk.audio(data, sample_rate=self._in_rate)
                )
                continue
            text = msg.get("text")
            if text is not None:
                await self._handle_control(decode_control(text))

    async def _handle_control(self, ctrl: Control) -> None:
        if ctrl.type is ControlType.PLAYOUT and ctrl.samples is not None:
            self._clock.note_played(ctrl.samples)
        elif ctrl.type is ControlType.END:
            await self._conn.send_realtime_control(RealtimeControl.AUDIO_STREAM_END)

    # --- agent → client ---------------------------------------------------

    async def _to_client(self, pcm: bytes) -> None:
        self._clock.note_sent(len(pcm) // 2)  # PCM16 mono
        await self._socket.send_bytes(pcm)

    async def _on_vendor_msg(self, raw: object) -> None:
        for ev in self._conn.adapter.parse_event(raw):
            if isinstance(ev, Interrupted):
                await self._flush_client()
        if self._on_message is not None:
            await self._on_message(raw)

    async def _flush_client(self) -> None:
        """Barge-in: tell the client to drop buffered playout immediately."""
        self._clock.on_flush()
        await self._send_control(Control(type=ControlType.FLUSH))

    # --- helpers ----------------------------------------------------------

    async def _send_control(self, control: Control) -> None:
        await self._socket.send_text(encode_control(control))

    async def _teardown(self) -> None:
        # If the client already disconnected, sending BYE / closing would error and
        # break the close handshake — the socket is gone, nothing to say to.
        if self._client_gone:
            return
        for step in (
            lambda: self._send_control(Control(type=ControlType.BYE)),
            lambda: self._socket.close(),
        ):
            try:
                await step()
            except Exception:  # noqa: BLE001 - client may already be gone
                pass
