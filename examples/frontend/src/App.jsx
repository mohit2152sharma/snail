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
