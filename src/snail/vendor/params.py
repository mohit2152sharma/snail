"""Neutral connection params: SetupParam vs JoinContext (see docs 02/07).

Verified vendor split (docs 02/07):

    SetupParam  (bound at connect, BOTH vendors):  model, voice, system_instruction,
                tools, response_modality  = the agent's STATIC identity
    JoinContext (injected on join, BOTH vendors):  history, per-client facts
                = genuinely dynamic per-client data

``system_instruction`` and ``tools`` are bound at **setup** for both vendors
(symmetric), because Gemini cannot update them mid-session and a uniform mental model
beats an OpenAI-only micro-optimization.
"""

from __future__ import annotations

import enum

import msgspec

from snail.context import Item


class InputSource(enum.Enum):
    """Which user-audio source an agent consumes (docs 11).

    ``CLEAN`` is the default (RNNoise-denoised). ``RAW`` skips cleaning — for a model
    that self-denoises (``VendorCapabilities.self_denoise``) or when the caller wants
    the untouched signal. If no agent wants ``CLEAN``, RNNoise never runs (CPU saved).
    """

    CLEAN = "clean"
    RAW = "raw"


class ResponseModality(enum.Enum):
    """Per-agent output modality (docs 05).

    The active agent is ``AUDIO``. A listener is ``TEXT`` **or** ``AUDIO`` — per
    listener, not both: ``TEXT`` is cheapest but needs a text→audio flip (a reconnect
    on Gemini) to promote; ``AUDIO`` costs audio-out for suppressed audio but promotes
    with no flip.
    """

    AUDIO = "audio"
    TEXT = "text"


class ToolSpec(msgspec.Struct, frozen=True, kw_only=True):
    """Vendor-neutral tool declaration bound at setup.

    ``parameters`` is a JSON-schema dict (common-denominator schema, docs 03). The
    full :mod:`snail.tools` layer produces these; the adapter serializes them.
    """

    name: str
    description: str = ""
    parameters: dict | None = None
    #: async behavior hint; maps to Gemini `Behavior.NON_BLOCKING` where supported.
    non_blocking: bool = False


class SetupParam(msgspec.Struct, frozen=True, kw_only=True):
    """The agent's static identity — bound at connect (the pool key, docs 02)."""

    model: str
    voice: str | None = None
    system_instruction: str = ""
    tools: tuple[ToolSpec, ...] = ()
    response_modality: ResponseModality = ResponseModality.AUDIO
    #: which user-audio source this agent consumes (raw vs cleaned, docs 11).
    input_source: InputSource = InputSource.CLEAN


class JoinContext(msgspec.Struct, frozen=True, kw_only=True):
    """Per-client dynamic data injected on join (docs 02).

    ``history`` is projected ``Item[]`` injected as ``user``/``model`` turns **before
    the first model turn** (docs 07). ``facts`` are extra neutral items (per-client
    context).
    """

    history: tuple[Item, ...] = ()
    facts: tuple[Item, ...] = ()
