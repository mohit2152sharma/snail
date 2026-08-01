"""The three tools this example is about (see ``goal.md`` + docs 14).

They cover the shapes a voice agent actually has:

* ``look_and_tell`` — blocks on consent, and the answer it eventually gives depends on
  the *original* question, which it still holds across the wait. This is the case the
  design exists for: the tool call that asks for permission is not the tool call that
  answers.
* ``record_meeting`` — blocks on consent and has a real refusal branch: no recording.
* ``get_date_and_time`` — blocks on nothing. The same executor, no ``ctx`` at all.

Camera and microphone are mocked (``mocks.py``): the point being demonstrated is the
permission round-trip, not device I/O.
"""

from __future__ import annotations

from snail.tools import (
    InputRequired,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    build_provide_input_tool,
    declared_keys,
)

from .mocks import capture_photo, describe_photo, now_local, start_microphone

CAMERA_CONSENT = "camera_consent"
RECORDING_CONSENT = "recording_consent"


# --- 1. blocks, then uses what it was originally asked ---------------------


async def look_and_tell(args: dict, ctx: ToolContext) -> ToolResult:
    question = args["question"]
    granted = await ctx.require(CAMERA_CONSENT)
    if not granted:
        return ToolResult.blocked("the user did not consent to a photo")
    photo = capture_photo()
    return ToolResult.success(
        {"answer": describe_photo(photo, question), "question": question}
    )


LOOK_AND_TELL = Tool(
    "look_and_tell",
    look_and_tell,
    description=(
        "Look through the camera and answer a question about what is in front of the "
        "user. Use this whenever the question is about something the user can see "
        "('this sign', 'what does that say', 'what am I holding')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "the user's question about what they are looking at",
            }
        },
        "required": ["question"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "question": {"type": "string"},
        },
        "required": ["answer"],
    },
    requires=(
        InputRequired(
            key=CAMERA_CONSENT,
            expects="boolean",
            ask="ask whether you may take a photo to answer this",
            budget_s=45.0,
        ),
    ),
)


# --- 2. blocks, with a real refusal branch ---------------------------------


async def record_meeting(args: dict, ctx: ToolContext) -> ToolResult:
    granted = await ctx.require(RECORDING_CONSENT)
    if not granted:
        # Not an error: a refusal is a valid outcome, and nothing was recorded.
        return ToolResult.blocked("the user did not consent to recording")
    mic = start_microphone()
    return ToolResult.success({"recording": True, "device": mic, "title": args.get("title", "")})


RECORD_MEETING = Tool(
    "record_meeting",
    record_meeting,
    description="Start recording the meeting audio from the microphone.",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "optional name for the recording"}
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "recording": {"type": "boolean"},
            "device": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["recording"],
    },
    requires=(
        InputRequired(
            key=RECORDING_CONSENT,
            expects="boolean",
            ask="ask whether you may start recording the meeting",
            budget_s=45.0,
        ),
    ),
)


# --- 3. blocks on nothing --------------------------------------------------


def get_date_and_time(args: dict) -> dict:
    stamp = now_local()
    return {"iso": stamp.isoformat(timespec="seconds"), "spoken": stamp.strftime("%A, %d %B %Y at %I:%M %p")}


GET_DATE_AND_TIME = Tool(
    "get_date_and_time",
    get_date_and_time,
    description="Get the current date and time.",
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {"iso": {"type": "string"}, "spoken": {"type": "string"}},
        "required": ["iso", "spoken"],
    },
)


AGENT_TOOLS = (LOOK_AND_TELL, RECORD_MEETING, GET_DATE_AND_TIME)


def build_registry() -> ToolRegistry:
    """The catalog, plus the framework tool answers come back through.

    ``provide_input`` is built from the keys the agent tools declare, so its ``key``
    argument carries an enum of exactly the two consents that exist here.
    """
    registry = ToolRegistry()
    for tool in AGENT_TOOLS:
        registry.register(tool)
    registry.register(build_provide_input_tool(declared_keys(AGENT_TOOLS)))
    return registry
