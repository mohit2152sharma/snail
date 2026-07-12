"""Tests for the Router mechanism — promote/demote/seam/barge-in (docs 05)."""

from __future__ import annotations

from snail.audio import AudioSource, FanoutBus, FramePool
from snail.registry import ToolCallRegistry
from snail.router import (
    ChainPolicy,
    ControlToolPolicy,
    OutputGate,
    ProgrammaticPolicy,
    Router,
    RoutingAction,
    RoutingDecision,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
    Seam,
)
from snail.router.signals import AgentRole, HealthState
from snail.vendor import ResponseModality


def _make(policy=None, registry=None, **hooks):
    pool = FramePool(capacity=32, slab_samples=4)
    bus = FanoutBus(pool)
    gate = OutputGate()
    router = Router(gate=gate, bus=bus, registry=registry, policy=policy, **hooks)
    return router, gate, bus


def _register_pair(router):
    router.register_agent(
        "main", "s_main",
        modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.register_agent(
        "billing", "s_bill",
        modality=ResponseModality.TEXT,  # text listener → needs flip on promote
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    router.add_listener("billing")


def test_set_active_grants_token_and_subscribes() -> None:
    router, gate, bus = _make()
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    assert gate.holder == "main"
    assert bus.get("main") is not None
    assert router.active_id == "main"


def test_transfer_at_turn_end_deferred_then_fires() -> None:
    promotes: list[tuple[str, bool]] = []
    demotes: list[str] = []
    router, gate, bus = _make(
        policy=ChainPolicy([ControlToolPolicy(seam=Seam.AT_TURN_END)]),
        on_promote=lambda a, flip: promotes.append((a, flip)),
        on_demote=demotes.append,
    )
    _register_pair(router)

    d = router.handle(
        RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.TRANSFER_TO, target="billing"))
    )
    assert d.action is RoutingAction.HANDOFF
    # deferred: token still with main, transfer pending
    assert gate.holder == "main"
    assert router.pending == ("billing", Seam.AT_TURN_END)

    fired = router.on_turn_end()
    assert fired is True
    assert gate.holder == "billing"
    assert router.active_id == "billing"
    assert router.role_of("main") is AgentRole.LISTENER  # demote-to-listener
    assert router.role_of("billing") is AgentRole.ACTIVE
    assert promotes == [("billing", True)]  # text listener → needs_flip True
    assert demotes == ["main"]


def test_cut_now_transfers_immediately_and_cancels() -> None:
    cancels: list[str] = []
    registry = ToolCallRegistry()
    router, gate, bus = _make(
        policy=ProgrammaticPolicy(),  # we push a CUT_NOW decision
        registry=registry,
        on_vendor_cancel=cancels.append,
    )
    _register_pair(router)
    # main has an in-flight tool call
    registry.register("c1", "f", {}, origin_connection_id="main", now=0.0)
    gate.write("main", "half-sentence")  # queued audio

    router._policy.push(  # type: ignore[attr-defined]
        RoutingDecision(action=RoutingAction.HANDOFF, target="billing", seam=Seam.CUT_NOW)
    )
    router.handle(RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC)))

    assert gate.holder == "billing"  # immediate
    assert len(gate) == 0  # flushed
    assert cancels == ["main"]  # vendor cancel on the demoted agent
    assert "c1" not in registry  # its calls swept


def test_health_gate_blocks_stale_promotion() -> None:
    router, gate, bus = _make(policy=ProgrammaticPolicy())
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.register_agent(
        "stale", "s2", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
        health=HealthState.STALE,
    )
    router.set_active("main")
    router._policy.push(  # type: ignore[attr-defined]
        RoutingDecision(action=RoutingAction.HANDOFF, target="stale", seam=Seam.CUT_NOW)
    )
    router.handle(RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC)))
    # never promote a stale socket (docs 02)
    assert gate.holder == "main"
    assert router.active_id == "main"
    assert router.last_block == ("stale", "health=stale")


def test_audio_listener_promotes_without_flip() -> None:
    promotes: list[tuple[str, bool]] = []
    router, gate, bus = _make(
        policy=ProgrammaticPolicy(),
        on_promote=lambda a, flip: promotes.append((a, flip)),
    )
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.register_agent(
        "audio_listener", "s2", modality=ResponseModality.AUDIO,  # already audio!
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    router.add_listener("audio_listener")
    router._policy.push(  # type: ignore[attr-defined]
        RoutingDecision(action=RoutingAction.HANDOFF, target="audio_listener", seam=Seam.CUT_NOW)
    )
    router.handle(RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC)))
    assert promotes == [("audio_listener", False)]  # no text→audio flip needed


def test_barge_in_flushes_cancels_sweeps_keeps_token() -> None:
    cancels: list[str] = []
    registry = ToolCallRegistry()
    router, gate, bus = _make(registry=registry, on_vendor_cancel=cancels.append)
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.set_active("main")
    registry.register("c1", "f", {}, response_group_id="g1", now=0.0)
    gate.write("main", "audio")

    router.barge_in(response_group_id="g1")
    assert len(gate) == 0  # flushed
    assert cancels == ["main"]  # vendor cancel
    assert "c1" not in registry  # response group swept
    assert gate.holder == "main"  # token stays — not a handoff


def test_fanout_add_and_remove() -> None:
    router, gate, bus = _make(policy=ProgrammaticPolicy())
    router.register_agent(
        "main", "s", modality=ResponseModality.AUDIO,
        input_source=AudioSource.USER_CLEAN, target_rate=16000,
    )
    router.register_agent(
        "l1", "s2", modality=ResponseModality.TEXT,
        input_source=AudioSource.USER_RAW, target_rate=16000,
    )
    router.set_active("main")

    router._policy.push(  # type: ignore[attr-defined]
        RoutingDecision(action=RoutingAction.FANOUT_ADD, target="l1")
    )
    router.handle(RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC)))
    assert bus.get("l1") is not None
    assert bus.get("l1").source is AudioSource.USER_RAW  # from its record

    router._policy.push(  # type: ignore[attr-defined]
        RoutingDecision(action=RoutingAction.FANOUT_REMOVE, target="l1")
    )
    router.handle(RoutingSignal(event=RoutingEvent(kind=RoutingEventKind.PROGRAMMATIC)))
    assert bus.get("l1") is None
