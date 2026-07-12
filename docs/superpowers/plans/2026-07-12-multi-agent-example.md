# Multi-Agent Example (host + echo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A runnable multi-agent voice example: a **host** Gemini agent (default active) with a `start_echo` tool that hands off to an **echo** Gemini agent (repeats the user); echo has a `stop` tool that hands back to host. Driven by the existing shared frontend over the WebSocket contract.

**Architecture:** A FastAPI WS server composes snail primitives — two `AgentConnection`s from a `ConnectionPool`, one shared `Router` + `ToolCallRegistry`, one `AudioPipeline` (shared fanout/gate/jitter, per-connection `OpusCodec`), and one `Session` per connection sharing the router. A custom multi-connection bridge pumps client Opus↔agents and token-gates egress. Neutral events are translated to the frontend's 9-type JSON. Handoff is tool-result-driven via `RulePolicy`.

**Tech Stack:** Python 3.14, snail package, FastAPI/uvicorn (already deps), `snail.audio.opus_codec.OpusCodec` (opuslib), google-genai (Gemini Dev API, `GEMINI_API_KEY`).

## Global Constraints

- All under `examples/multi-agent/backend/`. Do NOT modify the snail package.
- Gemini Dev API only; `GEMINI_API_KEY` from env. Model `gemini-2.5-flash-live`.
- Wire contract fixed by the frontend (`docs/superpowers/specs/2026-07-12-multi-agent-frontend-design.md`): binary WS = Opus (uplink 48k, downlink 48k — see frontend fix in Task 7); text WS = JSON control/events.
- `OpusCodec` is 48k-native, per-connection (one instance per stream); import from `snail.audio.opus_codec`.
- One asyncio loop per session (snail invariant); no locks.

---

### Task 1: Agent specs

**Files:** Create `examples/multi-agent/backend/agents.py`

- [ ] Define `HOST_ID="host"`, `ECHO_ID="echo"`.
- [ ] `build_host_spec()` → `AgentSpec(id="host", backend=Backend.GEMINI_DEV, setup=SetupParam(model="gemini-2.5-flash-live", response_modality=AUDIO, input_source=InputSource.RAW, system_instruction="You are a friendly host assistant. Converse normally. When the user asks to start echo mode, call the start_echo tool.", tools=(ToolSpec(name="start_echo", description="Switch to echo mode where the user's words are repeated back."),)))`.
- [ ] `build_echo_spec()` → same shape, `id="echo"`, `system_instruction="You are an echo bot. Repeat back verbatim exactly what the user says, nothing else. When the user says stop, call the stop tool.", tools=(ToolSpec(name="stop", description="Leave echo mode and return to the host."),)`.
- [ ] `SPECS = {HOST_ID: build_host_spec(), ECHO_ID: build_echo_spec()}`; `resolve_spec(name) -> SPECS[name]`.
- [ ] **Verify:** `python -c "from examples.multi_agent.backend.agents import SPECS; print(list(SPECS))"` prints `['host','echo']`. (Run from repo root with `PYTHONPATH=.`; note the import path uses the on-disk dir — adjust to a `sys.path` insert in `app.py` if the hyphen blocks import; see Task 6.)

---

### Task 2: Tools

**Files:** Create `examples/multi-agent/backend/tools.py`

- [ ] `host_tools() -> ToolRegistry`: register `Tool(name="start_echo", handler=lambda args: {"ok": True}, input_schema={"type":"object","properties":{}}, output_schema=None)`.
- [ ] `echo_tools() -> ToolRegistry`: register `Tool(name="stop", handler=lambda args: {"ok": True}, ...)`.
- [ ] Handlers are trivial — their **only** job is to resolve so the Session fires a `TOOL_RESULT` routing signal (the router does the actual handoff, Task 3).
- [ ] **Verify:** `python -c "..."` builds both registries, `"start_echo" in host_tools()` is True.

---

