"""Session — the loop-bound orchestrator (see docs 05/06 + implementation plan).

Ties the vendor-neutral pieces together on **one asyncio loop per session** (docs 06):
it consumes :mod:`ParsedEvent`\\ s from an adapter, drives the event log, runs tools as
concurrent tasks (with timeout + cooperative cancel), resolves the
:class:`ToolCallRegistry`, feeds :class:`Router` signals, and sends vendor-bound
messages through an injected async ``send``.

This is the layer that owns the loop, so the loop-bound concerns the lower layers
deferred live here: awaiting async tool handlers, per-call timeouts (``asyncio.wait_for``
instead of the registry's ``sweep_timeouts``), task cancellation on barge-in, and
turn/idle boundary dispatch. The vendor **socket** itself is not here — it belongs to
the connection layer; the session talks to it via ``send`` + fed ``ParsedEvent``\\ s, so
it is fully testable against :class:`MockVendorAdapter`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable

from snail.context import EventLog, EventType
from snail.registry import CallState, RegistryFull, ToolCallRegistry
from snail.router import (
    Router,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
)
from snail.tools import ToolRegistry, ToolResult, ToolStatus, validate
from snail.vendor import (
    AgentTranscript,
    GoAway,
    Interrupted,
    ParsedEvent,
    ResumptionUpdate,
    ToolCallRequest,
    TurnComplete,
    UserTranscript,
    VendorAdapter,
    VendorError,
)

Send = Callable[[dict], Awaitable[None]]


class Session:
    """Orchestrates one user-session's runtime on the event loop."""

    def __init__(
        self,
        *,
        adapter: VendorAdapter,
        log: EventLog,
        tools: ToolRegistry,
        registry: ToolCallRegistry,
        router: Router,
        send: Send,
        on_goaway: Callable[[GoAway], None] | None = None,
        on_resumption: Callable[[str], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._log = log
        self._tools = tools
        self._registry = registry
        self._router = router
        self._send = send
        self._on_goaway = on_goaway
        self._on_resumption = on_resumption

        self._group_counter = 0
        self._current_group = "r0"
        self._tool_tasks: dict[str, asyncio.Task] = {}

    # --- inbound ----------------------------------------------------------

    async def on_vendor_raw(self, raw: dict) -> None:
        """Parse one raw vendor message and dispatch every neutral event it yields."""
        for ev in self._adapter.parse_event(raw):
            await self.handle_event(ev)

    async def handle_event(self, ev: ParsedEvent) -> None:
        """React to one neutral vendor event."""
        if isinstance(ev, UserTranscript):
            if ev.is_final:
                self._log.append(EventType.USER_SPEECH, content=ev.text)
                self._route(RoutingEventKind.USER_SPEECH_FINAL, text=ev.text)
        elif isinstance(ev, AgentTranscript):
            if ev.is_final:
                self._log.append(
                    EventType.AGENT_SPEECH,
                    agent_id=self._router.active_id,
                    content=ev.text,
                )
        elif isinstance(ev, ToolCallRequest):
            self._start_tool(ev)
        elif isinstance(ev, TurnComplete):
            self._router.on_turn_end()
            self._new_group()
        elif isinstance(ev, Interrupted):
            await self.barge_in()
        elif isinstance(ev, GoAway):
            if self._on_goaway is not None:
                self._on_goaway(ev)
        elif isinstance(ev, ResumptionUpdate):
            if self._on_resumption is not None:
                self._on_resumption(ev.handle)
        elif isinstance(ev, VendorError):
            self._log.append(
                EventType.EXTERNAL_CONTEXT,
                meta={"vendor_error": ev.code, "message": ev.message},
            )

    # --- tool execution ---------------------------------------------------

    def _start_tool(self, ev: ToolCallRequest) -> None:
        active = self._router.active_id
        try:
            self._registry.register(
                ev.call_id,
                ev.name,
                ev.args,
                origin_connection_id=active,
                response_group_id=self._current_group,
            )
        except (ValueError, RegistryFull):
            return  # duplicate call_id or in-flight cap → drop (backpressure)
        self._log.append(
            EventType.TOOL_CALL,
            agent_id=active,
            meta={"tool_name": ev.name, "tool_call_id": ev.call_id, "args": ev.args},
        )
        task = asyncio.create_task(self._run_tool(ev.call_id, ev.name, ev.args, active))
        self._tool_tasks[ev.call_id] = task
        task.add_done_callback(
            lambda t, cid=ev.call_id: self._tool_tasks.pop(cid, None)
        )

    async def _run_tool(
        self, call_id: str, name: str, args: dict, active: str | None
    ) -> None:
        tool = self._tools.get(name)
        if tool is None:
            result: ToolResult = ToolResult.not_found(name)
        else:
            try:
                self._registry.advance(call_id, CallState.EXECUTING)
            except KeyError:
                return  # already terminal (swept by barge-in/handoff) → stop
            result, _raw = await self._invoke_guarded(tool, args)
        # First terminal wins; if already swept, resolve() no-ops and we drop.
        if not self._registry.resolve(call_id, result):
            return
        content = self._result_content(result)
        self._log.append(
            EventType.TOOL_RESULT,
            agent_id=active,
            content=content,
            meta={
                "tool_name": name,
                "tool_call_id": call_id,
                "status": result.status.value,
            },
        )
        await self._send(
            self._adapter.serialize_tool_result(
                call_id=call_id,
                name=name,
                content=content,
                meta={"status": result.status.value},
            )
        )
        self._route(
            RoutingEventKind.TOOL_RESULT,
            tool_name=name,
            status=result.status.value,
            agent_id=active,
            retriable=result.retriable,
            data=result.data,
        )

    async def _invoke_guarded(
        self, tool, args: dict
    ) -> tuple[ToolResult, Exception | None]:
        if tool.timeout_s is not None:
            try:
                return await asyncio.wait_for(self._invoke(tool, args), tool.timeout_s)
            except asyncio.TimeoutError:
                return ToolResult.timeout(), None
        return await self._invoke(tool, args)

    async def _invoke(self, tool, args: dict) -> tuple[ToolResult, Exception | None]:
        err = validate(args, tool.input_schema)
        if err is not None:
            return ToolResult.invalid_args(err), None
        try:
            r = tool.handler(args)
            if inspect.isawaitable(r):
                r = await r
        except asyncio.CancelledError:
            raise  # cooperative cancel (barge-in/handoff) — let it propagate
        except Exception as exc:  # noqa: BLE001 - envelope boundary
            return ToolResult.error(f"{tool.name} failed"), exc
        out_err = validate(r, tool.output_schema)
        if out_err is not None:
            return ToolResult.invalid_output(), AssertionError(out_err)
        return ToolResult.success(r), None

    @staticmethod
    def _result_content(result: ToolResult) -> str:
        if result.status is ToolStatus.SUCCESS:
            return "" if result.data is None else json.dumps(result.data)
        return result.reason or result.status.value

    # --- barge-in / boundaries / lifecycle -------------------------------

    async def barge_in(self) -> None:
        """User interrupted: cancel this turn's tool tasks + drive the Router seam."""
        gid = self._current_group
        for call_id in self._registry.group_call_ids(gid):
            task = self._tool_tasks.get(call_id)
            if task is not None:
                task.cancel()
        self._router.barge_in(response_group_id=gid)

    def _new_group(self) -> None:
        self._group_counter += 1
        self._current_group = f"r{self._group_counter}"

    async def drain_tools(self) -> None:
        """Await all in-flight tool tasks (for tests / graceful close)."""
        tasks = list(self._tool_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel outstanding tool tasks and sweep the registry."""
        for task in list(self._tool_tasks.values()):
            task.cancel()
        await self.drain_tools()
        self._registry.sweep_all()

    # --- helpers ----------------------------------------------------------

    def _route(self, kind: RoutingEventKind, **fields) -> None:
        active = self._router.active_id
        signal = RoutingSignal(
            event=RoutingEvent(kind=kind, **fields),
            active_agent=self._router.agent_ref(active) if active else None,
        )
        self._router.handle(signal)

    @property
    def current_group(self) -> str:
        return self._current_group
