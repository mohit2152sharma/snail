// src/ui/SetupPanel.jsx — how long the session took to come up, stage by stage.
//
// Both sides are shown because both cost the user real seconds and they fail
// differently: a slow `websocket_open` is the network to your own backend, a slow
// `vendor_connect` is the model provider, and a slow `mic_capture` is the browser
// waiting on a permission prompt. Bars are scaled to the largest stage, so the
// dominant term is obvious without reading the numbers.
import React from "react";
import { ms } from "../format.js";

export default function SetupPanel({ metrics }) {
  const { stages, setupTotalMs, readyMs } = metrics;
  const max = stages.reduce((m, s) => Math.max(m, s.ms ?? 0), 0) || 1;

  return (
    <div className="panel">
      <h2>Connection setup</h2>
      <div className="kv"><span className="k">start → live</span><span className="v">{ms(readyMs)}</span></div>
      <div className="kv"><span className="k">backend setup</span><span className="v">{ms(setupTotalMs)}</span></div>

      {stages.length === 0 ? (
        <div className="empty" style={{ padding: "10px 0" }}>No stages yet.</div>
      ) : (
        <div className="bars">
          {stages.map((s) => (
            <div className="bar-row" key={s.key + s.ms}>
              <div>
                <div className="bar-label" title={s.detail}>
                  {s.label}{s.agentId ? ` · ${s.agentId}` : ""}
                </div>
                <div className="bar-track">
                  <div
                    className={`bar-fill ${s.side}`}
                    style={{ width: `${Math.max(2, ((s.ms ?? 0) / max) * 100)}%` }}
                  />
                </div>
              </div>
              <div className="bar-ms">{ms(s.ms)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
