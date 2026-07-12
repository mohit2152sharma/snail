# Playground Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared React+Vite browser playground (`examples/frontend/`) that streams mic audio (Opus) to a snail backend over one WebSocket, plays agent audio back, and renders every neutral event on a live timeline — reused across examples, with `examples/multi-agent/` as the first thin consumer.

**Architecture:** One Vite single-page app. A `useSession` hook owns the WebSocket and branches messages: binary = Opus audio (WebCodecs encode/decode), text = JSON control/events. Pure logic (protocol builders, event reducer, jitter buffer) is isolated and unit-tested with vitest; audio + UI are browser-API-bound and verified manually against a throwaway mock backend. The app is parameterized (backend WS URL, agent list, title) via `config.js` so each example points it at a different backend.

**Tech Stack:** React 18, Vite, vanilla JS (no TypeScript), WebCodecs (`AudioEncoder`/`AudioDecoder`, codec `opus`), Web Audio (`AudioContext`, `AudioWorklet`), vitest for unit tests. Python `websockets` for the mock backend only.

## Global Constraints

- Frontend lives in `examples/frontend/`; it has its own `package.json` — the node toolchain stays inside that dir, never at repo root.
- Do NOT add any dependency to the snail package `pyproject.toml`.
- Chrome-target only (WebCodecs Opus assumed available).
- Wire contract is fixed by the spec (`docs/superpowers/specs/2026-07-12-multi-agent-frontend-design.md`) and MUST NOT drift: binary WS frame = one raw Opus packet; text WS frame = JSON.
- Uplink audio: 48 kHz mono Opus. Downlink audio: 24 kHz mono Opus.
- Control types (client→server): `start` `{agents:string[]}`, `stop`, `mute` `{on:bool}`, `barge_in`, `handoff` `{agent_id:string}`, `text` `{text:string}`.
- Event types (server→client), all with `{type, ts}`: `user_transcript` `{text,is_final}`, `agent_transcript` `{agent_id,text,is_final}`, `tool_call` `{agent_id,tool_name,call_id,args}`, `tool_result` `{agent_id,tool_name,call_id,status,content}`, `turn_complete`, `interrupted`, `go_away` `{time_left_ms}`, `active_agent_changed` `{agent_id}`, `error` `{code,message}`.

---

## File Structure

```
examples/
  frontend/
    package.json
    vite.config.js
    index.html
    vitest.config.js
    src/
      main.jsx              # React root mount
      App.jsx               # layout: ControlsBar + Timeline + AgentPanel + banners
      config.js             # per-example params (WS url, agents, title) from query/env
      protocol.js           # control-message builders + event-type constants (pure)
      events.js             # pure event reducer: append / update-in-place transcripts
      useSession.js         # WS lifecycle, control dispatch, wires audio, event state
      audio/
        capture-worklet.js  # AudioWorklet processor: posts Float32 blocks
        uplink.js           # mic -> worklet -> AudioEncoder(opus) -> onFrame(bytes)
        downlink.js         # onFrame(bytes) -> AudioDecoder(opus) -> jitter buffer -> play
        jitter.js           # pure ordered playback queue math (testable)
      ui/
        ControlsBar.jsx
        Timeline.jsx
        AgentPanel.jsx
    tests/
      protocol.test.js
      events.test.js
      jitter.test.js
    mock-backend/
      server.py             # throwaway websockets mock: scripted events + canned Opus tone
      README.md
  multi-agent/
    README.md               # how to run the shared frontend against this example's backend
    config.json             # agents + title + ws url for this example
```

---

### Task 1: Scaffold the Vite app + tooling

**Files:**
- Create: `examples/frontend/package.json`
- Create: `examples/frontend/vite.config.js`
- Create: `examples/frontend/vitest.config.js`
- Create: `examples/frontend/index.html`
- Create: `examples/frontend/src/main.jsx`
- Create: `examples/frontend/src/App.jsx`
- Create: `examples/frontend/.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: a runnable `npm run dev` app and `npm test` (vitest) command. `App` default export (React component) rendering a placeholder heading.

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "snail-playground-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "vite": "^5.4.0",
    "vitest": "^2.0.0"
  }
}
```

- [ ] **Step 2: Create `vite.config.js`**

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
```

- [ ] **Step 3: Create `vitest.config.js`**

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["tests/**/*.test.js"] },
});
```

- [ ] **Step 4: Create `index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Snail Playground</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

- [ ] **Step 5: Create `src/main.jsx`**

```jsx
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";

createRoot(document.getElementById("root")).render(<App />);
```

- [ ] **Step 6: Create `src/App.jsx` (placeholder)**

```jsx
import React from "react";

