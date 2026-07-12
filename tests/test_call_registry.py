"""Tests for ToolCallRegistry — the single-resolution invariant + sweeps (docs 04)."""

from __future__ import annotations

import pytest

from snail.registry import (
    CallState,
    Destination,
    RegistryFull,
    ToolCallRegistry,
)
from snail.tools import ToolResult, ToolStatus


def _reg(**kw) -> ToolCallRegistry:
    return ToolCallRegistry(**kw)


def test_register_creates_entry_and_indexes() -> None:
    reg = _reg()
    e = reg.register(
        "c1", "f", {"x": 1},
        origin_connection_id="conn-a", response_group_id="g1", now=0.0,
    )
    assert e.state is CallState.RECEIVED
    assert "c1" in reg and reg.in_flight == 1
    assert reg.group_size("g1") == 1


def test_duplicate_call_id_rejected() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, now=0.0)
    with pytest.raises(ValueError):
        reg.register("c1", "f", {}, now=0.0)


def test_backpressure_cap() -> None:
    reg = _reg(max_concurrent=1)
    reg.register("c1", "f", {}, now=0.0)
    with pytest.raises(RegistryFull):
        reg.register("c2", "f", {}, now=0.0)


def test_advance_transitions_and_guards() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, now=0.0)
    reg.advance("c1", CallState.VALIDATING)
    reg.advance("c1", CallState.EXECUTING)
    assert reg.get("c1").state is CallState.EXECUTING
    with pytest.raises(ValueError):
        reg.advance("c1", CallState.DONE)  # terminal must go via resolve()
    reg.resolve("c1", ToolResult.success())
    with pytest.raises(KeyError):
        reg.advance("c1", CallState.EXECUTING)  # gone


def test_single_resolution_first_wins() -> None:
    reg = _reg()
    e = reg.register("c1", "f", {}, now=0.0)
    assert reg.resolve("c1", ToolResult.success({"v": 1})) is True
    assert e.future.done()
    assert e.future.result().data == {"v": 1}
    # second resolve is a no-op (entry gone) — the invariant
    assert reg.resolve("c1", ToolResult.error()) is False
    assert "c1" not in reg


def test_cancel_resolves_cancelled() -> None:
    reg = _reg()
    e = reg.register("c1", "f", {}, now=0.0)
    assert reg.cancel("c1") is True
    assert e.state is CallState.CANCELLED
    assert e.future.result().status is ToolStatus.CANCELLED


def test_sweep_response_group_barge_in_scope() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, response_group_id="g1", now=0.0)
    reg.register("c2", "f", {}, response_group_id="g1", now=0.0)
    reg.register("c3", "f", {}, response_group_id="g2", now=0.0)
    assert reg.sweep_response_group("g1") == 2
    assert reg.group_size("g1") == 0
    assert "c3" in reg  # other group untouched


def test_sweep_connection_handoff_scope() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, origin_connection_id="A", now=0.0)
    reg.register("c2", "f", {}, origin_connection_id="B", now=0.0)
    assert reg.sweep_connection("A") == 1
    assert "c1" not in reg and "c2" in reg


def test_sweep_all_close_scope() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, now=0.0)
    reg.register("c2", "f", {}, now=0.0)
    assert reg.sweep_all() == 2
    assert reg.in_flight == 0


def test_sweep_timeouts_only_expired() -> None:
    reg = _reg()
    reg.register("live", "f", {}, deadline=100.0, now=0.0)
    reg.register("dead", "f", {}, deadline=10.0, now=0.0)
    reg.register("no_deadline", "f", {}, now=0.0)
    expired = reg.sweep_timeouts(now=50.0)
    assert expired == ["dead"]
    assert "dead" not in reg
    assert reg.get("live").future.done() is False
    assert "no_deadline" in reg


def test_late_resolve_after_close_is_dropped() -> None:
    reg = _reg()
    reg.register("c1", "f", {}, origin_connection_id="A", now=0.0)
    reg.sweep_connection("A")  # connection closed / handed off
    # a late handler result arrives → nothing to resolve, dropped
    assert reg.resolve("c1", ToolResult.success()) is False


def test_promise_done_callback_fires_on_resolve() -> None:
    reg = _reg()
    e = reg.register("c1", "f", {}, destination=Destination.HANDLER, now=0.0)
    seen = []
    e.future.add_done_callback(lambda p: seen.append(p.result().status))
    reg.resolve("c1", ToolResult.success())
    assert seen == [ToolStatus.SUCCESS]
