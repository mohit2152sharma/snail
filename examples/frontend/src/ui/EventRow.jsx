// src/ui/EventRow.jsx — one timeline row.
//
// Every row has the same skeleton (elapsed · type · body) so the eye can scan a column
// instead of parsing prose. The body is a one-line summary; anything structured —
// tool arguments above all — hangs off a collapsed <details> with the raw JSON, so
// nothing the backend sent is hidden, only folded.
import React from "react";
import { kindOf } from "../protocol.js";
import { ms, since, pretty, statusClass } from "../format.js";

const HIDE_IN_JSON = new Set(["id", "type", "ts"]);

function extras(ev) {
  const out = {};
  for (const [k, v] of Object.entries(ev)) if (!HIDE_IN_JSON.has(k)) out[k] = v;
  return out;
}

function Body({ ev }) {
  switch (ev.type) {
    case "user_transcript":
      return (
        <span className={ev.is_final ? "" : "partial"}>
          <span className="who user">you</span>{ev.text}{ev.is_final ? "" : " …"}
        </span>
      );

    case "agent_transcript":
      return (
        <span className={ev.is_final ? "" : "partial"}>
          <span className="who">{ev.agent_id}</span>{ev.text}{ev.is_final ? "" : " …"}
        </span>
      );

    case "tool_call":
      return (
        <>
          <span className="who">{ev.agent_id}</span>
          <b>{ev.tool_name}</b>
          <span className="muted">({Object.keys(ev.args ?? {}).join(", ")})</span>
          <span className="tag">{ev.call_id}</span>
          <details className="json">
            <summary>arguments</summary>
            <pre>{pretty(ev.args ?? {})}</pre>
          </details>
        </>
      );

    case "tool_result":
      return (
        <>
          <b>{ev.tool_name}</b>
          <span className={`tag ${statusClass(ev.status)}`}>{ev.status}</span>
          {ev.content ? <span className="muted"> {ev.content}</span> : null}
        </>
      );

    case "tool_run":
      // The run-level machine (docs 14): started → blocked → submit → finished.
      return (
        <>
          <span className="muted">{ev.run_id ?? "—"}</span>{" "}
          <b>{ev.phase}</b>{ev.tool_name ? <span className="muted"> {ev.tool_name}</span> : null}
          {ev.key ? <span className="tag">{ev.key}:{ev.expects}</span> : null}
          {ev.outcome ? (
            <span className={`tag ${ev.outcome === "accepted" ? "ok" : "bad"}`}>{ev.outcome}</span>
          ) : null}
          {ev.status ? <span className="tag">{ev.status}</span> : null}
        </>
      );

    case "ttfb":
      return (
        <>
          <span className="who">{ev.agent_id}</span>
          end-of-speech → first byte <span className="num">{ms(ev.ms)}</span>
        </>
      );

    case "setup_stage":
      return (
        <>
          <b>{ev.stage}</b>{ev.agent_id ? <span className="who"> {ev.agent_id}</span> : null}{" "}
          <span className="num">{ms(ev.ms)}</span>
          {ev.detail ? <span className="muted"> {ev.detail}</span> : null}
        </>
      );

    case "client_stage":
      return (
        <>
          <b>{ev.stage}</b> <span className="num">{ms(ev.ms)}</span>
          <span className="tag">client</span>
          {ev.detail ? <span className="muted"> {ev.detail}</span> : null}
        </>
      );

    case "setup_complete":
      return <>session ready <span className="num">{ms(ev.total_ms)}</span>{" "}
        <span className="muted">({(ev.agents ?? []).join(", ")})</span></>;

    case "active_agent_changed":
      return <>active → <span className="who">{ev.agent_id}</span></>;

    case "turn_complete":
      return <span className="muted">turn complete{ev.agent_id ? ` (${ev.agent_id})` : ""}</span>;

    case "speech_start":
      return <span className="muted">user speech start</span>;

    case "speech_end":
      return <span className="muted">user speech end — TTFB clock armed</span>;

    case "interrupted":
      return <span className="muted">barge-in — output flushed</span>;

    case "go_away":
      return <>go_away <span className="num">{ms(ev.time_left_ms)}</span> left</>;

    case "error":
      return <span style={{ color: "var(--bad)" }}>{ev.code}: {ev.message}</span>;

    default:
      return <span className="muted">{ev.type}</span>;
  }
}

// Rows whose whole content is already on screen do not need a JSON fold.
const NO_JSON = new Set([
  "user_transcript", "agent_transcript", "turn_complete", "interrupted",
  "speech_start", "speech_end", "active_agent_changed", "tool_call",
]);

export default function EventRow({ ev, t0 }) {
  return (
    <div className={`row ${kindOf(ev)}`}>
      <div className="t">{since(ev.ts, t0)}</div>
      <div className="type" title={ev.type}>{ev.type}</div>
      <div className="body">
        <Body ev={ev} />
        {NO_JSON.has(ev.type) ? null : (
          <details className="json">
            <summary>raw</summary>
            <pre>{pretty(extras(ev))}</pre>
          </details>
        )}
      </div>
    </div>
  );
}
