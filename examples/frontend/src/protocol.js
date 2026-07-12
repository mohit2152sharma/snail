// src/protocol.js — frontend<->backend wire contract (see spec).

export const EVENT_TYPES = Object.freeze({
  USER_TRANSCRIPT: "user_transcript",
  AGENT_TRANSCRIPT: "agent_transcript",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  TURN_COMPLETE: "turn_complete",
  INTERRUPTED: "interrupted",
  GO_AWAY: "go_away",
  ACTIVE_AGENT_CHANGED: "active_agent_changed",
  ERROR: "error",
});

export const control = {
  start: (agents) => ({ type: "start", agents }),
  stop: () => ({ type: "stop" }),
  mute: (on) => ({ type: "mute", on }),
  bargeIn: () => ({ type: "barge_in" }),
  handoff: (agentId) => ({ type: "handoff", agent_id: agentId }),
  text: (text) => ({ type: "text", text }),
};

export function isAudioMessage(data) {
  return data instanceof ArrayBuffer || (typeof Blob !== "undefined" && data instanceof Blob);
}
