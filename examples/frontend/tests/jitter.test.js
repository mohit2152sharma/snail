import { describe, it, expect } from "vitest";
import { nextStartTime, advanceCursor } from "../src/audio/jitter.js";

describe("jitter scheduling", () => {
  it("leads from now when cursor is behind", () => {
    expect(nextStartTime(10, 0, 0.05)).toBeCloseTo(10.05);
  });
  it("continues from cursor when cursor is ahead", () => {
    expect(nextStartTime(10, 10.5, 0.05)).toBeCloseTo(10.5);
  });
  it("advanceCursor adds duration", () => {
    expect(advanceCursor(10.5, 0.02)).toBeCloseTo(10.52);
  });
  it("recovers after flush (cursor 0 -> lead from now)", () => {
    const c = 0;
    expect(nextStartTime(42, c, 0.05)).toBeCloseTo(42.05);
  });
});
