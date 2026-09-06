// src/format.js — display helpers shared by the panels. Pure, so they are testable.

/** Milliseconds, rounded the way a human reads latency: sub-second exact, then seconds. */
export function ms(v) {
  if (v === null || v === undefined) return "—";
  if (v < 1000) return `${Math.round(v)} ms`;
  return `${(v / 1000).toFixed(2)} s`;
}

/** Wall-clock ms → mm:ss.mmm relative to the first event of the session. */
export function since(ts, t0) {
  if (t0 === null || t0 === undefined) return "0.000";
  const d = Math.max(0, ts - t0) / 1000;
  const m = Math.floor(d / 60);
  const s = (d - m * 60).toFixed(3).padStart(6, "0");
  return m > 0 ? `${m}:${s}` : s;
}

export function pretty(obj) {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

/** Terminal tool statuses that mean "went wrong", for tag colouring. */
const BAD = new Set(["error", "invalid_args", "invalid_output", "not_found", "timeout"]);
const WAIT = new Set(["input_required", "deferred"]);

export function statusClass(status) {
  if (!status) return "";
  if (status === "success") return "ok";
  if (BAD.has(status)) return "bad";
  if (WAIT.has(status)) return "wait";
  return "";
}
