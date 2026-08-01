"""Scripted walkthrough of the consent flow — no API key, no audio, no network.

The model is played by a script here. That is the point: the flow this example exists
to validate is entirely framework-side, so pinning the vendor's half makes every branch
reproducible and lets the interesting failures (a stale answer, a topic change, a
wrong-typed value) be provoked on demand instead of waited for.

What is real: the ``Session``, the ``RunSlots`` state machine, the executor, the
envelope, and the exact payloads an adapter would put on the wire. What is scripted:
which function calls Gemini emits, and what the user says.

Run::

    PYTHONPATH=examples python -m confirmation.demo
"""

from __future__ import annotations

import asyncio
import time

from snail.audio import AudioSource, FanoutBus, FramePool
from snail.context import EventLog, EventType
from snail.registry import ToolCallRegistry
from snail.router import OutputGate, Router
from snail.session import Session
from snail.vendor import (
    MockVendorAdapter,
    ResponseModality,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
)

from .agent import AGENT_ID
from .tools import build_registry

RULE = "─" * 78


class Wire:
    """One session, plus enough plumbing to narrate what crosses the vendor boundary."""

    def __init__(self) -> None:
        pool = FramePool(capacity=32, slab_samples=4)
        self.log = EventLog()
        self.calls = ToolCallRegistry()
        router = Router(gate=OutputGate(), bus=FanoutBus(pool), registry=self.calls)
        router.register_agent(
            AGENT_ID,
            "spec",
            modality=ResponseModality.AUDIO,
            input_source=AudioSource.USER_CLEAN,
            target_rate=16000,
        )
        router.set_active(AGENT_ID)
        self.sent: list[dict] = []
        self.session = Session(
            adapter=MockVendorAdapter(),
            log=self.log,
            tools=build_registry(),
            registry=self.calls,
            router=router,
            send=self._send,
        )
        self._shown = 0
        self._call_no = 0

    async def _send(self, msg: dict) -> None:
        self.sent.append(msg)

    # --- driving ----------------------------------------------------------

    async def _settle(self) -> None:
        """Let parked coroutines run. Not ``drain_tools``: a blocked run never finishes."""
        for _ in range(8):
            await asyncio.sleep(0)
        self._flush()

    def _flush(self) -> None:
        for msg in self.sent[self._shown :]:
            payload = msg["payload"]
            status = payload["status"]
            print(f"    <-- result   {msg['name']}#{msg['call_id']}  {status}")
            detail = {k: v for k, v in payload.items() if k != "status"}
            if detail:
                print(f"        {detail}")
        self._shown = len(self.sent)

    async def says(self, text: str) -> None:
        print(f"  user  : {text!r}")
        await self.session.handle_event(UserTranscript(text=text, is_final=True))
        await self._settle()

    async def calls_tool(self, name: str, **args) -> str:
        self._call_no += 1
        call_id = f"c{self._call_no}"
        print(f"    --> call     {name}#{call_id}  {args}")
        await self.session.handle_event(
            ToolCallRequest(call_id=call_id, name=name, args=args)
        )
        await self._settle()
        return call_id

    async def turn_end(self) -> None:
        await self.session.handle_event(TurnComplete())
        await self._settle()

    def runs(self) -> list[str]:
        return [
            f"{e.meta.get('run_id', '-')} {e.meta['phase']}"
            + (f" ({e.meta['outcome']})" if "outcome" in e.meta else "")
            + (f" {e.meta['tool_name']}" if "tool_name" in e.meta else "")
            for e in self.log.filter(types=[EventType.TOOL_RUN])
        ]


def title(n: int, text: str) -> None:
    print(f"\n{RULE}\n{n}. {text}\n{RULE}")


# --- the scenarios ---------------------------------------------------------


async def happy_path() -> None:
    title(1, "consent granted — the answer rides the resume call")
    w = Wire()
    await w.says("can you tell me in english what's written on this sign board?")
    await w.calls_tool("look_and_tell", question="what does this sign board say in english?")
    print("  agent : (asks) 'can I take a photo?'")
    await w.says("yes")
    # The model answers on behalf of the user. Note the tool name on the wire is
    # provide_input, but the *result* it carries is look_and_tell's.
    await w.calls_tool(
        "provide_input", for_tool="look_and_tell", key="camera_consent", bool_value=True
    )
    print(f"  runs  : {w.runs()}")


async def refusal() -> None:
    title(2, "consent refused — no recording, and the model is told so")
    w = Wire()
    await w.says("start recording this meeting")
    await w.calls_tool("record_meeting", title="standup")
    print("  agent : (asks) 'shall I start recording?'")
    await w.says("no, don't")
    await w.calls_tool(
        "provide_input",
        for_tool="record_meeting",
        key="recording_consent",
        bool_value=False,
    )
    print(f"  runs  : {w.runs()}")


async def direct() -> None:
    title(3, "no consent needed — one call, one result")
    w = Wire()
    await w.says("what's the time?")
    await w.calls_tool("get_date_and_time")
    print(f"  runs  : {w.runs()}")


async def topic_change() -> None:
    title(4, "user changes the subject mid-question — the old run is dropped silently")
    w = Wire()
    await w.says("what does this sign say?")
    await w.calls_tool("look_and_tell", question="what does this sign say?")
    print("  agent : (asks) 'can I take a photo?'")
    await w.says("actually never mind, what's the date?")
    # Newer call from the same agent takes the slot. The blocked run had no open call
    # left to close, so nothing is sent for it — the user simply never hears about it.
    await w.calls_tool("get_date_and_time")
    print("  user  : (late) 'yeah go ahead' -> a stale answer arrives")
    await w.calls_tool(
        "provide_input", for_tool="look_and_tell", key="camera_consent", bool_value=True
    )
    print(f"  runs  : {w.runs()}")


async def misaddressed() -> None:
    title(5, "answer addressed to the wrong tool — rejected, run stays answerable")
    w = Wire()
    await w.says("record the meeting")
    await w.calls_tool("record_meeting")
    await w.calls_tool(
        "provide_input", for_tool="look_and_tell", key="camera_consent", bool_value=True
    )
    print("  (run still blocked — the right answer still works)")
    await w.calls_tool(
        "provide_input",
        for_tool="record_meeting",
        key="recording_consent",
        bool_value=True,
    )
    print(f"  runs  : {w.runs()}")


async def wrong_type() -> None:
    title(6, "answer in the wrong slot — retriable, then corrected")
    w = Wire()
    await w.says("record the meeting")
    await w.calls_tool("record_meeting")
    await w.calls_tool(
        "provide_input", for_tool="record_meeting", key="recording_consent", text_value="yes"
    )
    print("  (invalid_args is retriable and the run never left BLOCKED)")
    await w.calls_tool(
        "provide_input",
        for_tool="record_meeting",
        key="recording_consent",
        bool_value=True,
    )
    print(f"  runs  : {w.runs()}")


async def expiry() -> None:
    title(7, "nobody answers — the run expires on its own budget")
    w = Wire()
    await w.says("record the meeting")
    await w.calls_tool("record_meeting")
    expired = w.session.sweep_runs(now=time.time() + 3600)
    print(f"  swept : {[r.run_id for r in expired]} (nothing is sent — no call is open)")
    await w._settle()
    print(f"  runs  : {w.runs()}")


async def main() -> None:
    for scenario in (
        happy_path,
        refusal,
        direct,
        topic_change,
        misaddressed,
        wrong_type,
        expiry,
    ):
        await scenario()
    print()


if __name__ == "__main__":
    asyncio.run(main())
