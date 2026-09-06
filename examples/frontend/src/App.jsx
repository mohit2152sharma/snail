// src/App.jsx — layout only. All state lives in useSession; all numbers in metrics.js.
import React, { useMemo, useState } from "react";
import { loadConfig } from "./config.js";
import { useSession } from "./useSession.js";
import { kindOf, KINDS } from "./protocol.js";
import ControlsBar from "./ui/ControlsBar.jsx";
import Timeline, { Filters } from "./ui/Timeline.jsx";
import AgentPanel from "./ui/AgentPanel.jsx";
import PendingPanel from "./ui/PendingPanel.jsx";
import SetupPanel from "./ui/SetupPanel.jsx";
import TurnsPanel from "./ui/TurnsPanel.jsx";
import CountsPanel from "./ui/CountsPanel.jsx";
import "./styles.css";

const ALL_KINDS = new Set(Object.values(KINDS));

export default function App() {
  const config = useMemo(() => loadConfig(), []);
  const s = useSession(config);
  const [kinds, setKinds] = useState(ALL_KINDS);
  const unsupported = typeof window !== "undefined" && !("AudioEncoder" in window);

  const toggle = (k) => setKinds((prev) => {
    const next = new Set(prev);
    if (next.has(k)) next.delete(k); else next.add(k);
    return next;
  });

  const shown = s.events.filter((ev) => kinds.has(kindOf(ev)));
  const t0 = s.events.length > 0 ? s.events[0].ts : null;

  return (
    <div className="app">
      <div className="header">
        <h1>{config.title}</h1>
        <span className={`pill ${s.status}`}><span className="dot" />{s.status}</span>
        {s.activeAgentId && (
          <span className="pill agent"><span className="dot" />{s.activeAgentId}</span>
        )}
        <span className="spacer" />
        <span className="pill">{config.wsUrl}</span>
      </div>

      {unsupported && (
        <div className="banner">
          WebCodecs unavailable — audio needs Chrome. Typed turns still work.
        </div>
      )}

      <ControlsBar
        status={s.status} muted={s.muted}
        onStart={s.start} onStop={s.stop}
        onToggleMute={s.setMute} onBargeIn={s.bargeIn} onSendText={s.sendText}
      />

      <div className="main">
        <div className="timeline-col">
          <Filters events={s.events} active={kinds} onToggle={toggle} />
          <Timeline events={shown} t0={t0} />
        </div>
        <div className="rail">
          <PendingPanel metrics={s.metrics} />
          <SetupPanel metrics={s.metrics} />
          <TurnsPanel metrics={s.metrics} />
          <AgentPanel
            agents={s.agents} activeAgentId={s.activeAgentId}
            onHandoff={s.handoff} disabled={s.status !== "live"}
          />
          <CountsPanel metrics={s.metrics} />
        </div>
      </div>
    </div>
  );
}
