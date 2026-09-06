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
import json
import time
from collections.abc import Awaitable, Callable

from snail.context import EventLog, EventType
from snail.registry import (
    CallState,
    RegistryFull,
    RunSlots,
    SubmitOutcome,
    ToolCallRegistry,
    ToolRun,
)
from snail.router import (
    Router,
    RoutingEvent,
    RoutingEventKind,
    RoutingSignal,
)
from snail.tools import (
    PROVIDE_INPUT,
    InputRequired,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    execute,
    extract_value,
)
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
        agent_id: str | None = None,
        runs: RunSlots | None = None,
        on_goaway: Callable[[GoAway], None] | None = None,
        on_resumption: Callable[[str], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._log = log
        self._tools = tools
        self._registry = registry
        self._router = router
        self._send = send
        #: The agent this session's connection serves. A multi-agent bridge runs one
        #: session per connection, so this — not whoever is currently active — is what
        #: names the agent a tool call arrived on (docs 14's correlation key).
        self._agent_id = agent_id
        #: Multi-step runs, one slot per agent (docs 14). Shared across the sessions of
        #: a multi-agent bridge when passed in; private otherwise.
        self._runs = runs if runs is not None else RunSlots()
        self._on_goaway = on_goaway
        self._on_resumption = on_resumption

        self._group_counter = 0
        self._current_group = "r0"
        self._tool_tasks: dict[str, asyncio.Task] = {}
        self._run_tasks: dict[str, asyncio.Task] = {}

    # --- inbound ----------------------------------------------------------

    async def on_vendor_raw(self, raw: dict) -> None:
        """Parse one raw vendor message and dispatch every neutral event it yields."""
        await self.on_events(self._adapter.parse_event(raw))

    async def on_events(self, events: list[ParsedEvent]) -> None:
        """Dispatch already-parsed neutral events (caller parsed once — no reparse)."""
        for ev in events:
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
            await self._start_tool(ev)
        elif isinstance(ev, TurnComplete):
            self._router.on_turn_end()
            self.sweep_runs()
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

    async def _start_tool(self, ev: ToolCallRequest) -> None:
        active = self._origin()
        if ev.name == PROVIDE_INPUT:
            await self._deliver_input(ev, active)
            return
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
        # One run per agent: this call takes the slot, whatever was there is skipped.
        run, displaced = self._runs.start(
            active, ev.name, carrier_call_id=ev.call_id
        )
        self._log_run(run, "started", tool_call_id=ev.call_id)
        if displaced is not None:
            await self._discard(displaced)
        task = asyncio.create_task(self._run_tool(ev.call_id, ev.name, ev.args, run))
        self._tool_tasks[ev.call_id] = task
        self._run_tasks[run.run_id] = task
        task.add_done_callback(
            lambda t, cid=ev.call_id, rid=run.run_id: (
                self._tool_tasks.pop(cid, None),
                self._run_tasks.pop(rid, None),
            )
        )

    async def _run_tool(
        self, call_id: str, name: str, args: dict, run: ToolRun
    ) -> None:
        active = run.agent_id
        tool = self._tools.get(name)
        if tool is None:
            result: ToolResult = ToolResult.not_found(name)
        else:
            try:
                self._registry.advance(call_id, CallState.EXECUTING)
            except KeyError:
                self._runs.cancel(run)
                return  # already terminal (swept by barge-in/handoff) → stop
            ctx = ToolContext(
                lambda pending: self._block(run, pending), declared=tool.declared
            )
            result, _raw = await self._invoke_guarded(tool, args, ctx=ctx)
        # The final result rides the run's *current* carrier — the original call if the
        # run never blocked, otherwise the provide_input call that resumed it (docs 14).
        carrier = run.carrier_call_id
        self._runs.finish(run)
        self._log_run(run, "finished", status=result.status.value)
        if carrier is None:
            return  # nothing open to answer on (displaced or expired) → drop
        await self._emit_result(carrier, result, active, route_as=name)

    # --- the blocking seam ------------------------------------------------

    async def _block(self, run: ToolRun, pending: InputRequired):
        """``ctx.require`` landed here: park the run, close its carrier, hand back a future.

        This is the loop-bound half of docs 14 — :mod:`snail.registry.run` holds the
        state, the future lives here because only the session has a loop.
        """
        carrier = run.carrier_call_id
        future = asyncio.get_running_loop().create_future()
        self._runs.block(run, pending, resume=future)
        self._log_run(
            run,
            "blocked",
            key=pending.key,
            expects=pending.expects,
            ask=pending.ask,
            budget_s=pending.budget_s,
            deadline=run.deadline,
        )
        if carrier is not None:
            await self._emit_result(
                carrier,
                ToolResult.input_required(run.tool_name, pending),
                run.agent_id,
                route_as=run.tool_name,
            )
        return await future

    async def _deliver_input(self, ev: ToolCallRequest, active: str) -> None:
        """Route a ``provide_input`` call to this agent's blocked run.

        The session intercepts by name because it alone knows which agent the call
        arrived on — that connection is the whole correlation key (docs 14). Every
        rejection still closes the call, so the model is never left hanging, and leaves
        the run answerable, so a wrong answer never costs the user the right one.
        """
        args = ev.args or {}
        try:
            self._registry.register(
                ev.call_id,
                ev.name,
                args,
                origin_connection_id=active,
                response_group_id=self._current_group,
            )
        except (ValueError, RegistryFull):
            return
        self._log.append(
            EventType.TOOL_CALL,
            agent_id=active,
            meta={"tool_name": ev.name, "tool_call_id": ev.call_id, "args": args},
        )
        run = self._runs.blocked(active)
        if run is None or run.pending is None:
            self._log_run_miss(active, SubmitOutcome.NO_BLOCKED_RUN, args)
            await self._emit_result(
                ev.call_id, ToolResult.skipped("no input was expected"), active
            )
            return
        value, err = extract_value(args, run.pending.expects)
        if err is not None:
            await self._emit_result(ev.call_id, ToolResult.invalid_args(err), active)
            return
        # This call carries the run's next output — set before submitting, because the
        # handler resumes as soon as the future resolves.
        run.carrier_call_id = ev.call_id
        outcome, _ = self._runs.submit(
            active,
            for_tool=str(args.get("for_tool") or ""),
            key=str(args.get("key") or ""),
            value=value,
        )
        self._log_run(run, "submit", outcome=outcome.value, tool_call_id=ev.call_id)
        if outcome is SubmitOutcome.ACCEPTED:
            return  # the parked handler owns this call now
        run.carrier_call_id = None
        if outcome is SubmitOutcome.TYPE_MISMATCH:
            reject = ToolResult.invalid_args(
                f"{run.pending.key} must be a {run.pending.expects}"
            )
        else:  # for_tool / key mismatch → stale or misaddressed; say nothing about it
            reject = ToolResult.skipped("that input was not expected")
        await self._emit_result(ev.call_id, reject, active)

    # --- one exit for every result ----------------------------------------

    async def _emit_result(
        self,
        call_id: str,
        result: ToolResult,
        active: str | None,
        *,
        route_as: str | None = None,
    ) -> None:
        """Resolve ``call_id`` exactly once, then log + send + route.

        ``route_as`` is the *run's* tool name: when a result rides a ``provide_input``
        carrier the wire name must be ``provide_input`` (the vendor correlates by it),
        but routing must still see the tool that actually produced the result.
        """
        entry = self._registry.get(call_id)
        if entry is None:
            return  # already swept → nothing to close
        name = entry.tool_name
        # First terminal wins; if already resolved, resolve() no-ops and we drop.
        if not self._registry.resolve(call_id, result):
            return
        self._log.append(
            EventType.TOOL_RESULT,
            agent_id=active,
            content=self._result_content(result),
            meta={
                "tool_name": name,
                "tool_call_id": call_id,
                "status": result.status.value,
            },
        )
        await self._send(
            self._adapter.serialize_tool_result(
                call_id=call_id, name=name, payload=result.to_payload()
            )
        )
        self._route(
            RoutingEventKind.TOOL_RESULT,
            tool_name=route_as or name,
            status=result.status.value,
            agent_id=active,
            retriable=result.retriable,
            data=result.data,
        )

    async def _invoke_guarded(
        self, tool, args: dict, ctx=None
    ) -> tuple[ToolResult, Exception | None]:
        """Apply the per-tool budget around the one authoritative envelope executor.

        The envelope itself lives in :func:`snail.tools.execute` — the session used to
        keep a second copy, which is exactly how a one-shot path and a blocking path
        drift apart (docs 14).

        ``timeout_s`` is wall-clock, so it is not applied to a tool that declares
        ``requires``: a human's thinking time would consume it. Those tools are bounded
        by ``InputRequired.budget_s`` and :meth:`sweep_runs` instead.
        """
        if tool.timeout_s is not None and not tool.requires:
            try:
                return await asyncio.wait_for(
                    execute(tool, args, ctx=ctx), tool.timeout_s
                )
            except asyncio.TimeoutError:
                return ToolResult.timeout(), None
        return await execute(tool, args, ctx=ctx)

    # --- run bookkeeping ---------------------------------------------------

    async def _discard(self, run: ToolRun) -> None:
        """Silently drop a displaced run: cancel its task, close its carrier."""
        self._log_run(run, "displaced")
        task = self._run_tasks.pop(run.run_id, None)
        if task is not None:
            task.cancel()
        carrier = run.carrier_call_id
        run.carrier_call_id = None
        if carrier is not None:
            # A blocked run has no carrier — its ask already closed one. Only a run
            # cancelled mid-execution still owes the vendor a terminal result.
            await self._emit_result(
                carrier,
                ToolResult.skipped("superseded by a newer request"),
                run.agent_id,
                route_as=run.tool_name,
            )

    def sweep_runs(self, now: float | None = None) -> list[ToolRun]:
        """Cancel blocked runs past their budget. Nothing is sent: the ask already closed
        its call, so an unanswered question owes the vendor nothing."""
        expired = self._runs.sweep_expired(now)
        for run in expired:
            self._log_run(run, "expired", waited_s=round(time.time() - run.started_at, 1))
            task = self._run_tasks.pop(run.run_id, None)
            if task is not None:
                task.cancel()
        return expired

    def _log_run(self, run: ToolRun, phase: str, **fields) -> None:
        self._log.append(
            EventType.TOOL_RUN,
            agent_id=run.agent_id,
            meta={
                "run_id": run.run_id,
                "tool_name": run.tool_name,
                "phase": phase,
                "state": run.state.value,
                **fields,
            },
        )

    def _log_run_miss(self, active: str, outcome: SubmitOutcome, args: dict) -> None:
        self._log.append(
            EventType.TOOL_RUN,
            agent_id=active,
            meta={
                "phase": "submit",
                "outcome": outcome.value,
                "for_tool": args.get("for_tool"),
                "key": args.get("key"),
            },
        )

    @staticmethod
    def _result_content(result: ToolResult) -> str:
        if result.status is ToolStatus.SUCCESS:
            return "" if result.data is None else json.dumps(result.data)
        return result.reason or result.status.value

    # --- barge-in / boundaries / lifecycle -------------------------------

    async def barge_in(self) -> None:
        """User interrupted: cancel this turn's tool tasks + drive the Router seam.

        Blocked runs are spared. The interruption *is* the user answering the question
        the run asked — cancelling here would kill every consent flow at the moment it
        was about to succeed (docs 14).
        """
        gid = self._current_group
        spared = {
            self._run_tasks.get(run.run_id)
            for agent_id in self._runs.agents()
            if (run := self._runs.blocked(agent_id)) is not None
        }
        for call_id in self._registry.group_call_ids(gid):
            task = self._tool_tasks.get(call_id)
            if task is not None and task not in spared:
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

    def _origin(self) -> str:
        """Which agent a call on this session belongs to."""
        return self._agent_id or self._router.active_id or ""

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
