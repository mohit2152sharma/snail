import React, { useEffect, useRef } from "react";

function summarize(ev) {
  switch (ev.type) {
    case "user_transcript": return `you: ${ev.text}${ev.is_final ? "" : " …"}`;
    case "agent_transcript": return `${ev.agent_id}: ${ev.text}${ev.is_final ? "" : " …"}`;
    case "tool_call": return `${ev.agent_id} → ${ev.tool_name}(${JSON.stringify(ev.args)})`;
    case "tool_result": return `${ev.tool_name} = [${ev.status}] ${ev.content}`;
    case "active_agent_changed": return `active → ${ev.agent_id}`;
    case "go_away": return `go_away (${ev.time_left_ms}ms left)`;
    case "error": return `error ${ev.code}: ${ev.message}`;
    default: return ev.type;
  }
}

export default function Timeline({ events }) {
  const ref = useRef(null);
  useEffect(() => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight; }, [events]);
  return (
    <div ref={ref} style={{ flex: 1, overflowY: "auto", padding: 8, fontFamily: "monospace", fontSize: 13 }}>
      {events.map((ev) => (
        <div key={ev.id} style={{ padding: "2px 0" }}>
          <span style={{ opacity: 0.4, marginRight: 6 }}>{ev.type}</span>
          {summarize(ev)}
        </div>
      ))}
    </div>
  );
}
