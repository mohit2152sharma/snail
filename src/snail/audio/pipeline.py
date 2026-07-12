"""AudioPipeline — assembles the audio plane into the two directional runners (docs 11).

Ties the plane's primitives (:class:`FramePool`, :class:`FanoutBus`,
:class:`RNNoiseCleaner`, :class:`LazyResampler`, :class:`AudioCodec`,
:class:`JitterBuffer`, :class:`~snail.router.OutputGate`) into the pipeline the docs
draw, exactly once, in one place. Everything runs on the session's single loop (docs 06),
so no locks.

Two directions:

**Ingress (client → vendors).** :meth:`on_client_audio` decodes the client bytes,
resamples to the 48k interior, rechunks to 10ms (480-sample) frames, and publishes each
onto the fan-out bus as ``USER_RAW``. If any subscriber wants ``USER_CLEAN`` it also runs
the (per-session) RNNoise cleaner and publishes cleaned frames. :meth:`drain` then pulls
each subscriber's ring, **lazily resamples 48k → that subscriber's vendor rate** (no-op
if equal, memoized per rate), and hands back vendor-ready PCM per subscriber — the
zero-copy shared-48k / resample-only-at-the-leg policy (docs 11).

**Egress (active vendor → client).** :meth:`on_vendor_audio` decodes/upsamples the
vendor's output to 48k and pushes it into the :class:`JitterBuffer` to smooth the bursts.
:meth:`playout` is the paced drain: one jittered frame → the :class:`OutputGate` token
check (only the active agent's audio reaches the user) → codec-encoded client bytes.

The pool is used on **ingress only** (user audio is fanned out to N consumers, so it
needs refcounted slabs); egress is a single active stream and stays plain-array.
"""

from __future__ import annotations

import numpy as np

from snail.router import OutputGate

from .clean import RNNoiseCleaner
from .codec import AudioCodec, PcmCodec
from .fanout import FanoutBus
from .frame import AudioFrame, AudioSource, FrameFlags
from .jitter import FRAME_LEN, JitterBuffer
from .pool import FramePool
from .resample import LazyResampler

INTERIOR_RATE = 48000  # the canonical interior sample rate (docs 11)


