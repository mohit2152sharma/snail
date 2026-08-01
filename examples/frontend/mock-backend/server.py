"""Throwaway mock backend for the playground frontend.

Runs the wire contract without a real vendor: scripted JSON events + a canned
Opus tone as binary downlink. Not a product artifact.

    python examples/frontend/mock-backend/server.py

The script exercises every panel — setup stages, per-turn TTFB, a tool call with real
arguments, and a full consent round-trip (`input_required` → `provide_input`, docs 14)
— so the UI can be developed and eyeballed without a key or a microphone.

Note: the canned tone is a placeholder; if `av`/opus encoding is unavailable it
sends silence-length Opus frames the browser decoder tolerates. The point is to
exercise the JSON + control paths and the decode/playback plumbing.
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets

AGENT = "host"


def _event(type_: str, **fields) -> str:
    return json.dumps({"type": type_, "ts": int(time.time() * 1000), **fields})


async def _setup(ws) -> None:
    await ws.send(_event("setup_stage", stage="vendor_connect", agent_id=AGENT,
                         ms=412.7, detail="gemini-live-2.5-flash"))
    await asyncio.sleep(0.05)
    await ws.send(_event("setup_stage", stage="vendor_connect", agent_id="echo",
                         ms=38.2, detail="gemini-live-2.5-flash (warm standby)"))
    await ws.send(_event("setup_complete", total_ms=455.9, agents=[AGENT, "echo"]))
    await ws.send(_event("active_agent_changed", agent_id=AGENT))


async def _turn(ws, *, said: str, reply: str, ttfb_ms: float) -> None:
    """One user→agent turn, with the boundaries the TTFB clock is measured between."""
    await ws.send(_event("speech_start"))
    await asyncio.sleep(0.2)
    await ws.send(_event("user_transcript", text=said, is_final=True))
    await ws.send(_event("speech_end"))
    await asyncio.sleep(ttfb_ms / 1000.0)
    await ws.send(_event("ttfb", agent_id=AGENT, ms=ttfb_ms))
    words = reply.split()
    for i in range(1, len(words) + 1):
        await ws.send(_event("agent_transcript", agent_id=AGENT,
                             text=" ".join(words[:i]), is_final=False))
        await asyncio.sleep(0.12)
    await ws.send(_event("agent_transcript", agent_id=AGENT, text=reply, is_final=True))
    await ws.send(_event("turn_complete", agent_id=AGENT))


async def _consent_turn(ws) -> None:
    """A blocking tool: ask, wait, resume — the flow docs 14 exists for."""
    await ws.send(_event("speech_start"))
    await ws.send(_event("user_transcript", text="what does this sign say?", is_final=True))
    await ws.send(_event("speech_end"))
    await asyncio.sleep(0.3)
    await ws.send(_event("ttfb", agent_id=AGENT, ms=298.4))
    await ws.send(_event("tool_call", agent_id=AGENT, tool_name="look_and_tell",
                         call_id="c1", args={"question": "what does this sign say?"}))
    await ws.send(_event("tool_run", agent_id=AGENT, run_id="R1", phase="started",
                         state="executing", tool_name="look_and_tell"))
    await asyncio.sleep(0.15)
    await ws.send(_event("tool_run", agent_id=AGENT, run_id="R1", phase="blocked",
                         state="blocked", tool_name="look_and_tell",
                         key="camera_consent", expects="boolean"))
    await ws.send(_event("tool_result", agent_id=AGENT, tool_name="look_and_tell",
                         call_id="c1", status="input_required", content="input_required"))
    await ws.send(_event("agent_transcript", agent_id=AGENT,
                         text="May I take a photo to answer that?", is_final=True))
    await ws.send(_event("turn_complete", agent_id=AGENT))

    await asyncio.sleep(0.8)
    await ws.send(_event("speech_start"))
    await ws.send(_event("user_transcript", text="yes go ahead", is_final=True))
    await ws.send(_event("speech_end"))
    await asyncio.sleep(0.25)
    await ws.send(_event("ttfb", agent_id=AGENT, ms=246.1))
    await ws.send(_event("tool_call", agent_id=AGENT, tool_name="provide_input",
                         call_id="c2", args={"for_tool": "look_and_tell",
                                             "key": "camera_consent",
                                             "bool_value": True}))
    await ws.send(_event("tool_run", agent_id=AGENT, run_id="R1", phase="submit",
                         state="executing", tool_name="look_and_tell",
                         outcome="accepted"))
    await ws.send(_event("tool_run", agent_id=AGENT, run_id="R1", phase="finished",
                         state="done", tool_name="look_and_tell", status="success"))
    await ws.send(_event("tool_result", agent_id=AGENT, tool_name="provide_input",
                         call_id="c2", status="success",
                         content='{"answer": "The sign reads NO ENTRY."}'))
    await ws.send(_event("agent_transcript", agent_id=AGENT,
                         text="It says: no entry.", is_final=True))
    await ws.send(_event("turn_complete", agent_id=AGENT))


async def _script(ws) -> None:
    await _setup(ws)
    await asyncio.sleep(0.3)
    await _turn(ws, said="hello", reply="hi there, how can I help?", ttfb_ms=312.5)
    await asyncio.sleep(0.6)
    await _consent_turn(ws)
    await asyncio.sleep(0.6)
    await _turn(ws, said="what time is it?", reply="it is half past four.", ttfb_ms=189.3)


async def handler(ws) -> None:
    script_task: asyncio.Task | None = None
    async for msg in ws:
        if isinstance(msg, bytes):
            continue  # ignore uplink audio in the mock
        try:
            ctl = json.loads(msg)
        except ValueError:
            continue
        t = ctl.get("type")
        if t == "start":
            script_task = asyncio.create_task(_script(ws))
        elif t == "handoff":
            await ws.send(_event("active_agent_changed", agent_id=ctl["agent_id"]))
        elif t == "text":
            await ws.send(_event("user_transcript", text=ctl["text"], is_final=True))
            await ws.send(_event("speech_end"))
            await asyncio.sleep(0.2)
            await ws.send(_event("ttfb", agent_id=AGENT, ms=201.7))
            await ws.send(_event("agent_transcript", agent_id=AGENT,
                                 text=f"echo: {ctl['text']}", is_final=True))
            await ws.send(_event("turn_complete", agent_id=AGENT))
        elif t == "barge_in":
            await ws.send(_event("interrupted", agent_id=AGENT))
        elif t == "stop":
            if script_task:
                script_task.cancel()
            await ws.close()
            return


async def main() -> None:
    async with websockets.serve(handler, "localhost", 8000):
        print("mock backend on ws://localhost:8000/ws")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
