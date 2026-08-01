"""Session-side wiring for multi-step tool runs (docs 14). Async — asyncio auto mode.

``tests/test_tool_runs.py`` covers the pure pieces (``RunSlots``, ``InputRequired``, the
envelope). This file covers the half that only exists on the loop: which call carries
which result, what the vendor is sent and when, and what is deliberately *not* sent.
"""

from __future__ import annotations

import asyncio
import time

from snail.audio import AudioSource, FanoutBus, FramePool
from snail.context import EventLog, EventType, Projection
from snail.registry import ToolCallRegistry
from snail.router import OutputGate, Router
from snail.session import Session
from snail.tools import (
    InputRequired,
    Tool,
    ToolRegistry,
    ToolResult,
    build_provide_input_tool,
    declared_keys,
)
from snail.vendor import (
    Interrupted,
    MockVendorAdapter,
    ResponseModality,
    ToolCallRequest,
    UserTranscript,
)

_OBJ = {"type": "object"}

CONSENT = InputRequired(key="consent", expects="boolean", ask="may I?", budget_s=30.0)


async def _confirm_then_act(args: dict, ctx) -> ToolResult:
    granted = await ctx.require("consent")
    if not granted:
        return ToolResult.blocked("declined")
    return ToolResult.success({"acted": True, "on": args.get("subject", "")})


def _blocking_tool(name: str = "act") -> Tool:
    return Tool(
        name,
        _confirm_then_act,
        input_schema={"type": "object", "properties": {"subject": {"type": "string"}}},
        output_schema=_OBJ,
        requires=(CONSENT,),
    )


def _catalog(tools: tuple[Tool, ...]) -> ToolRegistry:
    catalog = ToolRegistry()
    for tool in tools:
        catalog.register(tool)
    catalog.register(build_provide_input_tool(declared_keys(tools)))
    return catalog


def _wire(*tools: Tool):
    pool = FramePool(capacity=32, slab_samples=4)
    calls = ToolCallRegistry()
    router = Router(gate=OutputGate(), bus=FanoutBus(pool), registry=calls)
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    sent: list[dict] = []
    log = EventLog()

    async def send(msg: dict) -> None:
        sent.append(msg)

    session = Session(
        adapter=MockVendorAdapter(), log=log, tools=_catalog(tools),
        registry=calls, router=router, send=send, agent_id="main",
    )
    return session, {"sent": sent, "log": log, "calls": calls, "router": router}


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


async def _call(session, call_id: str, name: str, **args) -> None:
    await session.handle_event(ToolCallRequest(call_id=call_id, name=name, args=args))
    await _settle()


async def _answer(session, call_id: str, *, for_tool="act", key="consent", **slots) -> None:
    await _call(session, call_id, "provide_input", for_tool=for_tool, key=key, **slots)


def _runs(ctx) -> list[dict]:
    return [e.meta for e in ctx["log"].filter(types=[EventType.TOOL_RUN])]


# --- the round trip --------------------------------------------------------


async def test_block_closes_the_call_and_resume_carries_the_result() -> None:
    session, ctx = _wire(_blocking_tool())

    await _call(session, "c1", "act", subject="door")
    ask = ctx["sent"][0]
    assert ask["call_id"] == "c1" and ask["name"] == "act"
    assert ask["payload"] == {
        "status": "input_required",
        "for_tool": "act",
        "key": "consent",
        "expects": "boolean",
        "ask": "may I?",
    }
    assert ctx["calls"].in_flight == 0  # the call is closed; only the *run* is open

    await _answer(session, "c2", bool_value=True)
    done = ctx["sent"][1]
    # N requirements -> N+1 calls: the answer's call is what the result rides back on.
    assert done["call_id"] == "c2"
    assert done["name"] == "provide_input"  # wire name is the call being answered
    assert done["payload"] == {"status": "success", "data": {"acted": True, "on": "door"}}
    assert len(ctx["sent"]) == 2
    assert ctx["calls"].in_flight == 0


async def test_refusal_is_a_normal_outcome() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    await _answer(session, "c2", bool_value=False)
    assert ctx["sent"][1]["payload"] == {"status": "blocked", "reason": "declined"}


async def test_non_blocking_tool_still_answers_its_own_call() -> None:
    session, ctx = _wire(Tool("now", lambda a: {"t": 1}, output_schema=_OBJ))
    await _call(session, "c1", "now")
    assert ctx["sent"] == [
        {"type": "tool_result", "call_id": "c1", "name": "now",
         "payload": {"status": "success", "data": {"t": 1}}}
    ]
    phases = [m["phase"] for m in _runs(ctx)]
    assert phases == ["started", "finished"]


async def test_routing_sees_the_run_tool_not_the_carrier() -> None:
    seen: list[str] = []
    session, ctx = _wire(_blocking_tool())
    ctx["router"].handle = lambda signal: seen.append(signal.event.tool_name)  # type: ignore[assignment]
    await _call(session, "c1", "act")
    await _answer(session, "c2", bool_value=True)
    assert seen == ["act", "act"]  # never "provide_input"


# --- displacement ----------------------------------------------------------


async def test_newer_call_displaces_a_blocked_run_silently() -> None:
    session, ctx = _wire(_blocking_tool(), Tool("now", lambda a: {"t": 1}, output_schema=_OBJ))

    await _call(session, "c1", "act")
    await _call(session, "c2", "now")

    # The blocked run had no open call left, so displacement sends nothing for it.
    assert [m["call_id"] for m in ctx["sent"]] == ["c1", "c2"]
    assert ctx["sent"][1]["payload"]["status"] == "success"
    phases = [(m.get("run_id"), m["phase"]) for m in _runs(ctx)]
    assert ("R1", "displaced") in phases

    # A late answer to the dropped run is rejected without prompting the model.
    await _answer(session, "c3", bool_value=True)
    assert ctx["sent"][2]["payload"] == {
        "status": "skipped", "reason": "no input was expected"
    }


