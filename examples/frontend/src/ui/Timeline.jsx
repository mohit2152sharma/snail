// src/ui/Timeline.jsx — the scrolling event list, with kind filters.
//
// Auto-scroll sticks to the bottom only while the user is already there; scrolling up
// to read a tool's arguments must not be yanked back by the next partial transcript.
import React, { useEffect, useLayoutEffect, useRef } from "react";
import EventRow from "./EventRow.jsx";
import { kindOf, KINDS } from "../protocol.js";

const ORDER = [KINDS.TRANSCRIPT, KINDS.TOOL, KINDS.TURN, KINDS.TIMING, KINDS.SYSTEM];

export function Filters({ events, active, onToggle }) {
  const counts = {};
  for (const ev of events) {
    const k = kindOf(ev);
    counts[k] = (counts[k] ?? 0) + 1;
  }
  return (
    <div className="filters">
      {ORDER.map((k) => (
        <button
          key={k}
          className={`chip ${active.has(k) ? "on" : ""}`}
          onClick={() => onToggle(k)}
        >
          {k}<span className="n">{counts[k] ?? 0}</span>
        </button>
      ))}
      <span className="chip" style={{ marginLeft: "auto", cursor: "default" }}>
        {events.length} events
      </span>
    </div>
  );
}

export default function Timeline({ events, t0 }) {
  const ref = useRef(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    };
    el.addEventListener("scroll", onScroll);
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  return (
    <div className="timeline" ref={ref}>
      {events.length === 0 ? (
        <div className="empty">No events yet — press Start.</div>
      ) : (
        events.map((ev) => <EventRow key={ev.id} ev={ev} t0={t0} />)
      )}
    </div>
  );
}