export default function App() {
  return <h1>Snail Playground</h1>;
}
```

- [ ] **Step 7: Create `.gitignore`**

```
node_modules
dist
```

- [ ] **Step 8: Install and verify dev server boots**

Run: `cd examples/frontend && npm install && npm run build`
Expected: build succeeds, `dist/` produced, no errors.

- [ ] **Step 9: Commit**

```bash
git add examples/frontend
git commit -m "feat(examples): scaffold shared playground frontend (vite+react)"
```

---

### Task 2: Protocol module (control builders + event constants)

**Files:**
- Create: `examples/frontend/src/protocol.js`
- Test: `examples/frontend/tests/protocol.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `EVENT_TYPES` — frozen object of the 9 event type strings.
  - `control` — object of builder fns returning JSON-serializable control objects:
    - `control.start(agents: string[]) -> {type:"start", agents}`
    - `control.stop() -> {type:"stop"}`
    - `control.mute(on: boolean) -> {type:"mute", on}`
    - `control.bargeIn() -> {type:"barge_in"}`
    - `control.handoff(agentId: string) -> {type:"handoff", agent_id}`
    - `control.text(text: string) -> {type:"text", text}`
  - `isAudioMessage(data) -> boolean` (true for ArrayBuffer/Blob).

- [ ] **Step 1: Write the failing test**

```js
import { describe, it, expect } from "vitest";
import { control, EVENT_TYPES, isAudioMessage } from "../src/protocol.js";

describe("control builders", () => {
  it("start carries agents", () => {
    expect(control.start(["a", "b"])).toEqual({ type: "start", agents: ["a", "b"] });
  });
  it("mute carries on flag", () => {
    expect(control.mute(true)).toEqual({ type: "mute", on: true });
  });
  it("handoff maps to agent_id", () => {
    expect(control.handoff("g2")).toEqual({ type: "handoff", agent_id: "g2" });
  });
  it("text carries text", () => {
    expect(control.text("hi")).toEqual({ type: "text", text: "hi" });
  });
  it("stop and barge_in are bare", () => {
    expect(control.stop()).toEqual({ type: "stop" });
    expect(control.bargeIn()).toEqual({ type: "barge_in" });
  });
});

describe("EVENT_TYPES", () => {
  it("includes the neutral events", () => {
    expect(EVENT_TYPES.AGENT_TRANSCRIPT).toBe("agent_transcript");
    expect(EVENT_TYPES.ACTIVE_AGENT_CHANGED).toBe("active_agent_changed");
  });
});

describe("isAudioMessage", () => {
  it("true for ArrayBuffer, false for string", () => {
    expect(isAudioMessage(new ArrayBuffer(4))).toBe(true);
    expect(isAudioMessage("{}\n")).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd examples/frontend && npx vitest run tests/protocol.test.js`
Expected: FAIL — cannot resolve `../src/protocol.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/protocol.js — frontend<->backend wire contract (see spec).

export const EVENT_TYPES = Object.freeze({
  USER_TRANSCRIPT: "user_transcript",
  AGENT_TRANSCRIPT: "agent_transcript",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  TURN_COMPLETE: "turn_complete",
  INTERRUPTED: "interrupted",
  GO_AWAY: "go_away",
  ACTIVE_AGENT_CHANGED: "active_agent_changed",
  ERROR: "error",
});

export const control = {
  start: (agents) => ({ type: "start", agents }),
  stop: () => ({ type: "stop" }),
  mute: (on) => ({ type: "mute", on }),
  bargeIn: () => ({ type: "barge_in" }),
  handoff: (agentId) => ({ type: "handoff", agent_id: agentId }),
  text: (text) => ({ type: "text", text }),
};

export function isAudioMessage(data) {
  return data instanceof ArrayBuffer || (typeof Blob !== "undefined" && data instanceof Blob);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd examples/frontend && npx vitest run tests/protocol.test.js`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add examples/frontend/src/protocol.js examples/frontend/tests/protocol.test.js
git commit -m "feat(examples): playground wire-protocol builders + event constants"
```

---

### Task 3: Event reducer (append / update-in-place transcripts)

**Files:**
- Create: `examples/frontend/src/events.js`
- Test: `examples/frontend/tests/events.test.js`

**Interfaces:**
- Consumes: `EVENT_TYPES` from `protocol.js`.
- Produces:
  - `reduceEvent(list: Event[], ev: Event) -> Event[]` — returns a NEW array. Non-final transcript events (`user_transcript`/`agent_transcript` with `is_final:false`) replace the last row of the same `type` (and same `agent_id`, for agent) if that row is also non-final; otherwise append. Final transcripts and all other event types always append. Each stored row is the raw event plus a stable `id` (monotonic counter via a closure-free `nextId` param is NOT used — see impl: id = `${ev.ts}-${list.length}`).
  - `INITIAL_EVENTS = []`.

- [ ] **Step 1: Write the failing test**

```js
import { describe, it, expect } from "vitest";
import { reduceEvent, INITIAL_EVENTS } from "../src/events.js";

const agentPartial = (text, is_final = false) => ({
  type: "agent_transcript", agent_id: "g1", text, is_final, ts: 1,
});