async def test_displacing_an_executing_run_closes_its_open_call() -> None:
    started = asyncio.Event()

    async def slow(args: dict) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    session, ctx = _wire(
        Tool("slow", slow, output_schema=_OBJ),
        Tool("now", lambda a: {"t": 1}, output_schema=_OBJ),
    )
    await _call(session, "c1", "slow")
    assert started.is_set()
    await _call(session, "c2", "now")

    by_call = {m["call_id"]: m["payload"] for m in ctx["sent"]}
    assert by_call["c1"] == {
        "status": "skipped", "reason": "superseded by a newer request"
    }
    assert by_call["c2"]["status"] == "success"


async def test_displacement_does_not_cross_agents() -> None:
    """Two connections, one shared slot table — the real multi-agent shape (docs 14).

    A bridge runs one Session per connection over shared state; each session names its
    own agent, so a call on one never displaces the other's run.
    """
    from snail.registry import RunSlots

    pool = FramePool(capacity=32, slab_samples=4)
    calls = ToolCallRegistry()
    router = Router(gate=OutputGate(), bus=FanoutBus(pool), registry=calls)
    for agent_id in ("main", "other"):
        router.register_agent(
            agent_id, "s", modality=ResponseModality.AUDIO,
            input_source=AudioSource.USER_CLEAN, target_rate=16000,
        )
    router.set_active("main")
    runs, catalog = RunSlots(), _catalog((_blocking_tool(),))
    sent: dict[str, list[dict]] = {"main": [], "other": []}

    def _session(agent_id: str) -> Session:
        async def send(msg: dict, _bucket=sent[agent_id]) -> None:
            _bucket.append(msg)

        return Session(
            adapter=MockVendorAdapter(), log=EventLog(), tools=catalog,
            registry=calls, router=router, send=send, agent_id=agent_id, runs=runs,
        )

    a, b = _session("main"), _session("other")
    await _call(a, "c1", "act", subject="A")
    await _call(b, "c2", "act", subject="B")
    assert len(runs) == 2  # b's call took b's slot, not a's

    await _answer(b, "c3", bool_value=True)
    assert sent["other"][1]["payload"] == {
        "status": "success", "data": {"acted": True, "on": "B"}
    }
    await _answer(a, "c4", bool_value=False)
    assert sent["main"][1]["payload"] == {"status": "blocked", "reason": "declined"}


# --- rejections ------------------------------------------------------------


async def test_wrong_for_tool_is_rejected_and_the_run_stays_answerable() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    await _answer(session, "c2", for_tool="something_else", bool_value=True)
    assert ctx["sent"][1]["payload"]["status"] == "skipped"
    await _answer(session, "c3", bool_value=True)
    assert ctx["sent"][2]["payload"]["status"] == "success"


async def test_wrong_key_is_rejected_and_the_run_stays_answerable() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    await _answer(session, "c2", key="other_key", bool_value=True)
    assert ctx["sent"][1]["payload"]["status"] == "skipped"
    await _answer(session, "c3", bool_value=True)
    assert ctx["sent"][2]["payload"]["status"] == "success"


async def test_wrong_slot_is_retriable() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    await _answer(session, "c2", text_value="yes")
    assert ctx["sent"][1]["payload"] == {
        "status": "invalid_args",
        "reason": "bool_value is required for an answer of type boolean",
        "retriable": True,
    }
    await _answer(session, "c3", bool_value=True)
    assert ctx["sent"][2]["payload"]["status"] == "success"


async def test_provide_input_with_no_run_is_closed_silently() -> None:
    session, ctx = _wire(_blocking_tool())
    await _answer(session, "c1", bool_value=True)
    assert ctx["sent"][0]["payload"] == {
        "status": "skipped", "reason": "no input was expected"
    }


# --- expiry + barge-in -----------------------------------------------------


async def test_expired_run_is_cancelled_and_sends_nothing() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    expired = session.sweep_runs(now=time.time() + 3600)
    await _settle()

    assert [r.run_id for r in expired] == ["R1"]
    assert len(ctx["sent"]) == 1  # only the original ask
    assert [m["phase"] for m in _runs(ctx)][-1] == "expired"
    # The slot is free, so the next request starts clean.
    await _answer(session, "c2", bool_value=True)
    assert ctx["sent"][1]["payload"]["status"] == "skipped"


async def test_barge_in_spares_a_blocked_run() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    # The user speaking *is* the answer — cancelling here would kill every consent flow.
    await session.handle_event(UserTranscript(text="yes go ahead", is_final=True))
    await session.handle_event(Interrupted())
    await _settle()
    await _answer(session, "c2", bool_value=True)
    assert ctx["sent"][1]["payload"]["status"] == "success"


async def test_run_state_never_reaches_the_model() -> None:
    session, ctx = _wire(_blocking_tool())
    await _call(session, "c1", "act")
    await _answer(session, "c2", bool_value=True)

    assert any(e.type is EventType.TOOL_RUN for e in ctx["log"].filter())
    # TOOL_RUN is a control event: the projection has no mapping for it, so run state
    # is invisible to the model by construction (docs 14).
    items = Projection().apply(ctx["log"])
    assert all("run_id" not in (i.text or "") for i in items)
