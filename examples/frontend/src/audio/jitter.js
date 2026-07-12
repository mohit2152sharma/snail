// src/audio/jitter.js — pure scheduling math for gapless Opus playback.

export function nextStartTime(now, cursor, minLeadSec) {
  return Math.max(cursor, now + minLeadSec);
}

export function advanceCursor(startTime, durationSec) {
  return startTime + durationSec;
}
