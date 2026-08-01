// src/ui/PendingPanel.jsx — the question a tool is currently waiting on, and its budget.
//
// The one piece of state that is otherwise invisible: between `input_required` and the
// user's answer the run is parked server-side, holding an agent's slot, with a clock
// running. This shows what it asked, what shape of answer it wants, and how long is
// left — so the expiry path can be *watched* rather than inferred from a log line.
//
// The countdown ticks locally off `budget_s`; the server's sweep is authoritative, so
// hitting zero here and the `expired` row arriving are two separate events, and a few
// hundred ms of drift between them is expected.
import React, { useEffect, useState } from "react";

const OUTCOME_CLASS = {
  answered: "ok",
  expired: "bad",
  displaced: "wait",
  finished: "ok",
};

export default function PendingPanel({ metrics }) {
  const { pending, lastWait } = metrics;
  const [, tick] = useState(0);

  // Re-render 4×/s only while something is actually waiting.
  useEffect(() => {
    if (!pending) return undefined;
    const id = setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(id);
  }, [pending]);

  if (!pending) {
    return (
      <div className="panel">
        <h2>Waiting on input</h2>
        {lastWait ? (
          <>
            <div className="kv">
              <span className="k">{lastWait.tool} · {lastWait.key}</span>
              <span className={`tag ${OUTCOME_CLASS[lastWait.outcome] ?? ""}`}>
                {lastWait.outcome}
              </span>
            </div>
            <div className="kv">
              <span className="k">waited</span>
              <span className="v">{lastWait.waitedS}s</span>
            </div>
          </>
        ) : (
          <div className="empty" style={{ padding: "10px 0" }}>Nothing pending.</div>
        )}
      </div>
    );
  }

  const budget = pending.budgetS;
  const elapsed = (Date.now() - pending.blockedTs) / 1000;
  const left = budget === null ? null : Math.max(0, budget - elapsed);
  const frac = budget === null ? 0 : Math.min(1, elapsed / budget);
  const dead = left !== null && left <= 0;

  return (
    <div className="panel pending">
      <h2>Waiting on input</h2>
      <div className="kv">
        <span className="k">{pending.tool}</span>
        <span className="tag wait">{pending.runId}</span>
      </div>
      {pending.ask ? <div className="ask">“{pending.ask}”</div> : null}
      <div className="kv">
        <span className="k">answer</span>
        <span className="v">{pending.key} : {pending.expects}</span>
      </div>
      <div className="kv">
        <span className="k">budget</span>
        <span className="v">
          {budget === null ? "—" : (dead ? "expiring…" : `${left.toFixed(1)}s left of ${budget}s`)}
        </span>
      </div>
      <div className="bar-track" style={{ height: 6, marginTop: 6 }}>
        <div
          className={`bar-fill ${dead ? "over" : "budget"}`}
          style={{ width: `${frac * 100}%` }}
        />
      </div>
    </div>
  );
}
