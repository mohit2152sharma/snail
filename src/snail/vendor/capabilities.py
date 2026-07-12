"""Vendor capability descriptor (see docs 07-vendor-capability-matrix).

Cross-cutting pattern: **adapters declare capabilities; the framework branches on
them.** Capability is keyed per **(vendor, model, backend)**, not per vendor — so two
Gemini backends can differ (e.g. native async tools on the Developer API, emulated on
Vertex — issue #1739). The neutral surface stays the same; the adapter absorbs the
difference.
"""

from __future__ import annotations

import enum

import msgspec


class Backend(enum.Enum):
    """A concrete (vendor, model) hosting backend."""

    GEMINI_DEV = "gemini_dev"  # Gemini Developer API
    GEMINI_VERTEX = "gemini_vertex"  # Vertex AI
    OPENAI_REALTIME = "openai_realtime"
    MOCK = "mock"


class VendorCapabilities(msgspec.Struct, frozen=True, kw_only=True):
    """What one ``(vendor, model, backend)`` actually supports.

    Verified cells for the real backends live in docs 07. The framework reads these
    flags instead of hardcoding vendor names.
    """

    vendor: str
    model: str
    backend: Backend

    #: native async / non-blocking tool calls (Gemini Dev API `NON_BLOCKING`).
    native_async_tools: bool = False
    #: native session resumption handle (Gemini). Else recycle = log-replay.
    session_resumption: bool = False
    #: accepts ``role="system"`` *content* turns. Gemini does NOT (docs 07).
    system_content_turn: bool = False
    #: config/instruction/tool update allowed mid-session. Gemini does NOT — so a
    #: text→audio modality flip on promote needs a reconnect (docs 05/07, 09§E).
    mid_session_config_update: bool = False
    #: can truncate an already-emitted item to "what the user actually heard"
    #: (OpenAI `conversation.item.truncate`; Gemini lacks it — docs 01/09§E).
    item_truncate: bool = False
    #: the model does its own noise suppression → the framework can feed it RAW audio
    #: and skip RNNoise for it (some Gemini models — docs 07/11). An agent on such a
    #: model should set ``SetupParam.input_source = RAW``.
    self_denoise: bool = False

    #: vendor wire sample rates (Hz); interior is always 48k. Resample is LAZY — a
    #: consumer's leg downsamples only when its rate ≠ 48k (docs 11).
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