describe("reduceEvent", () => {
  it("appends the first event", () => {
    const out = reduceEvent(INITIAL_EVENTS, agentPartial("he"));
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("he");
  });

  it("replaces a trailing non-final transcript of the same kind+agent", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("he"));
    s = reduceEvent(s, agentPartial("hell"));
    s = reduceEvent(s, agentPartial("hello"));
    expect(s).toHaveLength(1);
    expect(s[0].text).toBe("hello");
  });

  it("final transcript appends and locks the row", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("hello"));
    s = reduceEvent(s, agentPartial("hello", true)); // final -> append
    s = reduceEvent(s, agentPartial("next"));        // new partial -> append
    expect(s).toHaveLength(3);
    expect(s.map((e) => e.text)).toEqual(["hello", "hello", "next"]);
  });

  it("does not merge across different agents", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("a"));
    s = reduceEvent(s, { type: "agent_transcript", agent_id: "g2", text: "b", is_final: false, ts: 2 });
    expect(s).toHaveLength(2);
  });

  it("non-transcript events always append", () => {
    let s = reduceEvent(INITIAL_EVENTS, { type: "tool_call", agent_id: "g1", tool_name: "x", call_id: "1", args: {}, ts: 3 });
    s = reduceEvent(s, { type: "turn_complete", ts: 4 });
    expect(s).toHaveLength(2);
  });

  it("assigns a unique id to each row", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("hello", true));
    s = reduceEvent(s, { type: "turn_complete", ts: 4 });
    expect(s[0].id).not.toBe(s[1].id);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd examples/frontend && npx vitest run tests/events.test.js`
Expected: FAIL — cannot resolve `../src/events.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/events.js — pure reducer for the timeline event list.
import { EVENT_TYPES } from "./protocol.js";

export const INITIAL_EVENTS = [];

const TRANSCRIPT = new Set([EVENT_TYPES.USER_TRANSCRIPT, EVENT_TYPES.AGENT_TRANSCRIPT]);

let _seq = 0;
function makeId(ev) {
  _seq += 1;
  return `${ev.ts}-${_seq}`;
}

function sameStream(a, b) {
  return a.type === b.type && (a.agent_id ?? null) === (b.agent_id ?? null);
}

