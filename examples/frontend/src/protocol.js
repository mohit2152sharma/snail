// src/protocol.js — frontend<->backend wire contract (see spec).

export const EVENT_TYPES = Object.freeze({
  USER_TRANSCRIPT: "user_transcript",
  AGENT_TRANSCRIPT: "agent_transcript",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  TOOL_RUN: "tool_run",
  TURN_COMPLETE: "turn_complete",
  INTERRUPTED: "interrupted",
  GO_AWAY: "go_away",
  ACTIVE_AGENT_CHANGED: "active_agent_changed",
  SETUP_STAGE: "setup_stage",
  SETUP_COMPLETE: "setup_complete",
  SPEECH_START: "speech_start",
  SPEECH_END: "speech_end",
  TTFB: "ttfb",
  ERROR: "error",
});

// Events the frontend synthesizes for its own timings (mic, socket). Same shape as the
// wire events, so one timeline and one metrics reducer handle both halves of setup.
export const CLIENT_EVENTS = Object.freeze({
  CLIENT_STAGE: "client_stage",
});

export const control = {
  start: (agents) => ({ type: "start", agents }),
  stop: () => ({ type: "stop" }),
  mute: (on) => ({ type: "mute", on }),
  bargeIn: () => ({ type: "barge_in" }),
  handoff: (agentId) => ({ type: "handoff", agent_id: agentId }),
  text: (text) => ({ type: "text", text }),
};

// Timeline groups. The filter chips map 1:1 onto these, so classifying a new event
// type here is all it takes to make it filterable.
export const KINDS = Object.freeze({
  TRANSCRIPT: "transcript",
  TOOL: "tool",
  TURN: "turn",
  TIMING: "timing",
  SYSTEM: "system",
});

const KIND_OF = {
  [EVENT_TYPES.USER_TRANSCRIPT]: KINDS.TRANSCRIPT,
  [EVENT_TYPES.AGENT_TRANSCRIPT]: KINDS.TRANSCRIPT,
  [EVENT_TYPES.TOOL_CALL]: KINDS.TOOL,
  [EVENT_TYPES.TOOL_RESULT]: KINDS.TOOL,
  [EVENT_TYPES.TOOL_RUN]: KINDS.TOOL,
  [EVENT_TYPES.TURN_COMPLETE]: KINDS.TURN,
  [EVENT_TYPES.INTERRUPTED]: KINDS.TURN,
  [EVENT_TYPES.SPEECH_START]: KINDS.TURN,
  [EVENT_TYPES.SPEECH_END]: KINDS.TURN,
  [EVENT_TYPES.TTFB]: KINDS.TIMING,
  [EVENT_TYPES.SETUP_STAGE]: KINDS.TIMING,
  [EVENT_TYPES.SETUP_COMPLETE]: KINDS.TIMING,
  [CLIENT_EVENTS.CLIENT_STAGE]: KINDS.TIMING,
};

export function kindOf(ev) {
  return KIND_OF[ev.type] ?? KINDS.SYSTEM;
}

export function isAudioMessage(data) {
  return data instanceof ArrayBuffer || (typeof Blob !== "undefined" && data instanceof Blob);
}
