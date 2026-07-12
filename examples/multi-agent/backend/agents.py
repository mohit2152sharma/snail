"""Agent specs for the host+echo multi-agent example.

Two Gemini Dev-API agents sharing one model. ``host`` is the default active agent and
carries the ``start_echo`` control tool; ``echo`` repeats the user verbatim and carries
the ``stop`` tool. Handoff between them is driven by the tool results (see routing.py).
"""

from __future__ import annotations

import os

from snail.connections import AgentSpec
from snail.vendor import Backend, InputSource, ResponseModality, SetupParam, ToolSpec

HOST_ID = "host"
ECHO_ID = "echo"
MODEL = "gemini-2.5-flash-live"

#: Which Gemini backend the example runs on. Vertex (ADC) by default; set
#: SNAIL_GEMINI_BACKEND=dev for the Developer API (api key).
BACKEND = (
    Backend.GEMINI_DEV
    if os.environ.get("SNAIL_GEMINI_BACKEND", "vertex").lower() == "dev"
    else Backend.GEMINI_VERTEX
)

_HOST_INSTRUCTION = (
    "You are a friendly host assistant having a natural spoken conversation. "
    "Keep replies short. When the user asks to start echo mode (for example 'start "
    "echo'), call the start_echo tool — do not describe it, just call it."
)
_ECHO_INSTRUCTION = (
    "You are an echo bot. Repeat back, verbatim, exactly what the user says and nothing "
    "else. When the user says 'stop', call the stop tool — do not say anything else, "
    "just call it."
)


def build_host_spec() -> AgentSpec:
    return AgentSpec(
        id=HOST_ID,
        backend=BACKEND,
        setup=SetupParam(
            model=MODEL,
            response_modality=ResponseModality.AUDIO,
            input_source=InputSource.RAW,
            system_instruction=_HOST_INSTRUCTION,
            tools=(
                ToolSpec(
                    name="start_echo",
                    description="Switch to echo mode where the user's words are repeated back.",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
        ),
    )


def build_echo_spec() -> AgentSpec:
    return AgentSpec(
        id=ECHO_ID,
        backend=BACKEND,
        setup=SetupParam(
            model=MODEL,
            response_modality=ResponseModality.AUDIO,
            input_source=InputSource.RAW,
            system_instruction=_ECHO_INSTRUCTION,
            tools=(
                ToolSpec(
                    name="stop",
                    description="Leave echo mode and return to the host assistant.",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
        ),
    )


SPECS = {HOST_ID: build_host_spec(), ECHO_ID: build_echo_spec()}


def resolve_spec(name: str) -> AgentSpec:
    return SPECS[name]