### Task 3: Routing policy

**Files:** Create `examples/multi-agent/backend/routing.py`

- [ ] `build_policy() -> RulePolicy` with two rules keyed on tool-result event:
  - `Rule(when=<event.kind==TOOL_RESULT and event.tool_name=="start_echo" and event.status=="success">, then=RoutingDecision(action=HANDOFF, target="echo", seam=Seam.AT_TURN_END))`
  - `Rule(... tool_name=="stop" ... then target="host")`
- [ ] Use the package's declarative predicate surface (`{field, op, value}` per docs 05 `RulePolicy`); if the exact predicate constructor differs, fall back to a callable predicate reading `signal.event`.
- [ ] **Verify:** feed a hand-built `RoutingSignal` (TOOL_RESULT, tool_name="start_echo", status="success") to `build_policy().decide(sig)` → returns `HANDOFF target="echo"`; `stop` → `target="host"`; unrelated → `None`.

---

### Task 4: Event translator

**Files:** Create `examples/multi-agent/backend/events.py`

- [ ] `to_client_json(ev, *, agent_id) -> dict | None` mapping neutral events to the 9-type schema:
  - `UserTranscript`→`{type:"user_transcript", text, is_final, ts}`
  - `AgentTranscript`→`{type:"agent_transcript", agent_id, text, is_final, ts}`
  - `ToolCallRequest`→`{type:"tool_call", agent_id, tool_name:name, call_id, args, ts}`
  - `TurnComplete`→`{type:"turn_complete", ts}`; `Interrupted`→`{type:"interrupted", ts}`; `GoAway`→`{type:"go_away", time_left_ms, ts}`
  - others → `None` (skip)
- [ ] `active_agent_changed(agent_id)` + `tool_result(...)` + `error(code,message)` helper builders (fired from bridge/router hooks, not vendor events).
- [ ] `ts` = `int(time.time()*1000)`.
- [ ] **Verify:** unit-call each branch, assert dict shape matches the frontend `EVENT_TYPES`.

---

### Task 5: Multi-connection bridge

**Files:** Create `examples/multi-agent/backend/bridge.py`

- [ ] `class MultiAgentBridge` holding: `socket` (FastAPI WebSocket), `pool`, `router`, `pipeline`, `sessions: {id: Session}`, `connections: {id: AgentConnection}`, an `emit(dict)` for client JSON (via `websocket.send_text(json.dumps(...))`), and one `OpusCodec` per connection for the client leg.
- [ ] **Start:** acquire host + echo connections from pool; `activate()` both; `router.register_agent` each (host `set_active`); `router.on_promote` hook → `pipeline.hold_token(id)` + `emit(active_agent_changed(id))`; build a `Session` per connection sharing `router` + one `ToolCallRegistry`, `send=lambda fr, c=conn: c.send_tool_result(fr)`; `pipeline.attach_consumer` both (source=USER_RAW, target_rate=16000). Send frontend `ready`-equivalent if needed (frontend has none → skip). Emit initial `active_agent_changed("host")`.
- [ ] **client→agents pump:** on binary → `pipeline.on_client_audio(bytes)` (Opus decode+resample+fanout); `for cid, chunks in pipeline.drain().items(): for ch in chunks: await connections[cid].send_realtime(MediaChunk.audio(ch, sample_rate=16000))`. On text control: `start`→(no-op, already started), `stop`→teardown, `mute`→set a flag gating `on_client_audio`, `barge_in`→`pipeline.cut()`+`router.barge_in()`, `handoff`→`router.handle(programmatic HANDOFF target)`, `text`→`active_conn.send_turns([user text item], complete=True)`.
- [ ] **agents→client pumps:** for each connection run `conn.run(on_message=self._on_msg(id), on_audio=self._on_audio(id))`.
  - `_on_audio(id)(pcm)`: `if id != router.active_id: return`; `pipeline.on_vendor_audio(pcm, vendor_rate=conn.adapter.capabilities.output_sample_rate)`; drain `pipeline.playout(id)` → `await socket.send_bytes(frame)` (Opus-encoded).
  - `_on_msg(id)(raw)`: `for ev in conn.adapter.parse_event(raw): j = to_client_json(ev, agent_id=id); if j: emit(j)`; then `await sessions[id].on_vendor_raw(raw)` (drives tools + routing → handoff). On `Interrupted`, `emit(interrupted)`.