export function reduceEvent(list, ev) {
  const row = { ...ev, id: makeId(ev) };
  if (TRANSCRIPT.has(ev.type) && ev.is_final === false && list.length > 0) {
    const last = list[list.length - 1];
    if (sameStream(last, ev) && last.is_final === false) {
      const next = list.slice(0, -1);
      // keep the earlier row's id so React does not remount the row mid-stream.
      next.push({ ...row, id: last.id });
      return next;
    }
  }
  return [...list, row];
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd examples/frontend && npx vitest run tests/events.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/frontend/src/events.js examples/frontend/tests/events.test.js
git commit -m "feat(examples): timeline event reducer with in-place transcript merge"
```

---

### Task 4: Jitter buffer scheduling math

**Files:**
- Create: `examples/frontend/src/audio/jitter.js`
- Test: `examples/frontend/tests/jitter.test.js`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `nextStartTime(now: number, cursor: number, minLeadSec: number) -> number` — returns the start time for the next audio buffer: `max(cursor, now + minLeadSec)`. Keeps a small lead so playback never schedules in the past.
  - `advanceCursor(startTime: number, durationSec: number) -> number` — returns `startTime + durationSec` (new cursor).
  - These let `downlink.js` schedule successive `AudioBufferSourceNode`s gaplessly and recover after a flush (cursor reset to 0).

- [ ] **Step 1: Write the failing test**

```js
import { describe, it, expect } from "vitest";
import { nextStartTime, advanceCursor } from "../src/audio/jitter.js";

describe("jitter scheduling", () => {
  it("leads from now when cursor is behind", () => {
    expect(nextStartTime(10, 0, 0.05)).toBeCloseTo(10.05);
  });
  it("continues from cursor when cursor is ahead", () => {
    expect(nextStartTime(10, 10.5, 0.05)).toBeCloseTo(10.5);
  });
  it("advanceCursor adds duration", () => {
    expect(advanceCursor(10.5, 0.02)).toBeCloseTo(10.52);
  });
  it("recovers after flush (cursor 0 -> lead from now)", () => {
    const c = 0;
    expect(nextStartTime(42, c, 0.05)).toBeCloseTo(42.05);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd examples/frontend && npx vitest run tests/jitter.test.js`
Expected: FAIL — cannot resolve `../src/audio/jitter.js`.

- [ ] **Step 3: Write minimal implementation**

```js
// src/audio/jitter.js — pure scheduling math for gapless Opus playback.

export function nextStartTime(now, cursor, minLeadSec) {
  return Math.max(cursor, now + minLeadSec);
}

export function advanceCursor(startTime, durationSec) {
  return startTime + durationSec;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd examples/frontend && npx vitest run tests/jitter.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/frontend/src/audio/jitter.js examples/frontend/tests/jitter.test.js
git commit -m "feat(examples): jitter-buffer scheduling math for audio playback"
```

---

### Task 5: Config module (per-example parameters)

**Files:**
- Create: `examples/frontend/src/config.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `loadConfig() -> {wsUrl: string, agents: string[], title: string}`. Resolution order: URL query params (`ws`, `agents` comma-separated, `title`) override Vite env vars (`VITE_WS_URL`, `VITE_AGENTS`, `VITE_TITLE`) override defaults (`ws://localhost:8000/ws`, `["gemini-a","gemini-b"]`, `"Snail Playground"`). This is how one shared app serves every example.

- [ ] **Step 1: Write implementation**

```js
// src/config.js — parameterize the shared frontend per example.

const DEFAULTS = {
  wsUrl: "ws://localhost:8000/ws",
  agents: ["gemini-a", "gemini-b"],
  title: "Snail Playground",
};

export function loadConfig() {
  const q = new URLSearchParams(window.location.search);
  const env = import.meta.env ?? {};
  const agentsRaw = q.get("agents") ?? env.VITE_AGENTS;
  return {
    wsUrl: q.get("ws") ?? env.VITE_WS_URL ?? DEFAULTS.wsUrl,
    agents: agentsRaw ? agentsRaw.split(",").map((s) => s.trim()).filter(Boolean) : DEFAULTS.agents,
    title: q.get("title") ?? env.VITE_TITLE ?? DEFAULTS.title,
  };
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd examples/frontend && npm run build`
Expected: build succeeds (module imports resolve).

- [ ] **Step 3: Commit**

```bash
git add examples/frontend/src/config.js
git commit -m "feat(examples): per-example config resolution for shared frontend"
```

---

### Task 6: Audio capture worklet + uplink encoder

**Files:**
- Create: `examples/frontend/src/audio/capture-worklet.js`
- Create: `examples/frontend/src/audio/uplink.js`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `capture-worklet.js`: an `AudioWorkletProcessor` registered as `"capture"` that posts each render quantum's channel-0 `Float32Array` (copied) to the main thread.
  - `uplink.js`: `async function createUplink(onFrame: (bytes: Uint8Array) => void) -> { start(), stop(), setMuted(on: boolean) }`. `start()` opens the mic at 48 kHz mono, feeds worklet Float32 blocks into `AudioEncoder({codec:"opus", sampleRate:48000, numberOfChannels:1})`, and calls `onFrame` with each encoded chunk's bytes. `setMuted(true)` stops feeding the encoder (silence). `stop()` tears down mic + encoder + context.

- [ ] **Step 1: Write `capture-worklet.js`**

```js
// src/audio/capture-worklet.js — posts Float32 render quanta to the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (ch && ch.length) {
      this.port.postMessage(ch.slice(0)); // copy: buffer is reused by the graph
    }
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);
```

- [ ] **Step 2: Write `uplink.js`**

```js
// src/audio/uplink.js — mic -> worklet -> Opus -> onFrame(bytes).
import workletUrl from "./capture-worklet.js?url";

const SAMPLE_RATE = 48000;

export async function createUplink(onFrame) {
  let ctx = null;
  let stream = null;
  let node = null;
  let encoder = null;
  let muted = false;
  let baseTimeUs = 0;

  async function start() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: SAMPLE_RATE, echoCancellation: true },
    });
    ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await ctx.audioWorklet.addModule(workletUrl);
    const src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, "capture");

    encoder = new AudioEncoder({
      output: (chunk) => {
        const buf = new Uint8Array(chunk.byteLength);
        chunk.copyTo(buf);
        onFrame(buf);
      },
      error: (e) => console.error("[uplink] encoder error", e),
    });
    encoder.configure({ codec: "opus", sampleRate: SAMPLE_RATE, numberOfChannels: 1 });

    node.port.onmessage = (ev) => {
      if (muted || !encoder || encoder.state !== "configured") return;
      const samples = ev.data; // Float32Array, one channel
      const audioData = new AudioData({
        format: "f32",
        sampleRate: SAMPLE_RATE,
        numberOfFrames: samples.length,
        numberOfChannels: 1,
        timestamp: baseTimeUs,
        data: samples,
      });
      baseTimeUs += Math.round((samples.length / SAMPLE_RATE) * 1e6);
      encoder.encode(audioData);
      audioData.close();
    };

    src.connect(node);
    // Do not connect node to destination: we don't want to hear our own mic.
  }

  function setMuted(on) {
    muted = on;
  }

  async function stop() {
    try { node && (node.port.onmessage = null); } catch {}
    try { encoder && encoder.state !== "closed" && encoder.close(); } catch {}
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch {}
    try { ctx && (await ctx.close()); } catch {}
    ctx = stream = node = encoder = null;
    baseTimeUs = 0;
  }

  return { start, stop, setMuted };
}
```

- [ ] **Step 3: Verify it builds**

Run: `cd examples/frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add examples/frontend/src/audio/capture-worklet.js examples/frontend/src/audio/uplink.js
git commit -m "feat(examples): mic capture worklet + Opus uplink encoder"
```

---

### Task 7: Audio downlink decoder + playback

**Files:**
- Create: `examples/frontend/src/audio/downlink.js`

**Interfaces:**
- Consumes: `nextStartTime`, `advanceCursor` from `./jitter.js`.
- Produces: `function createDownlink() -> { pushFrame(bytes: Uint8Array), flush(), close() }`. `pushFrame` decodes an Opus packet via `AudioDecoder({codec:"opus"})` at 24 kHz mono, converts each `AudioData` to an `AudioBuffer`, and schedules it gaplessly on a 24 kHz playback `AudioContext` using the jitter math (min lead 0.05 s). `flush()` stops all pending sources and resets the cursor (barge-in). `close()` tears everything down.

- [ ] **Step 1: Write `downlink.js`**

```js
// src/audio/downlink.js — Opus 24k -> decode -> gapless playback.
import { nextStartTime, advanceCursor } from "./jitter.js";

const SAMPLE_RATE = 24000;
const MIN_LEAD_SEC = 0.05;

export function createDownlink() {
  const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
  let cursor = 0;
  const sources = new Set();

  const decoder = new AudioDecoder({
    output: (audioData) => {
      const frames = audioData.numberOfFrames;
      const buffer = ctx.createBuffer(1, frames, SAMPLE_RATE);
      const tmp = new Float32Array(frames);
      audioData.copyTo(tmp, { planeIndex: 0, format: "f32" });
      buffer.copyToChannel(tmp, 0);
      audioData.close();

      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      const start = nextStartTime(ctx.currentTime, cursor, MIN_LEAD_SEC);
      src.start(start);
      cursor = advanceCursor(start, buffer.duration);
      sources.add(src);
      src.onended = () => sources.delete(src);
    },
    error: (e) => console.error("[downlink] decoder error", e),
  });
  decoder.configure({ codec: "opus", sampleRate: SAMPLE_RATE, numberOfChannels: 1 });

  function pushFrame(bytes) {
    if (decoder.state !== "configured") return;
    decoder.decode(new EncodedAudioChunk({
      type: "key",
      timestamp: 0,
      data: bytes,
    }));
  }

  function flush() {
    for (const s of sources) { try { s.stop(); } catch {} }
    sources.clear();
    cursor = 0;
  }

  async function close() {
    flush();
    try { decoder.state !== "closed" && decoder.close(); } catch {}
    try { await ctx.close(); } catch {}
  }

  return { pushFrame, flush, close };
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd examples/frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add examples/frontend/src/audio/downlink.js
git commit -m "feat(examples): Opus downlink decoder + gapless playback"
```

---

### Task 8: useSession hook (WS lifecycle + wiring)

**Files:**
- Create: `examples/frontend/src/useSession.js`

**Interfaces:**
- Consumes: `control`, `isAudioMessage` from `protocol.js`; `reduceEvent`, `INITIAL_EVENTS` from `events.js`; `createUplink` from `audio/uplink.js`; `createDownlink` from `audio/downlink.js`; `EVENT_TYPES` from `protocol.js`.
- Produces: `useSession(config) -> { status, events, agents, activeAgentId, muted, start, stop, setMute, bargeIn, handoff, sendText }`.
  - `status`: `"idle" | "connecting" | "live" | "closed" | "error"`.
  - `start()`: opens WS to `config.wsUrl`, on open sends `control.start(config.agents)` and starts uplink; binary WS msgs → `downlink.pushFrame`; text msgs → parsed JSON → `reduceEvent`, with `active_agent_changed` also updating `activeAgentId` and `interrupted` calling `downlink.flush()`.
  - `bargeIn()`: `downlink.flush()` + send `control.bargeIn()`.
  - `setMute(on)`: `uplink.setMuted(on)` + send `control.mute(on)`.
  - `stop()`: send `control.stop()`, stop uplink, close downlink + WS, `status="closed"`.

- [ ] **Step 1: Write `useSession.js`**

```js
// src/useSession.js — owns the WebSocket, wires audio + event state.
import { useCallback, useRef, useState } from "react";
import { control, isAudioMessage, EVENT_TYPES } from "./protocol.js";
import { reduceEvent, INITIAL_EVENTS } from "./events.js";
import { createUplink } from "./audio/uplink.js";
import { createDownlink } from "./audio/downlink.js";

export function useSession(config) {
  const [status, setStatus] = useState("idle");
  const [events, setEvents] = useState(INITIAL_EVENTS);
  const [activeAgentId, setActiveAgentId] = useState(null);
  const [muted, setMuted] = useState(false);

  const wsRef = useRef(null);
  const uplinkRef = useRef(null);
  const downlinkRef = useRef(null);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  const handleText = useCallback((raw) => {
    let ev;
    try { ev = JSON.parse(raw); } catch { return; }
    if (ev.type === EVENT_TYPES.ACTIVE_AGENT_CHANGED) setActiveAgentId(ev.agent_id);
    if (ev.type === EVENT_TYPES.INTERRUPTED) downlinkRef.current?.flush();
    setEvents((list) => reduceEvent(list, ev));
  }, []);

  const start = useCallback(async () => {
    setStatus("connecting");
    setEvents(INITIAL_EVENTS);
    const dl = createDownlink();
    downlinkRef.current = dl;
    const ul = await createUplink((bytes) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(bytes);
    });
    uplinkRef.current = ul;

    const ws = new WebSocket(config.wsUrl);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    ws.onopen = async () => {
      send(control.start(config.agents));
      await ul.start();
      setStatus("live");
    };
    ws.onmessage = (m) => {
      if (isAudioMessage(m.data)) dl.pushFrame(new Uint8Array(m.data));
      else handleText(m.data);
    };
    ws.onerror = () => setStatus("error");
    ws.onclose = () => setStatus("closed");
  }, [config, send, handleText]);

  const stop = useCallback(async () => {
    send(control.stop());
    await uplinkRef.current?.stop();
    await downlinkRef.current?.close();
    wsRef.current?.close();
    uplinkRef.current = downlinkRef.current = wsRef.current = null;
    setStatus("closed");
  }, [send]);

  const doMute = useCallback((on) => {
    setMuted(on);
    uplinkRef.current?.setMuted(on);
    send(control.mute(on));
  }, [send]);

  const bargeIn = useCallback(() => {
    downlinkRef.current?.flush();
    send(control.bargeIn());
  }, [send]);

  const handoff = useCallback((agentId) => send(control.handoff(agentId)), [send]);
  const sendText = useCallback((text) => send(control.text(text)), [send]);

  return {
    status, events, agents: config.agents, activeAgentId, muted,
    start, stop, setMute: doMute, bargeIn, handoff, sendText,
  };
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd examples/frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add examples/frontend/src/useSession.js
git commit -m "feat(examples): useSession hook wiring WS + audio + event state"
```

---

### Task 9: UI components + App layout

**Files:**
- Create: `examples/frontend/src/ui/ControlsBar.jsx`
- Create: `examples/frontend/src/ui/Timeline.jsx`
- Create: `examples/frontend/src/ui/AgentPanel.jsx`
- Modify: `examples/frontend/src/App.jsx`

**Interfaces:**
- Consumes: `useSession` from `../useSession.js`; `loadConfig` from `../config.js`.
- Produces: bare-minimum three-region UI. `ControlsBar({status, muted, onStart, onStop, onToggleMute, onBargeIn, onSendText})`; `Timeline({events})`; `AgentPanel({agents, activeAgentId, onHandoff})`.

- [ ] **Step 1: Write `ui/ControlsBar.jsx`**

```jsx
import React, { useState } from "react";

export default function ControlsBar({ status, muted, onStart, onStop, onToggleMute, onBargeIn, onSendText }) {
  const [text, setText] = useState("");
  const live = status === "live";
  return (
    <div style={{ display: "flex", gap: 8, padding: 8, borderBottom: "1px solid #ccc", flexWrap: "wrap", alignItems: "center" }}>
      {live ? <button onClick={onStop}>Stop</button> : <button onClick={onStart}>Start</button>}
      <button disabled={!live} onClick={() => onToggleMute(!muted)}>{muted ? "Unmute" : "Mute"}</button>
      <button disabled={!live} onClick={onBargeIn}>Barge-in</button>
      <span style={{ marginLeft: "auto", opacity: 0.6 }}>{status}</span>
      <form
        style={{ display: "flex", gap: 4, flexBasis: "100%" }}
        onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onSendText(text); setText(""); } }}
      >
        <input style={{ flex: 1 }} placeholder="type a user turn" value={text} onChange={(e) => setText(e.target.value)} disabled={!live} />
        <button type="submit" disabled={!live}>Send</button>
      </form>
    </div>
  );
}
```

- [ ] **Step 2: Write `ui/Timeline.jsx`**

```jsx
import React, { useEffect, useRef } from "react";

function summarize(ev) {
  switch (ev.type) {
    case "user_transcript": return `you: ${ev.text}${ev.is_final ? "" : " …"}`;
    case "agent_transcript": return `${ev.agent_id}: ${ev.text}${ev.is_final ? "" : " …"}`;
    case "tool_call": return `${ev.agent_id} → ${ev.tool_name}(${JSON.stringify(ev.args)})`;
    case "tool_result": return `${ev.tool_name} = [${ev.status}] ${ev.content}`;
    case "active_agent_changed": return `active → ${ev.agent_id}`;
    case "go_away": return `go_away (${ev.time_left_ms}ms left)`;
    case "error": return `error ${ev.code}: ${ev.message}`;
    default: return ev.type;
  }
}

export default function Timeline({ events }) {
  const ref = useRef(null);
  useEffect(() => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight; }, [events]);
  return (
    <div ref={ref} style={{ flex: 1, overflowY: "auto", padding: 8, fontFamily: "monospace", fontSize: 13 }}>
      {events.map((ev) => (
        <div key={ev.id} style={{ padding: "2px 0" }}>
          <span style={{ opacity: 0.4, marginRight: 6 }}>{ev.type}</span>
          {summarize(ev)}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Write `ui/AgentPanel.jsx`**

```jsx
import React from "react";

export default function AgentPanel({ agents, activeAgentId, onHandoff }) {
  return (
    <div style={{ width: 200, borderLeft: "1px solid #ccc", padding: 8 }}>
      <h3 style={{ marginTop: 0 }}>Agents</h3>
      {agents.map((id) => (
        <div key={id} style={{ marginBottom: 6 }}>
          <span style={{ fontWeight: id === activeAgentId ? "bold" : "normal" }}>
            {id === activeAgentId ? "● " : "○ "}{id}
          </span>
          {id !== activeAgentId && (
            <button style={{ marginLeft: 6 }} onClick={() => onHandoff(id)}>hand off</button>
          )}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: Rewrite `src/App.jsx`**

```jsx
import React, { useMemo } from "react";
import { loadConfig } from "./config.js";
import { useSession } from "./useSession.js";
import ControlsBar from "./ui/ControlsBar.jsx";
import Timeline from "./ui/Timeline.jsx";
import AgentPanel from "./ui/AgentPanel.jsx";

export default function App() {
  const config = useMemo(() => loadConfig(), []);
  const s = useSession(config);
  const unsupported = typeof window !== "undefined" && !("AudioEncoder" in window);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <div style={{ padding: 8, borderBottom: "1px solid #ccc" }}><b>{config.title}</b></div>
      {unsupported && (
        <div style={{ background: "#fee", padding: 8 }}>
          WebCodecs unavailable — use Chrome.
        </div>
      )}
      <ControlsBar
        status={s.status} muted={s.muted}
        onStart={s.start} onStop={s.stop}
        onToggleMute={s.setMute} onBargeIn={s.bargeIn} onSendText={s.sendText}
      />
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <Timeline events={s.events} />
        <AgentPanel agents={s.agents} activeAgentId={s.activeAgentId} onHandoff={s.handoff} />
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Verify it builds**

Run: `cd examples/frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 6: Commit**

```bash
git add examples/frontend/src/ui examples/frontend/src/App.jsx
git commit -m "feat(examples): bare-minimum playground UI (controls, timeline, agents)"
```

---

### Task 10: Mock backend for standalone verification

**Files:**
- Create: `examples/frontend/mock-backend/server.py`
- Create: `examples/frontend/mock-backend/README.md`

**Interfaces:**
- Consumes: the wire contract (JSON control in, JSON events + binary Opus out).
- Produces: a runnable `python server.py` websocket server on `ws://localhost:8000/ws` that, on `start`, emits a scripted event sequence and streams a canned Opus tone; responds to `handoff`/`text`/`mute`/`barge_in`/`stop` with plausible events. Uses only the `websockets` library (already available in the repo `.venv`).

- [ ] **Step 1: Write `server.py`**

```python
"""Throwaway mock backend for the playground frontend.

Runs the wire contract without a real vendor: scripted JSON events + a canned
Opus tone as binary downlink. Not a product artifact.

    python examples/frontend/mock-backend/server.py

Note: the canned tone is a placeholder; if `av`/opus encoding is unavailable it
sends silence-length Opus frames the browser decoder tolerates. The point is to
exercise the JSON + control paths and the decode/playback plumbing.
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets


def _event(type_: str, **fields) -> str:
    return json.dumps({"type": type_, "ts": int(time.time() * 1000), **fields})


async def _script(ws) -> None:
    await ws.send(_event("active_agent_changed", agent_id="gemini-a"))
    await asyncio.sleep(0.3)
    await ws.send(_event("user_transcript", text="hello", is_final=True))
    await asyncio.sleep(0.3)
    for partial in ("hi", "hi there", "hi there, how can I help?"):
        await ws.send(_event("agent_transcript", agent_id="gemini-a", text=partial, is_final=False))
        await asyncio.sleep(0.2)
    await ws.send(_event("agent_transcript", agent_id="gemini-a", text="hi there, how can I help?", is_final=True))
    await ws.send(_event("turn_complete"))


async def handler(ws) -> None:
    script_task: asyncio.Task | None = None
    async for msg in ws:
        if isinstance(msg, bytes):
            continue  # ignore uplink audio in the mock
        try:
            ctl = json.loads(msg)
        except ValueError:
            continue
        t = ctl.get("type")
        if t == "start":
            script_task = asyncio.create_task(_script(ws))
        elif t == "handoff":
            await ws.send(_event("active_agent_changed", agent_id=ctl["agent_id"]))
        elif t == "text":
            await ws.send(_event("user_transcript", text=ctl["text"], is_final=True))
            await ws.send(_event("agent_transcript", agent_id="gemini-a", text=f"echo: {ctl['text']}", is_final=True))
            await ws.send(_event("turn_complete"))
        elif t == "barge_in":
            await ws.send(_event("interrupted"))
        elif t == "stop":
            if script_task:
                script_task.cancel()
            await ws.close()
            return


async def main() -> None:
    async with websockets.serve(handler, "localhost", 8000):
        print("mock backend on ws://localhost:8000/ws")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
```

Note: `websockets.serve(handler, ...)` serves all paths; the frontend's default `ws://localhost:8000/ws` connects fine.

- [ ] **Step 2: Write `mock-backend/README.md`**

```markdown
# Mock backend

Throwaway server that speaks the playground wire contract without a real vendor.

    python server.py   # ws://localhost:8000/ws

Emits scripted JSON events on `start`, echoes `text`, flips active agent on
`handoff`, emits `interrupted` on `barge_in`. Uplink audio is ignored. Use it to
run and eyeball the frontend before the real FastAPI backend is ready.
```

- [ ] **Step 3: Run the mock and the frontend together (manual verification)**

Run (terminal A): `cd examples/frontend/mock-backend && python server.py`
Run (terminal B): `cd examples/frontend && npm run dev`
Open Chrome at `http://localhost:5173`, click Start, grant mic.
Expected:
- Status → `live`.
- Timeline shows: `active → gemini-a`, `you: hello`, an agent transcript that updates in place then locks, `turn_complete`.
- AgentPanel shows `gemini-a` bold. Clicking "hand off" on `gemini-b` flips the bold marker.
- Typing text + Send shows `you: …` then `echo: …`.
- Barge-in produces an `interrupted` row.

- [ ] **Step 4: Commit**

```bash
git add examples/frontend/mock-backend
git commit -m "feat(examples): mock backend for standalone frontend verification"
```

---

### Task 11: multi-agent example wiring + docs

**Files:**
- Create: `examples/multi-agent/README.md`
- Create: `examples/multi-agent/config.json`
- Create: `examples/frontend/README.md`

**Interfaces:**
- Consumes: the shared frontend (Task 1-9) + its config resolution (Task 5).
- Produces: docs + a per-example config that point the shared frontend at the multi-agent backend and agent list. `config.json` documents the params; the URL query (`?ws=...&agents=gemini-a,gemini-b&title=Multi-Agent`) is how they are actually passed at runtime.

- [ ] **Step 1: Write `examples/multi-agent/config.json`**

```json
{
  "title": "Multi-Agent",
  "wsUrl": "ws://localhost:8000/ws",
  "agents": ["gemini-a", "gemini-b"]
}
```

- [ ] **Step 2: Write `examples/multi-agent/README.md`**

```markdown
# Multi-agent example

Two Gemini agents in one snail session; one is active, the other listens. The
shared playground frontend (`../frontend`) drives it.

## Run

1. Start your FastAPI backend for this example (built separately) on
   `ws://localhost:8000/ws`, loading agents `gemini-a`, `gemini-b`.
2. Start the frontend:

       cd ../frontend && npm install && npm run dev

3. Open Chrome at:

       http://localhost:5173/?title=Multi-Agent&ws=ws://localhost:8000/ws&agents=gemini-a,gemini-b

   (Params match `config.json`.)

Speak, watch the timeline, use "hand off" to force-switch the active agent.

## Standalone (no backend)

Use the mock backend to eyeball the UI: see `../frontend/mock-backend/README.md`.
```

- [ ] **Step 3: Write `examples/frontend/README.md`**

```markdown
# Snail playground frontend (shared)

A React+Vite browser playground reused across snail examples. Streams mic audio
(Opus) to a snail backend over one WebSocket, plays agent audio back, and renders
every neutral event on a live timeline.

## Run

    npm install
    npm run dev        # http://localhost:5173
    npm test           # unit tests (protocol, events, jitter)

## Parameterize per example

Pass via URL query (highest priority), Vite env, or defaults:

- `ws` / `VITE_WS_URL` — backend WebSocket URL
- `agents` / `VITE_AGENTS` — comma-separated agent ids
- `title` / `VITE_TITLE` — page title

Example: `http://localhost:5173/?ws=ws://localhost:8000/ws&agents=a,b&title=Demo`

## Wire contract

Binary WS frame = one raw Opus packet (uplink 48 kHz mono, downlink 24 kHz mono).
Text WS frame = JSON control (client→server) / event (server→client). See
`docs/superpowers/specs/2026-07-12-multi-agent-frontend-design.md`.

## Chrome only

Uses WebCodecs Opus (`AudioEncoder`/`AudioDecoder`).
```

- [ ] **Step 4: Run the full unit suite (regression)**

Run: `cd examples/frontend && npm test`
Expected: PASS — protocol, events, jitter suites all green.

- [ ] **Step 5: Commit**

```bash
git add examples/multi-agent examples/frontend/README.md
git commit -m "docs(examples): multi-agent wiring + frontend README"
```

---

## Notes for the implementer

- WebCodecs `AudioEncoder`/`AudioDecoder`, `AudioData`, `EncodedAudioChunk`,
  `AudioWorklet` exist only in a real browser — the vitest suites deliberately
  cover only pure logic (`protocol`, `events`, `jitter`). Audio + UI are verified
  manually via Task 10 / Task 11.
- Opus in WebCodecs is a raw stream: the decoder tolerates chunk type `"key"` for
  every packet; do not attempt container muxing.
- Keep the wire contract in `protocol.js` the single source of truth on the
  frontend side; the backend mirrors it.
