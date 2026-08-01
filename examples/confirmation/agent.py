"""The agent's system instruction, and the Gemini Live spec built from it.

Two halves, deliberately separate:

* the **task** half — what the assistant is and when to reach for each tool. Written by
  whoever owns the product.
* the **protocol** half — ``PROVIDE_INPUT_INSTRUCTION``, shipped by the framework and
  spliced in verbatim. Every deployment that uses blocking tools needs exactly these
  words, so they are not re-worded per example (docs 14).
"""

from __future__ import annotations

import os

from snail.connections import AgentSpec
from snail.tools import PROVIDE_INPUT_INSTRUCTION
from snail.vendor import Backend, InputSource, ResponseModality, SetupParam

from .tools import build_registry

AGENT_ID = "assistant"

_TASK_INSTRUCTION = """\
You are a hands-free voice assistant. Keep every spoken reply short — one or two
sentences — and never read out JSON or tool names.

You have three tools:
  - look_and_tell: answers a question about whatever the user is looking at, by taking
    a photo through the camera. Use it for questions like "what does this sign say" or
    "what am I holding". Pass the user's question through in "question", in their own
    words. Taking a photo needs the user's consent, so this tool will ask for it.
  - record_meeting: starts recording the meeting from the microphone. Recording needs
    the user's consent, so this tool will ask for it.
  - get_date_and_time: tells the current date and time. Nothing to ask, just call it.

Never claim you have taken a photo or started recording unless a tool result said so.
If a result comes back "blocked", tell the user plainly that you did not do it.\
"""

#: Task first, protocol second — the protocol block is the last thing the model reads
#: before it starts, which is where short procedural rules survive best.
SYSTEM_INSTRUCTION = f"{_TASK_INSTRUCTION}\n\n{PROVIDE_INPUT_INSTRUCTION}"

BACKEND = (
    Backend.GEMINI_DEV
    if os.environ.get("SNAIL_GEMINI_BACKEND", "vertex").lower() == "dev"
    else Backend.GEMINI_VERTEX
)
MODEL = os.environ.get(
    "SNAIL_GEMINI_MODEL",
    "gemini-2.5-flash-live" if BACKEND is Backend.GEMINI_DEV else "gemini-live-2.5-flash",
)


def build_agent_spec() -> AgentSpec:
    """One agent, exposing all three tools plus ``provide_input``.

    The exposure list comes straight off the registry, so ``provide_input`` is declared
    to the vendor at setup — it has to be, the answer arrives as a normal function call.
    """
    registry = build_registry()
    return AgentSpec(
        id=AGENT_ID,
        backend=BACKEND,
        setup=SetupParam(
            model=MODEL,
            voice="Puck",
            response_modality=ResponseModality.AUDIO,
            input_source=InputSource.RAW,
            system_instruction=SYSTEM_INSTRUCTION,
            tools=registry.specs(),
        ),
    )