- [ ] **Teardown:** cancel run tasks, `session.aclose()` both, `pool.release` both.
- [ ] **Verify (deferred to Task 7 live run)** — this task ends when the module imports and type-checks; behavior is exercised end-to-end in Task 7.

---

### Task 6: FastAPI app + entrypoint

**Files:** Create `examples/multi-agent/backend/app.py`, `examples/multi-agent/backend/__init__.py`

- [ ] `create_app()`: build a `ConnectionPool(connector=GeminiConnector(client=GeminiAdapter.build_client(Backend.GEMINI_DEV, api_key=os.environ["GEMINI_API_KEY"]), adapter=GeminiAdapter()), max_warm=4)`. Note: one adapter/connector pair suffices since both specs share the Dev backend + model.
- [ ] `@app.websocket("/ws")`: instantiate `MultiAgentBridge(socket=ws, pool=pool, ...)` (building shared `Router` with `build_policy()`, `AudioPipeline` with per-connection `OpusCodec`, `client_rate=48000`), `await ws.accept()`, `await bridge.run()`; on disconnect, teardown.
- [ ] Lifespan: `pool.aclose()` on shutdown.
- [ ] `if __name__=="__main__": uvicorn.run("app:app", host="0.0.0.0", port=8000)` (also add a `sys.path.insert(0, repo_root)` shim so the hyphenated dir imports cleanly).
- [ ] **Verify:** `GEMINI_API_KEY=... uvicorn examples...app:app` boots without error; `curl` the docs route or check the startup log prints the WS route.

---

### Task 7: Frontend downlink fix + end-to-end run + docs

**Files:** Modify `examples/frontend/src/audio/downlink.js`; update `examples/multi-agent/README.md`

- [ ] In `downlink.js` change `SAMPLE_RATE = 24000` → `48000` (opus is 48k-native; backend `OpusCodec` emits 48k). Rebuild frontend, `npm test` still green.
- [ ] **Live run:** terminal A `GEMINI_API_KEY=... python examples/multi-agent/backend/app.py`; terminal B `cd examples/frontend && npm run dev`; open Chrome `http://localhost:5173/?title=Multi-Agent&ws=ws://localhost:8000/ws&agents=host,echo`.
- [ ] **Verify end-to-end:**
  - Speak → host replies (audio + `agent_transcript` on timeline), `active → host`.
  - Say "start echo" → `tool_call start_echo` row → after host's sentence, `active → echo` → now echo repeats what you say.
  - Say "stop" → `tool_call stop` → `active → host` → host resumes.
  - Barge-in cuts playout; mute stops uplink.
- [ ] Update `examples/multi-agent/README.md` with the real backend run command + the host/echo/start/stop script.
- [ ] Commit.

---

## Notes for the implementer

- Live Gemini required (real API key) — there is no offline path for Task 7; the mock backend does not exercise the multi-agent runtime.
- The demoted agent stays a `LISTENER` (still subscribed to user audio, still generating, output suppressed by the gate). For this two-agent demo that is acceptable; if the idle agent's chatter is distracting, `router.remove_listener(old)` in the `on_demote` hook to fully silence it (costs a re-subscribe on handoff back).
- If the declarative `RulePolicy` predicate constructor is unclear, use a callable predicate `lambda sig: sig.event.kind is TOOL_RESULT and sig.event.tool_name == "start_echo"` — the package supports a callable escape hatch (docs 05).
