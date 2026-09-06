// src/useSession.js — owns the WebSocket, wires audio + event/metric state.
//
// Setup is measured here as well as on the server: the socket handshake and the mic
// permission/worklet spin-up are client-side facts the backend cannot see. Each is
// synthesized as a `client_stage` event so it lands on the same timeline, in order,
// next to the server's `setup_stage` rows.
import { useCallback, useRef, useState } from "react";
import { control, isAudioMessage, EVENT_TYPES, CLIENT_EVENTS } from "./protocol.js";
import { reduceEvent, INITIAL_EVENTS } from "./events.js";
import { INITIAL_METRICS, reduceMetrics, withReady } from "./metrics.js";
import { createUplink } from "./audio/uplink.js";
import { createDownlink } from "./audio/downlink.js";

const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

export function useSession(config) {
  const [status, setStatus] = useState("idle");
  const [events, setEvents] = useState(INITIAL_EVENTS);
  const [metrics, setMetrics] = useState(INITIAL_METRICS);
  const [activeAgentId, setActiveAgentId] = useState(null);
  const [muted, setMuted] = useState(false);

  const wsRef = useRef(null);
  const uplinkRef = useRef(null);
  const downlinkRef = useRef(null);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  // One door for every event, wire or synthesized: timeline + metrics stay in step.
  const ingest = useCallback((ev) => {
    setEvents((list) => reduceEvent(list, ev));
    setMetrics((m) => reduceMetrics(m, ev));
  }, []);

  const stage = useCallback((name, ms, detail = "") => {
    ingest({
      type: CLIENT_EVENTS.CLIENT_STAGE, stage: name,
      ms: Math.round(ms * 10) / 10, detail, ts: Date.now(),
    });
  }, [ingest]);

  const handleText = useCallback((raw) => {
    let ev;
    try { ev = JSON.parse(raw); } catch { return; }
    if (ev.type === EVENT_TYPES.ACTIVE_AGENT_CHANGED) setActiveAgentId(ev.agent_id);
    if (ev.type === EVENT_TYPES.INTERRUPTED) downlinkRef.current?.flush();
    ingest(ev);
  }, [ingest]);

  const start = useCallback(async () => {
    setStatus("connecting");
    setEvents(INITIAL_EVENTS);
    setMetrics(INITIAL_METRICS);
    const t0 = now();

    const dl = createDownlink();
    downlinkRef.current = dl;

    const ul = await createUplink((bytes) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(bytes);
    });
    uplinkRef.current = ul;

    const tWs = now();
    const ws = new WebSocket(config.wsUrl);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    ws.onopen = async () => {
      stage("websocket_open", now() - tWs, config.wsUrl);
      send(control.start(config.agents));
      // Mic permission + AudioWorklet + encoder: usually the largest client-side term
      // on a cold load, and invisible from the server. A denied or absent mic is not
      // fatal — the session still connects, and typed turns still drive the flow.
      const tMic = now();
      try {
        await ul.start();
        stage("mic_capture", now() - tMic);
      } catch (err) {
        stage("mic_capture", now() - tMic, "unavailable");
        ingest({
          type: EVENT_TYPES.ERROR, code: "mic_unavailable",
          message: String(err?.message ?? err), ts: Date.now(),
        });
      }
      setStatus("live");
      setMetrics((m) => withReady(m, Math.round((now() - t0) * 10) / 10));
    };
    ws.onmessage = (m) => {
      if (isAudioMessage(m.data)) dl.pushFrame(new Uint8Array(m.data));
      else handleText(m.data);
    };
    ws.onerror = () => setStatus("error");
    ws.onclose = () => setStatus("closed");
  }, [config, send, handleText, stage, ingest]);

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
    status, events, metrics, agents: config.agents, activeAgentId, muted,
    start, stop, setMute: doMute, bargeIn, handoff, sendText,
  };
}
