# Multi-agent example — host + echo

Two Gemini agents in one snail session, one client face:

- **host** — the default active agent; normal conversation. Has a `start_echo` tool.
- **echo** — repeats back verbatim what you say. Has a `stop` tool.

Say **"start echo"** → the host calls `start_echo` → the router hands off to the echo
agent (at the end of the host's sentence). Say **"stop"** → echo calls `stop` → the
router hands back to the host, which resumes the conversation.

The shared playground frontend (`../frontend`) drives it over one WebSocket:
binary frames = Opus audio, text frames = JSON control/events.

## Run

Needs a real Gemini Developer API key (`gemini-2.5-flash-live`).

1. **Backend** (from the repo root):

       GEMINI_API_KEY=your_key PYTHONPATH=examples/multi-agent \
         python -m backend.app
       # serves ws://localhost:8000/ws

2. **Frontend**:

       cd examples/frontend && npm install && npm run dev

3. Open **Chrome** at:

       http://localhost:5173/?title=Multi-Agent&ws=ws://localhost:8000/ws&agents=host,echo

Grant mic access, click **Start**, and talk. Watch the timeline: `active → host`,
transcripts, the `start_echo` / `stop` `tool_call` rows, and `active → echo` / `active →
host` on each handoff. The **hand off** buttons force a switch manually; **Barge-in**
cuts playout; **Mute** stops the mic.

## How it maps to snail

| Piece | snail primitive |
|-------|-----------------|
| host / echo identity + tools | `AgentSpec` + `SetupParam` (`backend/agents.py`) |
| tool handlers | `Tool` / `ToolRegistry` (`backend/tools.py`) |
| `start_echo`→echo, `stop`→host | `RulePolicy` on tool results (`backend/routing.py`) |
| one active + handoff + token | `Router` + `OutputGate` (`backend/bridge.py`) |
| mic Opus → 2 agents, active → client | `AudioPipeline` (`FanoutBus` + `OutputGate` + `JitterBuffer` + `OpusCodec`) |
| per-agent event/tool loop | `Session` (one per connection, shared `Router`) |

`snail.transport.ClientBridge` is single-connection; the multi-agent runtime is composed
in `backend/bridge.py` from the primitives above.

## Chrome only

The frontend uses WebCodecs Opus (`AudioEncoder`/`AudioDecoder`).
