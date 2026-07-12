import { describe, it, expect } from "vitest";
import { control, EVENT_TYPES, isAudioMessage } from "../src/protocol.js";

describe("control builders", () => {
  it("start carries agents", () => {
    expect(control.start(["a", "b"])).toEqual({ type: "start", agents: ["a", "b"] });
  });
  it("mute carries on flag", () => {
    expect(control.mute(true)).toEqual({ type: "mute", on: true });
  });
  it("handoff maps to agent_id", () => {
    expect(control.handoff("g2")).toEqual({ type: "handoff", agent_id: "g2" });
  });
  it("text carries text", () => {
    expect(control.text("hi")).toEqual({ type: "text", text: "hi" });
  });
  it("stop and barge_in are bare", () => {
    expect(control.stop()).toEqual({ type: "stop" });
    expect(control.bargeIn()).toEqual({ type: "barge_in" });
  });
});

describe("EVENT_TYPES", () => {
  it("includes the neutral events", () => {
    expect(EVENT_TYPES.AGENT_TRANSCRIPT).toBe("agent_transcript");
    expect(EVENT_TYPES.ACTIVE_AGENT_CHANGED).toBe("active_agent_changed");
  });
});

describe("isAudioMessage", () => {
  it("true for ArrayBuffer, false for string", () => {
    expect(isAudioMessage(new ArrayBuffer(4))).toBe(true);
    expect(isAudioMessage("{}\n")).toBe(false);
  });
});
