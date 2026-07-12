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
