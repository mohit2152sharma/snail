"""Session.on_events dispatches already-parsed events without a second parse."""

from __future__ import annotations

from snail.context import EventType
from snail.vendor import UserTranscript

from test_session import _wire


async def test_on_events_dispatches_parsed_events():
    session, ctx = _wire()
    await session.on_events([UserTranscript(text="hi", is_final=True)])
    assert any(e.type is EventType.USER_SPEECH for e in ctx["log"])
