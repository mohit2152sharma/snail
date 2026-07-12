"""Tests for the Session orchestrator (docs 05/06). Async — pytest-asyncio auto mode."""

from __future__ import annotations

import asyncio
import json

from snail.audio import AudioSource, FanoutBus, FramePool
from snail.context import EventLog, EventType
from snail.registry import ToolCallRegistry
from snail.router import (
    ChainPolicy,
    F,
    OutputGate,
    Router,
    RoutingAction,
    RoutingDecision,
    Rule,
    RulePolicy,
    Seam,
    default_chain,
)
from snail.session import Session
from snail.tools import Tool, ToolRegistry, ToolStatus
from snail.vendor import (
    Interrupted,
    MockVendorAdapter,
    ResponseModality,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
)

_OBJ = {"type": "object"}


def _wire(*, policy=None, tools=None, cancels=None):
    pool = FramePool(capacity=32, slab_samples=4)
    bus = FanoutBus(pool)
    gate = OutputGate()
    registry = ToolCallRegistry()
    router = Router(
        gate=gate, bus=bus, registry=registry, policy=policy,
        on_vendor_cancel=(cancels.append if cancels is not None else None),
    )
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    log = EventLog()
    session = Session(
        adapter=MockVendorAdapter(),
        log=log,
        tools=tools or ToolRegistry(),
        registry=registry,
        router=router,
        send=send,
    )
    return session, {
        "log": log, "sent": sent, "registry": registry, "router": router,
        "gate": gate, "bus": bus,
    }


async def test_tool_call_success_flow() -> None:
    tools = ToolRegistry()
    tools.register(Tool("get_balance", lambda a: {"balance": 42}, output_schema=_OBJ))
    session, ctx = _wire(tools=tools)

    await session.handle_event(ToolCallRequest(call_id="c1", name="get_balance", args={}))
    await session.drain_tools()

    assert ctx["registry"].in_flight == 0  # resolved + removed
    sent = ctx["sent"]
    assert len(sent) == 1
    assert sent[0]["type"] == "tool_result"
    assert sent[0]["call_id"] == "c1"
    assert sent[0]["content"] == json.dumps({"balance": 42})
    types = [e.type for e in ctx["log"]]
    assert EventType.TOOL_CALL in types and EventType.TOOL_RESULT in types


async def test_unknown_tool_returns_not_found() -> None:
    session, ctx = _wire()
    await session.handle_event(ToolCallRequest(call_id="c1", name="ghost", args={}))
    await session.drain_tools()
    assert ctx["sent"][0]["meta"]["status"] == ToolStatus.NOT_FOUND.value


async def test_async_handler_awaited() -> None:
    async def slow_ok(a):
        await asyncio.sleep(0)
        return {"ok": True}

    tools = ToolRegistry()
    tools.register(Tool("a", slow_ok, output_schema=_OBJ))
    session, ctx = _wire(tools=tools)
    await session.handle_event(ToolCallRequest(call_id="c1", name="a", args={}))
    await session.drain_tools()
    assert ctx["sent"][0]["content"] == json.dumps({"ok": True})


async def test_tool_timeout() -> None:
    async def too_slow(a):
        await asyncio.sleep(5)
        return {}

    tools = ToolRegistry()
    tools.register(Tool("slow", too_slow, output_schema=_OBJ, timeout_s=0.01))
    session, ctx = _wire(tools=tools)
    await session.handle_event(ToolCallRequest(call_id="c1", name="slow", args={}))
    await session.drain_tools()
    assert ctx["sent"][0]["meta"]["status"] == ToolStatus.TIMEOUT.value


async def test_tool_result_drives_routing_handoff() -> None:
    # a tool result routes a signal → a rule promotes a listener (integration).
    tools = ToolRegistry()
    tools.register(Tool("go_billing", lambda a: {}, output_schema=_OBJ))
    rules = RulePolicy(
        [
            Rule(
                when=F("event.tool_name") == "go_billing",
                then=RoutingDecision(
                    action=RoutingAction.HANDOFF, target="billing", seam=Seam.CUT_NOW
                ),
            )
        ]
    )
    session, ctx = _wire(policy=default_chain(rules=rules), tools=tools)
    ctx["router"].register_agent(
        "billing", "s2", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    ctx["router"].add_listener("billing")

    await session.handle_event(ToolCallRequest(call_id="c1", name="go_billing", args={}))
    await session.drain_tools()
    assert ctx["router"].active_id == "billing"  # CUT_NOW handoff fired from tool result


async def test_barge_in_cancels_running_tool() -> None:
    started = asyncio.Event()

    async def hang(a):
        started.set()
        await asyncio.sleep(10)
        return {}

    tools = ToolRegistry()
    tools.register(Tool("hang", hang, output_schema=_OBJ))
    cancels: list[str] = []
    session, ctx = _wire(tools=tools, cancels=cancels)

    await session.handle_event(ToolCallRequest(call_id="c1", name="hang", args={}))
    await started.wait()  # tool is now awaiting
    assert ctx["registry"].in_flight == 1

    await session.barge_in()
    assert ctx["registry"].in_flight == 0  # swept (cancelled)
    assert cancels == ["main"]  # vendor cancel on active
    await session.drain_tools()  # the cancelled task settles


async def test_user_transcript_logged_and_turn_boundary() -> None:
    # user speech logs + routes; turn-complete advances the response group.
    prog_chain = ChainPolicy([])  # no policy fires
    session, ctx = _wire(policy=prog_chain)
    await session.handle_event(UserTranscript(text="hello there", is_final=True))
    assert [e.content for e in ctx["log"] if e.type is EventType.USER_SPEECH] == [
        "hello there"
    ]
    g0 = session.current_group
    await session.handle_event(TurnComplete())
    assert session.current_group != g0  # new response group
