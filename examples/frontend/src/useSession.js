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
