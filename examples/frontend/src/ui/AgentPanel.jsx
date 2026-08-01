// src/ui/AgentPanel.jsx — who holds the token, and manual handoff.
import React from "react";

export default function AgentPanel({ agents, activeAgentId, onHandoff, disabled }) {
  return (
    <div className="panel">
      <h2>Agents</h2>
      {agents.map((id) => {
        const active = id === activeAgentId;
        return (
          <div className="agent-row" key={id}>
            <span className={`pill ${active ? "agent" : ""}`}>
              <span className="dot" />{active ? "active" : "idle"}
            </span>
            <span className={`name ${active ? "active" : ""}`}>{id}</span>
            <button disabled={disabled || active} onClick={() => onHandoff(id)}>
              hand off
            </button>
          </div>
        );
      })}
    </div>
  );
}
