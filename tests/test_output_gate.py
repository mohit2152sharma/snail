"""Tests for OutputGate — GATE 2 token + single-writer ring (docs 05)."""

from __future__ import annotations

import pytest

from snail.router import OutputGate


def test_grant_and_single_writer() -> None:
    gate = OutputGate(depth=4)
    gate.grant("a")
    assert gate.holder == "a"
    assert gate.write("a", "f1") is True
    # non-holder write suppressed
    assert gate.write("b", "f2") is False
    assert len(gate) == 1
    assert gate.stats["suppressed_total"] == 1


def test_grant_conflict_requires_transfer() -> None:
    gate = OutputGate()
    gate.grant("a")
    with pytest.raises(RuntimeError):
        gate.grant("b")


def test_transfer_is_atomic() -> None:
    gate = OutputGate()
    gate.grant("a")
    old = gate.transfer("b")
    assert old == "a"
    assert gate.holder == "b"
    assert gate.write("b", "x") is True
    assert gate.write("a", "y") is False  # old holder now suppressed


def test_pop_drains_in_order() -> None:
    gate = OutputGate(depth=8)
    gate.grant("a")
    gate.write("a", "f1")
    gate.write("a", "f2")
    assert gate.pop() == "f1"
    assert gate.pop() == "f2"
    assert gate.pop() is None


def test_flush_drops_all_with_callback() -> None:
    dropped = []
    gate = OutputGate(depth=8, on_drop=dropped.append)
    gate.grant("a")
    gate.write("a", "f1")
    gate.write("a", "f2")
    assert gate.flush() == 2
    assert dropped == ["f1", "f2"]
    assert len(gate) == 0


def test_full_ring_drops_oldest() -> None:
    dropped = []
    gate = OutputGate(depth=2, on_drop=dropped.append)
    gate.grant("a")
    for f in ("f1", "f2", "f3"):
        gate.write("a", f)
    assert dropped == ["f1"]  # oldest evicted
    assert [gate.pop(), gate.pop()] == ["f2", "f3"]


def test_revoke_stops_writes_keeps_ring() -> None:
    gate = OutputGate()
    gate.grant("a")
    gate.write("a", "f1")
    assert gate.revoke() == "a"
    assert gate.holder is None
    assert gate.write("a", "f2") is False  # nobody holds the token now
    assert gate.pop() == "f1"  # ring intact (revoke != flush)
