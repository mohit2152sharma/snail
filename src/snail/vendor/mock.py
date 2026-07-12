"""MockVendorAdapter — deterministic, key-free vendor stand-in (see docs 09§E).

Resolves ``09§E TODO(doc-test-strategy)``: seam / router / registry tests need a
scripted, deterministic vendor. This adapter does real translation (so serialization
is exercised) plus two test affordances:

* it **records** everything the framework sent (setups, items, tool results), so a
  test can assert on it;
* ``parse_event`` accepts a small documented **mock wire schema** so a test can drive
  neutral events without a socket.

Capabilities are injectable — default is the **Gemini Developer API** profile (native
async tools + resumption, no system content turn, no mid-session config update), so
the awkward branches (SYSTEM down-convert, reconnect-to-flip) are on by default.

Mock wire schema for :meth:`parse_event` (``raw["type"]`` selects the event):
    ``user_transcript``  {text, final}          → UserTranscript
    ``agent_transcript`` {text, final}          → AgentTranscript
    ``tool_call``        {call_id, name, args}  → ToolCallRequest
    ``turn_complete``    {}                      → TurnComplete
    ``interrupted``      {}                      → Interrupted   (barge-in)
    ``go_away``          {time_left_ms}          → GoAway
    ``resumption``       {handle}                → ResumptionUpdate
    ``error``            {code, message}         → VendorError
    (anything else)                              → []  (ignored)
"""

from __future__ import annotations

from snail.context import Item, Role

from .capabilities import Backend, VendorCapabilities
from .media import MediaChunk, RealtimeControl
from .events import (
    AgentTranscript,
    GoAway,
    Interrupted,
    ParsedEvent,
    ResumptionUpdate,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
    VendorError,
)
from .params import SetupParam

_GEMINI_DEV_PROFILE = VendorCapabilities(
    vendor="mock",
    model="mock-live",
    backend=Backend.MOCK,
    native_async_tools=True,
    session_resumption=True,
    system_content_turn=False,
    mid_session_config_update=False,
    item_truncate=False,
    input_sample_rate=16000,
    output_sample_rate=24000,
)


class MockVendorAdapter:
    """A deterministic :class:`~snail.vendor.base.VendorAdapter` for tests."""

    def __init__(self, capabilities: VendorCapabilities | None = None) -> None:
        self._caps = capabilities or _GEMINI_DEV_PROFILE
        # recorders — what the framework pushed to this "vendor"
        self.sent_setups: list[dict] = []
        self.sent_items: list[dict] = []
        self.sent_tool_results: list[dict] = []
        self.sent_realtime: list[dict] = []
        self.sent_controls: list[str] = []
        self.sent_turns: list[dict] = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> VendorCapabilities:
        return self._caps

    def build_setup(self, setup: SetupParam) -> dict:
        msg = {
            "type": "setup",
            "model": setup.model,
            "voice": setup.voice,
            "system_instruction": setup.system_instruction,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "non_blocking": t.non_blocking,
                }
                for t in setup.tools
            ],
            "response_modality": setup.response_modality.value,
            "input_source": setup.input_source.value,
        }
        self.sent_setups.append(msg)
        return msg

    def serialize_item(self, item: Item) -> dict:
        # Down-convert SYSTEM content turns for vendors that forbid them (Gemini).
        if item.role is Role.SYSTEM and not self._caps.system_content_turn:
            msg: dict = {
                "role": "user",
                "text": f"[system] {item.text}",
                "_downconverted": True,
            }
        else:
            msg = {"role": item.role.value, "text": item.text}
        if item.name is not None:
            msg["name"] = item.name
        if item.tool_call_id is not None:
            msg["tool_call_id"] = item.tool_call_id
        if item.args is not None:
            msg["args"] = item.args
        self.sent_items.append(msg)
        return msg

    def serialize_history(self, items: list[Item]) -> list[dict]:
        return [self.serialize_item(i) for i in items]

    def serialize_realtime(self, chunk: MediaChunk) -> dict:
        msg = {
            "kind": chunk.kind.value,
            "data": chunk.data,
            "text": chunk.text,
            "mime_type": chunk.mime_type,
            "sample_rate": chunk.sample_rate,
        }
        self.sent_realtime.append(msg)
        return msg

    def serialize_realtime_control(self, control: RealtimeControl) -> dict:
        self.sent_controls.append(control.value)
        return {"control": control.value}

    def serialize_turns(self, items: list[Item], *, complete: bool) -> dict:
        msg = {
            "turns": [self.serialize_item(i) for i in items],
            "turn_complete": complete,
        }
        self.sent_turns.append(msg)
        return msg

    def serialize_tool_result(
        self, *, call_id: str, name: str, content: str, meta: dict | None = None
    ) -> dict:
        msg = {
            "type": "tool_result",
            "call_id": call_id,
            "name": name,
            "content": content,
            "meta": meta,
        }
        self.sent_tool_results.append(msg)
        return msg

    def parse_event(self, raw: dict) -> list[ParsedEvent]:
        kind = raw.get("type")
        if kind == "user_transcript":
            return [UserTranscript(text=raw["text"], is_final=raw.get("final", False))]
        if kind == "agent_transcript":
            return [AgentTranscript(text=raw["text"], is_final=raw.get("final", False))]
        if kind == "tool_call":
            return [
                ToolCallRequest(
                    call_id=raw["call_id"],
                    name=raw["name"],
                    args=raw.get("args", {}),
                )
            ]
        if kind == "turn_complete":
            return [TurnComplete()]
        if kind == "interrupted":
            return [Interrupted()]
        if kind == "go_away":
            return [GoAway(time_left_ms=raw.get("time_left_ms"))]
        if kind == "resumption":
            return [ResumptionUpdate(handle=raw["handle"])]
        if kind == "error":
            return [VendorError(code=raw.get("code", ""), message=raw.get("message", ""))]
        return []
