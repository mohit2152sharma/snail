"""AgentConnection — the live vendor session (see docs 02).

A connection owns one vendor socket (a :class:`LiveTransport`) plus the neutral send
seams and the inbound read loop. It is the **expensive, swappable** half of the
``AgentSpec (stable) → AgentConnection (swappable)`` split: recycle replaces the
transport underneath while the connection identity — and everything above it — stays
put (:meth:`adopt`).

Design boundaries kept deliberately narrow (like the rest of Snail):

* **Send** goes through the adapter's serialize seams, so the connection stays
  vendor-neutral: ``send_realtime`` / ``send_realtime_control`` / ``send_turns`` /
  ``send_tool_result`` map 1:1 onto the vendor's outbound shapes (docs 07).
* **Receive** is a bare loop over ``transport.receive()`` that hands each raw message to
  an injected async ``on_message`` (the Session's ``on_vendor_raw``) and, if wired,
  routes extracted output audio to ``on_audio``. The connection does **not** parse events
  itself — that is the adapter/Session boundary.
* **Deadline / resumption** bookkeeping lives in :class:`ConnectionMeta`, updated via
  callbacks the Session already exposes (``on_goaway``/``on_resumption``), so the
  connection never re-parses the stream.

The transport is a structural :class:`LiveTransport`; the real Gemini socket
(``google-genai`` ``AsyncSession``) satisfies it directly, and tests inject a fake — so
this whole layer is exercisable without a key or a network.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from snail.context import Item
from snail.vendor import (
    JoinContext,
    MediaChunk,
    RealtimeControl,
    VendorAdapter,
)

from .spec import AgentSpec

Clock = Callable[[], float]
OnMessage = Callable[[Any], Awaitable[None]]
OnAudio = Callable[[bytes], Awaitable[None]]


@runtime_checkable
class LiveTransport(Protocol):
    """The raw vendor socket surface the connection drives.

    Matches ``google-genai``'s ``AsyncSession`` structurally: the send methods take the
    exact kwargs the adapter's serialize seams produce, ``receive`` yields raw vendor
    messages, ``close`` tears the socket down. A fake with the same shape stands in for
    tests.
    """

    def send_realtime_input(self, **kwargs: Any) -> Awaitable[None]: ...

    def send_client_content(self, **kwargs: Any) -> Awaitable[None]: ...

    def send_tool_response(
        self, *, function_responses: Any
    ) -> Awaitable[None]: ...

    def receive(self) -> Any: ...  # AsyncIterator[raw message]

    async def close(self) -> None: ...


class ConnectionState(enum.Enum):
    """Lifecycle of a connection (docs 02)."""

    COLD = "cold"  # no socket yet
    CONNECTING = "connecting"  # handshake in flight
    WARM = "warm"  # socket up + configured, idle (pooled / parked)
    ACTIVE = "active"  # promoted: this connection holds the output token
    CLOSED = "closed"  # torn down


class ConnectionMeta:
    """Per-connection lifecycle bookkeeping (docs 02 §Steps).

    Tracks what the pool's recycle scheduler needs — creation time, the vendor
    ``deadline`` (from ``GoAway``), the latest ``resumption_handle``, and
    ``last_activity`` for idle-keepalive — without the connection having to re-parse the
    stream. Times are on the injected monotonic ``clock``.
    """

    __slots__ = ("created_at", "last_activity", "deadline", "resumption_handle")

    def __init__(self, *, now: float) -> None:
        self.created_at = now
        self.last_activity = now
        self.deadline: float | None = None
        self.resumption_handle: str | None = None

    def touch(self, now: float) -> None:
        self.last_activity = now

    def note_goaway(self, time_left_ms: int | None, *, now: float) -> None:
        """Record a vendor termination deadline from a ``GoAway`` (docs 02 step 1)."""
        if time_left_ms is not None:
            self.deadline = now + time_left_ms / 1000.0

    def note_resumption(self, handle: str) -> None:
        self.resumption_handle = handle

    def ttl_headroom(self, now: float) -> float | None:
        """Seconds until the vendor deadline, or ``None`` if no deadline known."""
        return None if self.deadline is None else self.deadline - now

    def recycle_due(self, now: float, *, margin: float) -> bool:
        """True when within ``margin`` seconds of the deadline (docs 02 step 2)."""
        head = self.ttl_headroom(now)
        return head is not None and head <= margin


class AgentConnection:
    """One live vendor session for an :class:`AgentSpec`."""

    def __init__(
        self,
        *,
        spec: AgentSpec,
        adapter: VendorAdapter,
        transport: LiveTransport | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._spec = spec
        self._adapter = adapter
        self._transport = transport
        self._clock = clock
        self._state = ConnectionState.WARM if transport is not None else ConnectionState.COLD
        self._meta = ConnectionMeta(now=clock())

    # --- identity / state -------------------------------------------------

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def id(self) -> str:
        return self._spec.id

    @property
    def adapter(self) -> VendorAdapter:
        return self._adapter

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def meta(self) -> ConnectionMeta:
        return self._meta

    def activate(self) -> None:
        """Mark this connection the active (token-holding) agent."""
        self._require_live()
        self._state = ConnectionState.ACTIVE

    def park(self) -> None:
        """Demote to a warm, idle standby (fast return after handoff-away, docs 02)."""
        self._require_live()
        self._state = ConnectionState.WARM

    def adopt(self, transport: LiveTransport) -> LiveTransport | None:
        """Atomic-swap in a fresh transport (recycle), returning the old one to close.

        Upper layers never see the socket change — the ``AgentConnection`` identity and
        the ``AgentSpec`` stay put (docs 02 §Component). Deadline/activity reset; the
        resumption handle carries forward so a native resume keeps context.
        """
        old = self._transport
        self._transport = transport
        handle = self._meta.resumption_handle
        self._meta = ConnectionMeta(now=self._clock())
        self._meta.resumption_handle = handle
        if self._state in (ConnectionState.COLD, ConnectionState.CLOSED):
            self._state = ConnectionState.WARM
        return old

    # --- lifecycle callbacks (wired to Session on_goaway/on_resumption) ---

    def note_goaway(self, ev: Any) -> None:
        self._meta.note_goaway(getattr(ev, "time_left_ms", None), now=self._clock())

    def note_resumption(self, handle: str) -> None:
        self._meta.note_resumption(handle)

    # --- outbound seams (neutral → vendor) --------------------------------

    async def send_realtime(self, chunk: MediaChunk) -> None:
        """Streaming multimodal input (audio/image/text) — VAD-driven, unordered."""
        await self._transport_ref().send_realtime_input(
            **self._adapter.serialize_realtime(chunk)
        )

    async def send_realtime_control(self, control: RealtimeControl) -> None:
        """Manual-VAD activity / audio-stream-end markers."""
        await self._transport_ref().send_realtime_input(
            **self._adapter.serialize_realtime_control(control)
        )

    async def send_turns(self, items: Sequence[Item], *, complete: bool) -> None:
        """Ordered content turns; ``complete`` triggers a response (docs 07)."""
        await self._transport_ref().send_client_content(
            **self._adapter.serialize_turns(list(items), complete=complete)
        )

    async def send_tool_result(self, function_response: Any) -> None:
        """Send a serialized tool result. This is the Session's ``send`` seam.

        The Session builds it via ``adapter.serialize_tool_result(...)`` and hands the
        result straight here; we route it to the vendor's tool-response shape.
        """
        await self._transport_ref().send_tool_response(
            function_responses=function_response
        )

    async def inject_history(self, join: JoinContext) -> None:
        """Inject per-client history/facts as content turns **before the first model
        turn** (docs 02/07). ``complete=False`` so it seeds context without triggering a
        response.
        """
        items = list(join.facts) + list(join.history)
        if items:
            await self.send_turns(items, complete=False)

    # --- inbound read loop ------------------------------------------------

    async def run(self, on_message: OnMessage, *, on_audio: OnAudio | None = None) -> None:
        """Pump ``transport.receive()`` into ``on_message`` until the socket closes.

        Each raw message refreshes ``last_activity``; if ``on_audio`` is wired and the
        adapter can pull output PCM, it is delivered before the message is dispatched to
        the neutral event handler (so playout leads transcript logging).
        """
        transport = self._transport_ref()
        extract = getattr(self._adapter, "extract_output_audio", None)
        async for msg in transport.receive():
            self._meta.touch(self._clock())
            if on_audio is not None and extract is not None:
                pcm = extract(msg)
                if pcm:
                    await on_audio(pcm)
            await on_message(msg)

    async def close(self) -> None:
        """Tear down the socket. Idempotent."""
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        self._state = ConnectionState.CLOSED

    # --- internals --------------------------------------------------------

    def _transport_ref(self) -> LiveTransport:
        if self._transport is None:
            raise RuntimeError(f"connection {self.id!r} has no live transport")
        return self._transport

    def _require_live(self) -> None:
        if self._state is ConnectionState.CLOSED or self._transport is None:
            raise RuntimeError(f"connection {self.id!r} is not live")
