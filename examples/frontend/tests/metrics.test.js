import { describe, it, expect } from "vitest";
import { INITIAL_METRICS, reduceMetrics, withReady } from "../src/metrics.js";

const fold = (evs) => evs.reduce(reduceMetrics, INITIAL_METRICS);

describe("setup stages", () => {
  it("keeps client and server stages in arrival order, tagged by side", () => {
    const m = fold([
      { type: "client_stage", stage: "mic_capture", ms: 120, ts: 1 },
      { type: "client_stage", stage: "websocket_open", ms: 8, ts: 2 },
      { type: "setup_stage", stage: "vendor_connect", agent_id: "host", ms: 400, ts: 3 },
    ]);
    expect(m.stages.map((s) => [s.label, s.side, s.ms])).toEqual([
      ["mic_capture", "client", 120],
      ["websocket_open", "client", 8],
      ["vendor_connect", "server", 400],
    ]);
    expect(m.stages[2].agentId).toBe("host");
  });

  it("records the backend total and the client-measured ready time", () => {
    let m = fold([{ type: "setup_complete", total_ms: 455.9, agents: ["host"], ts: 1 }]);
    m = withReady(m, 612.3);
    expect(m.setupTotalMs).toBe(455.9);
    expect(m.readyMs).toBe(612.3);
  });
});

describe("turns", () => {
  it("opens on end-of-speech, records ttfb, closes on turn_complete", () => {
    const m = fold([
      { type: "speech_end", ts: 1000 },
      { type: "ttfb", agent_id: "host", ms: 300, ts: 1300 },
      { type: "turn_complete", ts: 2500 },
    ]);
    expect(m.turns).toHaveLength(1);
    expect(m.turns[0]).toMatchObject({
      n: 1, agentId: "host", ttfbMs: 300, doneMs: 1500, open: false,
    });
  });

  it("attaches tool calls to the open turn and fills in their status", () => {
    const m = fold([
      { type: "speech_end", ts: 0 },
      { type: "tool_call", agent_id: "host", tool_name: "look_and_tell", call_id: "c1", args: {}, ts: 10 },
      { type: "tool_result", tool_name: "look_and_tell", call_id: "c1", status: "input_required", ts: 20 },
    ]);
    expect(m.turns[0].tools).toEqual([
      { name: "look_and_tell", callId: "c1", status: "input_required" },
    ]);
  });

  it("opens a turn implicitly when a ttfb arrives with none open", () => {
    const m = fold([{ type: "ttfb", agent_id: "host", ms: 200, ts: 5 }]);
    expect(m.turns).toHaveLength(1);
    expect(m.turns[0].ttfbMs).toBe(200);
  });

  it("does not reopen a closed turn", () => {
    const m = fold([
      { type: "speech_end", ts: 0 },
      { type: "turn_complete", ts: 100 },
      { type: "turn_complete", ts: 200 },
    ]);
    expect(m.turns).toHaveLength(1);
    expect(m.turns[0].doneMs).toBe(100);
  });

  it("numbers turns sequentially", () => {
    const m = fold([
      { type: "speech_end", ts: 0 }, { type: "turn_complete", ts: 1 },
      { type: "speech_end", ts: 2 }, { type: "turn_complete", ts: 3 },
    ]);
    expect(m.turns.map((t) => t.n)).toEqual([1, 2]);
  });
});

describe("ttfb aggregation", () => {
  it("tracks last, min, max and p50 across samples", () => {
    const m = fold([100, 500, 300].map((ms, i) => ({
      type: "ttfb", agent_id: "host", ms, ts: i,
    })));
    expect(m.ttfb).toMatchObject({ count: 3, lastMs: 300, minMs: 100, maxMs: 500, p50Ms: 300 });
  });
});

describe("counts and errors", () => {
  it("tallies every event type it sees", () => {
    const m = fold([
      { type: "turn_complete", ts: 1 },
      { type: "turn_complete", ts: 2 },
      { type: "interrupted", ts: 3 },
    ]);
    expect(m.counts).toEqual({ turn_complete: 2, interrupted: 1 });
  });

  it("collects errors", () => {
    const m = fold([{ type: "error", code: "1007", message: "precondition", ts: 1 }]);
    expect(m.errors).toEqual([{ code: "1007", message: "precondition", ts: 1 }]);
  });
});

describe("purity", () => {
  it("never mutates the state it is given", () => {
    const before = JSON.stringify(INITIAL_METRICS);
    reduceMetrics(INITIAL_METRICS, { type: "ttfb", agent_id: "a", ms: 1, ts: 1 });
    expect(JSON.stringify(INITIAL_METRICS)).toBe(before);
  });
});
