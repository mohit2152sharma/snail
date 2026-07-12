"""Agent specs for the host + echo + translate multi-agent example.

- ``host`` — default active agent; conversational. Tools: ``start_echo``,
  ``start_translation``. Runs on the configured backend (Vertex by default).
- ``echo`` — repeats the user verbatim. Tool: ``stop`` (hands back to host). Same
  backend as host.
- ``translate`` — Gemini 3.5 Live Translate: translates any language → Hindi. This model
  is **Developer-API only** and supports **no tools / no system instruction**, so it
  always uses ``GEMINI_DEV`` and hands back via the client's manual "hand off" button.

Handoff into echo/translate is tool-result driven (see routing.py); handback from echo is
its ``stop`` tool, handback from translate is manual.
"""

from __future__ import annotations

import os

from snail.connections import AgentSpec
from snail.vendor import Backend, InputSource, ResponseModality, SetupParam, ToolSpec

HOST_ID = "host"
ECHO_ID = "echo"
TRANSLATE_ID = "translate"

#: Backend for host + echo. Vertex (ADC) by default; SNAIL_GEMINI_BACKEND=dev for the
#: Developer API. (translate is always Dev — the translate model is Dev-only.)
BACKEND = (
    Backend.GEMINI_DEV
    if os.environ.get("SNAIL_GEMINI_BACKEND", "vertex").lower() == "dev"
    else Backend.GEMINI_VERTEX
)

#: Live model for host + echo. On Vertex, 2.5-flash-live is ``gemini-live-2.5-flash`` in
#: the ``global`` location; the Dev API serves ``gemini-2.5-flash-live``.
MODEL = os.environ.get(
    "SNAIL_GEMINI_MODEL",
    "gemini-2.5-flash-live" if BACKEND is Backend.GEMINI_DEV else "gemini-live-2.5-flash",
)

#: Gemini 3.5 Live Translate (Developer API only).
TRANSLATE_MODEL = os.environ.get("SNAIL_TRANSLATE_MODEL", "gemini-3.5-live-translate-preview")
#: BCP-47 target language for the translate agent.
TRANSLATE_TARGET = os.environ.get("SNAIL_TRANSLATE_TARGET", "hi")  # Hindi

_HOST_INSTRUCTION = (
    "You are the HOST assistant, having a natural spoken conversation. Keep replies "
    "short and answer the user's questions directly. "
    "IMPORTANT: You are ALWAYS the host. You must NEVER behave like the other agents: "
    "do NOT repeat/echo the user's words back, and do NOT translate anything — those "
    "are other agents' jobs. When control returns to you after echo or translation "
    "mode, simply resume being the normal conversational host; ignore whatever those "
    "agents were doing and respond to the user as yourself. "
    "When the user asks to start echo mode (for example 'start echo') or translation "
    "('start translation' / 'translate'), say ONE short spoken confirmation first "
    "(e.g. 'Sure, switching to echo now.' or 'Okay, starting translation.') and then, "
    "in the SAME turn, call the matching tool (start_echo or start_translation). Speak "
    "the confirmation before the tool call so the user hears it, then call the tool — "
    "do not add anything after."
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
            voice="Puck",  # distinct voice so host is audibly identifiable
            response_modality=ResponseModality.AUDIO,
            input_source=InputSource.RAW,
            system_instruction=_HOST_INSTRUCTION,
            tools=(
                ToolSpec(
                    name="start_echo",
                    description="Switch to echo mode where the user's words are repeated back.",
                    parameters={"type": "object", "properties": {}},
                ),
                ToolSpec(
                    name="start_translation",
                    description="Switch to translation mode: translate the user's speech into Hindi.",
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
            voice="Kore",  # distinct voice from host
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


def build_translate_spec() -> AgentSpec:
    # Translation mode: no tools, no system instruction (model constraint). The
    # translation target is applied by TranslateGeminiAdapter at connect time.
    return AgentSpec(
        id=TRANSLATE_ID,
        backend=Backend.GEMINI_DEV,  # translate model is Dev-API only
        setup=SetupParam(
            model=TRANSLATE_MODEL,
            response_modality=ResponseModality.AUDIO,
            input_source=InputSource.RAW,
        ),
    )


SPECS = {
    HOST_ID: build_host_spec(),
    ECHO_ID: build_echo_spec(),
    TRANSLATE_ID: build_translate_spec(),
}

#: Which connection pool serves each agent. host + echo share the "main" pool (same
#: backend + adapter); translate needs the Dev-API "translate" pool (different model +
#: adapter). Keyed by purpose, not backend, so it holds even if host/echo also run on Dev.
POOL_KEY = {HOST_ID: "main", ECHO_ID: "main", TRANSLATE_ID: "translate"}


def resolve_spec(name: str) -> AgentSpec:
    return SPECS[name]
