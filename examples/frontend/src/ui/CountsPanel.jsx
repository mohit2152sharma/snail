// src/ui/CountsPanel.jsx — raw event tallies + anything that errored.
//
// The tally is the cheapest way to notice a stream that has gone wrong: no
// `agent_transcript` means the model is not speaking, a climbing `interrupted` means
// the VAD is tripping on the agent's own echo.
import React from "react";

export default function CountsPanel({ metrics }) {
  const entries = Object.entries(metrics.counts).sort((a, b) => b[1] - a[1]);
  return (
    <div className="panel">
      <h2>Event counts</h2>
      {entries.length === 0 ? (
        <div className="empty" style={{ padding: "10px 0" }}>Nothing yet.</div>
      ) : (
        entries.map(([type, n]) => (
          <div className="kv" key={type}>
            <span className="k">{type}</span><span className="v">{n}</span>
          </div>
        ))
      )}
      {metrics.errors.length > 0 && (
        <>
          <h2 style={{ marginTop: 12 }}>Errors</h2>
          {metrics.errors.map((e, i) => (
            <div className="err" key={i}>{e.code}: {e.message}</div>
          ))}
        </>
      )}
    </div>
  );
}
