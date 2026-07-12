"""Throwaway mock backend for the playground frontend.

Runs the wire contract without a real vendor: scripted JSON events + a canned
Opus tone as binary downlink. Not a product artifact.

    python examples/frontend/mock-backend/server.py

Note: the canned tone is a placeholder; if `av`/opus encoding is unavailable it
sends silence-length Opus frames the browser decoder tolerates. The point is to
exercise the JSON + control paths and the decode/playback plumbing.
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets


def _event(type_: str, **fields) -> str:
    return json.dumps({"type": type_, "ts": int(time.time() * 1000), **fields})


async def _script(ws) -> None:
    await ws.send(_event("active_agent_changed", agent_id="gemini-a"))
    await asyncio.sleep(0.3)
    await ws.send(_event("user_transcript", text="hello", is_final=True))
    await asyncio.sleep(0.3)
    for partial in ("hi", "hi there", "hi there, how can I help?"):
        await ws.send(_event("agent_transcript", agent_id="gemini-a", text=partial, is_final=False))
        await asyncio.sleep(0.2)
    await ws.send(_event("agent_transcript", agent_id="gemini-a", text="hi there, how can I help?", is_final=True))
    await ws.send(_event("turn_complete"))


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
            await ws.send(_event("agent_transcript", agent_id="gemini-a", text=f"echo: {ctl['text']}", is_final=True))
            await ws.send(_event("turn_complete"))
        elif t == "barge_in":
            await ws.send(_event("interrupted"))
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
