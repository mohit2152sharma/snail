// src/metrics.js — pure reducer: event stream → the numbers the panels render.
//
// Kept out of React entirely so it can be unit-tested against a scripted stream. The
// same reducer folds both halves of setup: client-measured stages (mic, socket) arrive
// as synthesized `client_stage` events, backend-measured ones as `setup_stage`.
import { EVENT_TYPES, CLIENT_EVENTS } from "./protocol.js";

export const INITIAL_METRICS = Object.freeze({
  stages: [],          // [{ key, label, ms, side: "client"|"server", agentId, detail }]
  setupTotalMs: null,  // server-reported: first acquire → all agents ready
  readyMs: null,       // client-measured: Start pressed → session live
  turns: [],           // [{ n, agentId, endOfSpeechTs, ttfbMs, tools, doneMs, open }]
  ttfb: { count: 0, lastMs: null, minMs: null, maxMs: null, p50Ms: null, samples: [] },
  counts: {},          // event type → how many
  errors: [],          // [{ code, message, ts }]
  // The run currently waiting on the user, if any: what it asked, and when its budget
  // runs out. One per agent by construction, and this playground drives one agent.
  pending: null,       // { runId, agentId, tool, key, expects, ask, budgetS, blockedTs }
  // How the last wait ended, kept after `pending` clears so the outcome stays readable.
  lastWait: null,      // { runId, tool, key, outcome, waitedS, ts }
});

function withCount(counts, type) {
  return { ...counts, [type]: (counts[type] ?? 0) + 1 };
}

function percentile(sorted, p) {
  if (sorted.length === 0) return null;
  const i = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[i];
}

function addSample(ttfb, ms) {
  const samples = [...ttfb.samples, ms];
  const sorted = [...samples].sort((a, b) => a - b);
  return {
    count: samples.length,
    lastMs: ms,
    minMs: sorted[0],
    maxMs: sorted[sorted.length - 1],
    p50Ms: percentile(sorted, 50),
    samples,
  };
}

// A turn is opened by end-of-speech (the instant the TTFB clock starts) and closed by
// turn_complete. Everything in between — the model's tool calls, its first audio byte —
// belongs to it. Events that arrive with no turn open (a text-typed turn, a tool call
// the model made unprompted) open one implicitly, so nothing is silently dropped.
function openTurn(turns, ts, agentId) {
  const last = turns[turns.length - 1];
  if (last && last.open) return turns;
  return [...turns, {
    n: turns.length + 1, agentId: agentId ?? null, endOfSpeechTs: ts,
    ttfbMs: null, tools: [], doneMs: null, open: true,
  }];
}

function patchOpen(turns, patch) {
  if (turns.length === 0) return turns;
  const i = turns.length - 1;
  if (!turns[i].open) return turns;
  return [...turns.slice(0, i), { ...turns[i], ...patch }];
}

export function reduceMetrics(state, ev) {
  const s = { ...state, counts: withCount(state.counts, ev.type) };

  switch (ev.type) {
    case CLIENT_EVENTS.CLIENT_STAGE:
      return { ...s, stages: [...s.stages, {
        key: `client:${ev.stage}`, label: ev.stage, ms: ev.ms,
        side: "client", agentId: null, detail: ev.detail ?? "",
      }] };

    case EVENT_TYPES.SETUP_STAGE:
      return { ...s, stages: [...s.stages, {
        key: `server:${ev.stage}:${ev.agent_id ?? ""}`, label: ev.stage, ms: ev.ms,
        side: "server", agentId: ev.agent_id ?? null, detail: ev.detail ?? "",
      }] };

    case EVENT_TYPES.SETUP_COMPLETE:
      return { ...s, setupTotalMs: ev.total_ms };

    case EVENT_TYPES.SPEECH_END:
      return { ...s, turns: openTurn(s.turns, ev.ts, null) };

    case EVENT_TYPES.TTFB: {
      const turns = openTurn(s.turns, ev.ts, ev.agent_id);
      return {
        ...s,
        ttfb: addSample(s.ttfb, ev.ms),
        turns: patchOpen(turns, { ttfbMs: ev.ms, agentId: ev.agent_id }),
      };
    }

    case EVENT_TYPES.TOOL_CALL: {
      const turns = openTurn(s.turns, ev.ts, ev.agent_id);
      const cur = turns[turns.length - 1];
      return { ...s, turns: patchOpen(turns, {
        tools: [...cur.tools, { name: ev.tool_name, callId: ev.call_id, status: null }],
      }) };
    }

    case EVENT_TYPES.TOOL_RESULT: {
      const cur = s.turns[s.turns.length - 1];
      if (!cur || !cur.open) return s;
      return { ...s, turns: patchOpen(s.turns, {
        tools: cur.tools.map((t) =>
          t.callId === ev.call_id ? { ...t, status: ev.status } : t),
      }) };
    }

    case EVENT_TYPES.TURN_COMPLETE: {
      const cur = s.turns[s.turns.length - 1];
      if (!cur || !cur.open) return s;
      return { ...s, turns: patchOpen(s.turns, {
        open: false, doneMs: ev.ts - cur.endOfSpeechTs,
      }) };
    }

    // The run-level machine (docs 14). `blocked` opens a wait; anything terminal
    // closes it. `submit` only closes it when it was accepted — a rejected answer
    // leaves the run blocked and still answerable, so the countdown keeps running.
    case EVENT_TYPES.TOOL_RUN: {
      if (ev.phase === "blocked") {
        return { ...s, pending: {
          runId: ev.run_id, agentId: ev.agent_id ?? null, tool: ev.tool_name,
          key: ev.key, expects: ev.expects, ask: ev.ask ?? "",
          budgetS: ev.budget_s ?? null, blockedTs: ev.ts,
        } };
      }
      const closes =
        ev.phase === "expired" || ev.phase === "displaced" || ev.phase === "finished" ||
        (ev.phase === "submit" && ev.outcome === "accepted");
      if (!closes) return s;
      const open = s.pending;
      if (open && open.runId !== ev.run_id) return s;  // not this wait
      return {
        ...s,
        pending: null,
        lastWait: open ? {
          runId: ev.run_id, tool: ev.tool_name ?? open.tool, key: open.key,
          outcome: ev.phase === "submit" ? "answered" : ev.phase,
          waitedS: ev.waited_s ?? Math.round((ev.ts - open.blockedTs) / 100) / 10,
          ts: ev.ts,
        } : s.lastWait,
      };
    }

    case EVENT_TYPES.ERROR:
      return { ...s, errors: [...s.errors, { code: ev.code, message: ev.message, ts: ev.ts }] };

    default:
      return s;
  }
}

/** Client-measured "Start pressed → live", set once the session reports live. */
export function withReady(state, ms) {
  return { ...state, readyMs: ms };
}