class AudioPipeline:
    """The audio-plane runner for one session (ingress + egress)."""

    def __init__(
        self,
        *,
        pool: FramePool,
        bus: FanoutBus,
        resampler: LazyResampler,
        gate: OutputGate,
        jitter: JitterBuffer,
        cleaner: RNNoiseCleaner | None = None,
        codec: AudioCodec | None = None,
        client_rate: int = INTERIOR_RATE,
        frame_size: int = FRAME_LEN,
    ) -> None:
        self._pool = pool
        self._bus = bus
        self._resampler = resampler
        self._gate = gate
        self._jitter = jitter
        self._cleaner = cleaner
        self._codec = codec or PcmCodec()
        self._client_rate = client_rate
        self._frame = frame_size
        self._raw_carry = np.empty(0, dtype=np.int16)
        self._in_seq = 0
        self._dropped = 0  # ingress frames lost to pool exhaustion (discontinuities)

    # --- consumer (GATE 1) management ------------------------------------

    def attach_consumer(
        self, consumer_id: str, *, source: AudioSource, target_rate: int, depth: int = 8
    ) -> None:
        """Subscribe a consumer to the fan-out bus (docs 11 GATE 1).

        ``source`` = ``USER_RAW`` / ``USER_CLEAN``; ``target_rate`` is the consumer's
        vendor input rate (its leg resamples 48k→that, lazily). The active agent is
        always attached; listeners per the Router.
        """
        self._bus.subscribe(
            consumer_id, source=source, target_rate=target_rate, depth=depth
        )

    def detach_consumer(self, consumer_id: str) -> int:
        """Unsubscribe a consumer, releasing its buffered slabs (detach-release)."""
        return self._bus.unsubscribe(consumer_id)

    def hold_token(self, agent_id: str) -> None:
        """Give the output token to ``agent_id`` — only its audio reaches the user (GATE 2)."""
        self._gate.transfer(agent_id)

    # --- ingress: client → interior → fan-out ----------------------------

    def on_client_audio(self, data: bytes) -> None:
        """Decode one client media frame and publish it to the fan-out bus.

        RAW frames always publish; CLEAN frames publish only when a subscriber wants
        them (the cleaner is skipped entirely otherwise — the per-consumer CPU lever).
        """
        samples = self._codec.decode(data)
        at48 = self._resampler.resample(
            samples, from_rate=self._client_rate, to_rate=INTERIOR_RATE
        )
        for frame480 in self._rechunk_raw(at48):
            self._publish(frame480, AudioSource.USER_RAW)
        if self._cleaner is not None and self._wants_clean():
            for cleaned in self._cleaner.process(at48):
                self._publish(cleaned, AudioSource.USER_CLEAN)

    def drain(self) -> dict[str, list[bytes]]:
        """Pull every subscriber's ring → vendor-ready PCM bytes, per subscriber id.

        Each frame is lazily resampled 48k → the subscriber's ``target_rate`` (no-op at
        48k, memoized per distinct rate), encoded to bytes, then the pooled frame is
        released — the ring-pop ownership-transfer contract (docs 11).
        """
        out: dict[str, list[bytes]] = {}
        for sub in self._bus.subscribers:
            chunks: list[bytes] = []
            while True:
                frame = sub.ring.pop()
                if frame is None:
                    break
                resampled = self._resampler.resample(
                    frame.samples, from_rate=INTERIOR_RATE, to_rate=sub.target_rate
                )
                chunks.append(np.ascontiguousarray(resampled, dtype=np.int16).tobytes())
                self._pool.release(frame)
            if chunks:
                out[sub.id] = chunks
        return out

    # --- egress: vendor → jitter → gate → client -------------------------

    def on_vendor_audio(self, pcm: bytes, *, vendor_rate: int) -> None:
        """Push one vendor output burst (PCM16 mono) into the jitter buffer at 48k."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        at48 = self._resampler.resample(
            samples, from_rate=vendor_rate, to_rate=INTERIOR_RATE
        )
        self._jitter.push(at48)

    def playout(self, agent_id: str) -> bytes | None:
        """Paced drain → client bytes, or ``None`` if no frame is due.

        One jittered 48k frame passes the :class:`OutputGate` token check (suppressed if
        ``agent_id`` isn't the holder), is popped from the gate ring, and codec-encoded
        for the client leg. Call it on the speaker's 10ms tick.
        """
        frame = self._jitter.pop()
        if frame is None:
            return None
        if not self._gate.write(agent_id, frame):
            return None  # not the token holder → dropped (single-voice invariant)
        out = self._gate.pop()
        return None if out is None else self._codec.encode(out)

    def cut(self) -> None:
        """Barge-in / CUT_NOW on the output path: flush jitter + gate rings."""
        self._jitter.flush()
        self._gate.flush()

    @property
    def stats(self) -> dict:
        return {
            "ingress_dropped": self._dropped,
            "jitter": self._jitter.stats,
            "gate": self._gate.stats,
            "resample_pairs": self._resampler.rate_pairs,
        }

    # --- internals --------------------------------------------------------

    def _wants_clean(self) -> bool:
        return any(
            s.source is AudioSource.USER_CLEAN for s in self._bus.subscribers
        )

    def _rechunk_raw(self, at48: np.ndarray) -> list[np.ndarray]:
        """Align the RAW 48k stream to fixed 480-sample frames (fits pool slabs).

        Carries a remainder (< 480) across calls, like the cleaner's rechunker — so RAW
        and CLEAN both publish uniform interior frames.
        """
        if len(self._raw_carry):
            at48 = np.concatenate([self._raw_carry, at48])
        n_full = len(at48) // self._frame
        frames = [
            at48[i * self._frame : (i + 1) * self._frame] for i in range(n_full)
        ]
        self._raw_carry = at48[n_full * self._frame :].copy()
        return frames

    def _publish(self, samples: np.ndarray, source: AudioSource) -> None:
        """Copy a 48k frame into a pooled slab and fan it out (drop on exhaustion)."""
        frame = self._acquire(len(samples), source)
        if frame is None:
            self._dropped += 1  # discontinuity — never crash ingress (docs 11)
            return
        frame.samples[:] = samples
        self._bus.publish(frame)

    def _acquire(self, n: int, source: AudioSource) -> AudioFrame | None:
        """Acquire a pooled frame; on exhaustion drop the globally-oldest and retry once
        (default drop-oldest recovery, docs 11) before giving up (drop-newest)."""
        self._in_seq += 1
        frame = self._pool.try_acquire(
            n, sample_rate=INTERIOR_RATE, source=source, seq=self._in_seq,
            flags=FrameFlags.NONE,
        )
        if frame is not None:
            return frame
        if self._bus.reclaim_oldest():
            return self._pool.try_acquire(
                n, sample_rate=INTERIOR_RATE, source=source, seq=self._in_seq,
            )
        return None
