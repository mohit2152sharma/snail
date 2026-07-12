# Multi-agent example — host + echo + translate

Three Gemini agents in one snail session, one client face:

- **host** — default active agent; normal conversation. Tools: `start_echo`,
  `start_translation`.
- **echo** — repeats back verbatim what you say. Tool: `stop` (hands back to host).
- **translate** — Gemini 3.5 Live Translate: translates any language → **Hindi**.

Flow:

- Say **"start echo"** → host calls `start_echo` → router hands off to echo (at the end
  of the host's sentence). Say **"stop"** → echo calls `stop` → back to host.
- Say **"start translation"** → host calls `start_translation` → router hands off to the
  translate agent; now everything you say comes back in Hindi. Translation mode has **no
  tools**, so return to host with the **"hand off → host"** button in the UI.

The shared playground frontend (`../frontend`) drives it over one WebSocket: binary
frames = Opus audio, text frames = JSON control/events.

## Backends (important)

- **host + echo** run on `gemini-live-2.5-flash` — Vertex AI by default (ADC), served
  from the `global` location. (Set `SNAIL_GEMINI_BACKEND=dev` + `GEMINI_API_KEY` to run
  them on the Developer API instead.)
- **translate** uses `gemini-3.5-live-translate-preview`, which is **Developer-API only**
  (not on Vertex). It always needs `GEMINI_API_KEY`. Without that key the example still
  runs, but with host + echo only (translate is disabled).

So the full three-agent demo is a **mixed-backend** session: host/echo on Vertex,
translate on the Dev API.

## Run

1. **Backend** (from the repo root):

       gcloud auth application-default login       # one-time, your own terminal (Vertex ADC)
       GOOGLE_CLOUD_PROJECT=your-project \
       GEMINI_API_KEY=your_dev_api_key \
       PYTHONPATH=examples/multi-agent python -m backend.app
       # serves ws://localhost:8000/ws

   (`GOOGLE_CLOUD_LOCATION` defaults to `global`. Drop `GEMINI_API_KEY` to run host+echo
   only. `SNAIL_TRANSLATE_TARGET` overrides the target language, default `hi`.)

2. **Frontend**:

       cd examples/frontend && npm install && npm run dev

3. Open **Chrome** at:

       http://localhost:5173/?title=Multi-Agent&ws=ws://localhost:8000/ws&agents=host,echo,translate

Grant mic, click **Start**, talk. Timeline shows `active → host`, transcripts, the
`start_echo` / `start_translation` / `stop` `tool_call` rows, and `active → …` on each
handoff. **Hand off** buttons force a switch (and are how you leave translate mode);
**Barge-in** cuts playout; **Mute** stops the mic.

## How it maps to snail

| Piece | snail primitive |
|-------|-----------------|
| agent identity + tools | `AgentSpec` + `SetupParam` (`backend/agents.py`) |
| tool handlers | `Tool` / `ToolRegistry` (`backend/tools.py`) |
| `start_echo`→echo, `start_translation`→translate, `stop`→host | `RulePolicy` on tool results (`backend/routing.py`) |
| server-side VAD + translation config | `VadGeminiAdapter` / `TranslateGeminiAdapter` (`backend/adapter.py`) |
| one active + handoff + token | `Router` + `OutputGate` (`backend/bridge.py`) |
| mic Opus → agents, active → client | `AudioPipeline` (`FanoutBus` + `OutputGate` + `JitterBuffer` + `OpusCodec`) |
| per-agent event/tool loop | `Session` (one per connection, shared `Router`) |
| mixed backends (Vertex + Dev) | one `ConnectionPool` per purpose (`main`, `translate`) |

`snail.transport.ClientBridge` is single-connection; the multi-agent runtime is composed
in `backend/bridge.py` from the primitives above.

## Chrome only

The frontend uses WebCodecs Opus (`AudioEncoder`/`AudioDecoder`).
