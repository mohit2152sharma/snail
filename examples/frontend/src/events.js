// src/events.js — pure reducer for the timeline event list.
import { EVENT_TYPES } from "./protocol.js";

export const INITIAL_EVENTS = [];

const TRANSCRIPT = new Set([EVENT_TYPES.USER_TRANSCRIPT, EVENT_TYPES.AGENT_TRANSCRIPT]);

let _seq = 0;
function makeId(ev) {
  _seq += 1;
  return `${ev.ts}-${_seq}`;
}

function sameStream(a, b) {
  return a.type === b.type && (a.agent_id ?? null) === (b.agent_id ?? null);
}

export function reduceEvent(list, ev) {
  const row = { ...ev, id: makeId(ev) };
  if (TRANSCRIPT.has(ev.type) && ev.is_final === false && list.length > 0) {
    const last = list[list.length - 1];
    if (sameStream(last, ev) && last.is_final === false) {
      const next = list.slice(0, -1);
      // keep the earlier row's id so React does not remount the row mid-stream.
      next.push({ ...row, id: last.id });
      return next;
    }
  }
  return [...list, row];
}
