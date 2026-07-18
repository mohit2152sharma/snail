"""ClientBridge — wires one client socket to one live agent (see docs 09§E / 11).

This is where the transport layer's **default behaviour** lives: *whatever the
multi-agent generates is passed to the client*. Concretely it runs two pumps over one
:class:`~snail.connections.AgentConnection`:

* **client → agent:** binary frames become ``send_realtime`` audio; the ``end`` control
  becomes ``audio_stream_end``; the ``playout`` control feeds the :class:`PlayoutClock`.
* **agent → client:** the connection's inbound loop hands us output PCM (``on_audio``) →
  the client socket, and each raw vendor message is scanned for ``Interrupted`` → we push
  a ``flush`` control so a barge-in actually cuts the client's buffered playout (revoking
  the server token alone can't, docs 11).

**Audio plane (optional).** With no ``pipeline`` the bridge is a straight passthrough
(mic bytes → vendor, vendor PCM → client) — fine for a client already at the vendor's
rate. Inject an :class:`~snail.audio.AudioPipeline` and audio instead flows through the
plane: ingress runs decode → resample-to-48k → clean → fan-out → per-consumer
resample-to-vendor-rate; egress runs decode → jitter → the ``OutputGate`` token → codec.
The bridge attaches the connection as the (single, token-holding) consumer on start and
detaches it on teardown; a barge-in cuts the plane's jitter+gate rings as well as the
client.

An optional ``on_message`` forwards each raw vendor message onward (e.g. to a
:class:`~snail.session.Session` for logging/tools/routing) — the bridge never *requires*
a full session, so plain passthrough works out of the box.

The socket is the injected :class:`ClientSocket` seam, so the whole bridge runs against a
fake in tests — no web server, no network.

**Per-turn TTFB (``ttfb_stats``).** The bridge measures the metric this framework is
actually judged on: wall-clock from the *last* mic chunk ingested (the closest signal we
have to "user stopped talking") to the *first* agent-audio byte handed to the client for
that turn — silence-dwell + vendor generation + our own framework overhead, end to end,
not just the piece we control. Armed at session start, after every ``TurnComplete``, and
after every barge-in flush (each is the start of a fresh turn to measure). A bounded
recent-sample window (`docs 12`, ahead of the full Observer layer) — this is *not* the
full pluggable observability surface (msgspec Metric/Event, fan-out, PUSH/SAMPLED), just
the one number worth having before that lands.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

from snail.audio import AudioPipeline, AudioSource
from snail.connections import AgentConnection
from snail.vendor import (
    Interrupted,
    InputSource,
    MediaChunk,
    RealtimeControl,
    TurnComplete,
)

from .protocol import Control, ControlType, decode_control, encode_control
from .socket import DISCONNECT, ClientSocket

OnMessage = Callable[[object], Awaitable[None]]
Clock = Callable[[], float]


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
        pipeline: AudioPipeline | None = None,
        clock: Clock = time.monotonic,
        ttfb_window: int = 50,
    ) -> None:
        self._socket = socket
        self._conn = connection
        self._in_rate = input_sample_rate
        self._on_message = on_message
        self._pipeline = pipeline
        self._clock = PlayoutClock()
        self._client_gone = False
        # Vendor output rate drives egress upsample-to-48k; only needed with a pipeline.
        self._out_rate = (
            connection.adapter.capabilities.output_sample_rate
            if pipeline is not None
            else 0
        )
        # --- per-turn TTFB (wall clock, not sample count — see module docstring) ---
        self._now = clock
        self._last_ingest_at: float | None = None
        self._awaiting_first_byte = True  # armed: session start counts as turn 1
        self._ttfb_samples_ms: deque[float] = deque(maxlen=ttfb_window)

    @property
    def playout(self) -> PlayoutClock:
        return self._clock

    @property
    def ttfb_stats(self) -> dict[str, float | int]:
        """Recent per-turn TTFB samples: last-ingest → first-agent-byte, in ms.

        Empty (``count=0``) until at least one full turn has completed. This is the
        end-to-end number — silence-dwell + vendor generation + framework overhead —
        not just our own slice, since that's the number actually worth comparing
        against another framework's published TTFB (docs 00/11).
        """
        n = len(self._ttfb_samples_ms)
        if n == 0:
            return {"count": 0, "last_ms": 0.0, "mean_ms": 0.0, "p95_ms": 0.0}
        ordered = sorted(self._ttfb_samples_ms)
        p95 = ordered[min(n - 1, int(0.95 * n))]
        return {
            "count": n,
            "last_ms": self._ttfb_samples_ms[-1],
            "mean_ms": sum(ordered) / n,
            "p95_ms": p95,
        }

    def _arm_next_turn(self) -> None:
        """Mark the next agent-audio byte as the start of a fresh TTFB measurement."""
        self._awaiting_first_byte = True

    def _mark_first_byte(self) -> None:
        """Record one TTFB sample if a turn was armed and we know when it started."""
        if not self._awaiting_first_byte:
            return
        self._awaiting_first_byte = False
        if self._last_ingest_at is not None:
            self._ttfb_samples_ms.append((self._now() - self._last_ingest_at) * 1000.0)

    def _attach_pipeline(self) -> None:
        """Attach the connection as the single token-holding consumer (GATE 1 + 2)."""
        if self._pipeline is None:
            return
        source = (
            AudioSource.USER_CLEAN
            if self._conn.spec.setup.input_source is InputSource.CLEAN
            else AudioSource.USER_RAW
        )
        self._pipeline.attach_consumer(
            self._conn.id,
            source=source,
            target_rate=self._conn.adapter.capabilities.input_sample_rate,
        )
        self._pipeline.hold_token(self._conn.id)  # single active agent holds the token

    async def run(self) -> None:
        """Accept the socket and pump until either side ends. Cleans up on exit."""
        await self._socket.accept()
        self._attach_pipeline()
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
                await self._ingest_audio(data)
                continue
            text = msg.get("text")
            if text is not None:
                await self._handle_control(decode_control(text))

    async def _ingest_audio(self, data: bytes) -> None:
        """Mic bytes → vendor. Through the audio plane if wired, else straight passthrough."""
        self._last_ingest_at = self._now()  # TTFB start: latest signal of "still talking"
        if self._pipeline is None:
            await self._conn.send_realtime(
                MediaChunk.audio(data, sample_rate=self._in_rate)
            )
            return
        self._pipeline.on_client_audio(data)
        # single-agent slice: the one consumer is this connection.
        for chunk in self._pipeline.drain().get(self._conn.id, ()):
            await self._conn.send_realtime(
                MediaChunk.audio(
                    chunk, sample_rate=self._conn.adapter.capabilities.input_sample_rate
                )
            )

    async def _handle_control(self, ctrl: Control) -> None:
        if ctrl.type is ControlType.PLAYOUT and ctrl.samples is not None:
            self._clock.note_played(ctrl.samples)
        elif ctrl.type is ControlType.END:
            await self._conn.send_realtime_control(RealtimeControl.AUDIO_STREAM_END)

    # --- agent → client ---------------------------------------------------

    async def _to_client(self, pcm: bytes) -> None:
        """Vendor PCM → client. Through jitter + token gate + codec if wired, else direct."""
        if self._pipeline is None:
            self._mark_first_byte()
            self._clock.note_sent(len(pcm) // 2)  # PCM16 mono
            await self._socket.send_bytes(pcm)
            return
        self._pipeline.on_vendor_audio(pcm, vendor_rate=self._out_rate)
        while True:
            frame = self._pipeline.playout(self._conn.id)
            if frame is None:
                break
            self._mark_first_byte()
            self._clock.note_sent(len(frame) // 2)
            await self._socket.send_bytes(frame)

    async def _on_vendor_msg(self, raw: object) -> None:
        for ev in self._conn.adapter.parse_event(raw):
            if isinstance(ev, Interrupted):
                await self._flush_client()
            elif isinstance(ev, TurnComplete):
                self._arm_next_turn()  # this turn's audio is done; the next is fresh
        if self._on_message is not None:
            await self._on_message(raw)

    async def _flush_client(self) -> None:
        """Barge-in: cut the audio plane, then tell the client to drop buffered playout."""
        if self._pipeline is not None:
            self._pipeline.cut()  # flush jitter + gate rings server-side
        self._clock.on_flush()
        self._arm_next_turn()  # whatever plays after the cut starts a fresh measurement
        await self._send_control(Control(type=ControlType.FLUSH))

    # --- helpers ----------------------------------------------------------

    async def _send_control(self, control: Control) -> None:
        await self._socket.send_text(encode_control(control))

    async def _teardown(self) -> None:
        if self._pipeline is not None:
            self._pipeline.detach_consumer(self._conn.id)  # release buffered slabs
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
