// src/ui/TurnsPanel.jsx — per-turn TTFB, newest first.
//
// TTFB here is the server's number: end-of-speech → first audio byte out. It is
// measured at the bridge (which is what decides end-of-speech, via its VAD), so it does
// not include the client's playout buffer — that keeps it comparable across runs and
// independent of the listener's machine.
import React from "react";
import { ms } from "../format.js";

export default function TurnsPanel({ metrics }) {
  const { turns, ttfb } = metrics;
  const rows = [...turns].reverse().slice(0, 40);

  return (
    <div className="panel">
      <h2>Turns · TTFB</h2>
      <div className="kv"><span className="k">last</span><span className="v">{ms(ttfb.lastMs)}</span></div>
      <div className="kv"><span className="k">p50 / min / max</span>
        <span className="v">{ms(ttfb.p50Ms)} · {ms(ttfb.minMs)} · {ms(ttfb.maxMs)}</span>
      </div>
      <div className="kv"><span className="k">samples</span><span className="v">{ttfb.count}</span></div>

      {rows.length === 0 ? (
        <div className="empty" style={{ padding: "10px 0" }}>No turns yet.</div>
      ) : (
        <table className="turns">
          <thead>
            <tr><th>#</th><th>agent</th><th>ttfb</th><th>turn</th><th>tools</th></tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.n} className={t.open ? "open" : ""}>
                <td>{t.n}</td>
                <td>{t.agentId ?? "—"}</td>
                <td>{t.ttfbMs === null ? "—" : ms(t.ttfbMs)}</td>
                <td>{t.doneMs === null ? "…" : ms(t.doneMs)}</td>
                <td className="tools">{t.tools.map((x) => x.name).join(", ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
