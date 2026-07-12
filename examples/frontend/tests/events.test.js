import { describe, it, expect } from "vitest";
import { reduceEvent, INITIAL_EVENTS } from "../src/events.js";

const agentPartial = (text, is_final = false) => ({
  type: "agent_transcript", agent_id: "g1", text, is_final, ts: 1,
});

describe("reduceEvent", () => {
  it("appends the first event", () => {
    const out = reduceEvent(INITIAL_EVENTS, agentPartial("he"));
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("he");
  });

  it("replaces a trailing non-final transcript of the same kind+agent", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("he"));
    s = reduceEvent(s, agentPartial("hell"));
    s = reduceEvent(s, agentPartial("hello"));
    expect(s).toHaveLength(1);
    expect(s[0].text).toBe("hello");
  });

  it("final transcript appends and locks the row", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("hello"));
    s = reduceEvent(s, agentPartial("hello", true)); // final -> append
    s = reduceEvent(s, agentPartial("next"));        // new partial -> append
    expect(s).toHaveLength(3);
    expect(s.map((e) => e.text)).toEqual(["hello", "hello", "next"]);
  });

  it("does not merge across different agents", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("a"));
    s = reduceEvent(s, { type: "agent_transcript", agent_id: "g2", text: "b", is_final: false, ts: 2 });
    expect(s).toHaveLength(2);
  });

  it("non-transcript events always append", () => {
    let s = reduceEvent(INITIAL_EVENTS, { type: "tool_call", agent_id: "g1", tool_name: "x", call_id: "1", args: {}, ts: 3 });
    s = reduceEvent(s, { type: "turn_complete", ts: 4 });
    expect(s).toHaveLength(2);
  });

  it("assigns a unique id to each row", () => {
    let s = reduceEvent(INITIAL_EVENTS, agentPartial("hello", true));
    s = reduceEvent(s, { type: "turn_complete", ts: 4 });
    expect(s[0].id).not.toBe(s[1].id);
  });
});
