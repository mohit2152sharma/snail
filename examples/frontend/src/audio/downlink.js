// src/audio/downlink.js — Opus 24k -> decode -> gapless playback.
import { nextStartTime, advanceCursor } from "./jitter.js";

const SAMPLE_RATE = 24000;
const MIN_LEAD_SEC = 0.05;

export function createDownlink() {
  const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
  let cursor = 0;
  const sources = new Set();

  const decoder = new AudioDecoder({
    output: (audioData) => {
      const frames = audioData.numberOfFrames;
      const buffer = ctx.createBuffer(1, frames, SAMPLE_RATE);
      const tmp = new Float32Array(frames);
      audioData.copyTo(tmp, { planeIndex: 0, format: "f32" });
      buffer.copyToChannel(tmp, 0);
      audioData.close();

      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      const start = nextStartTime(ctx.currentTime, cursor, MIN_LEAD_SEC);
      src.start(start);
      cursor = advanceCursor(start, buffer.duration);
      sources.add(src);
      src.onended = () => sources.delete(src);
    },
    error: (e) => console.error("[downlink] decoder error", e),
  });
  decoder.configure({ codec: "opus", sampleRate: SAMPLE_RATE, numberOfChannels: 1 });

  function pushFrame(bytes) {
    if (decoder.state !== "configured") return;
    decoder.decode(new EncodedAudioChunk({
      type: "key",
      timestamp: 0,
      data: bytes,
    }));
  }

  function flush() {
    for (const s of sources) { try { s.stop(); } catch {} }
    sources.clear();
    cursor = 0;
  }

  async function close() {
    flush();
    try { decoder.state !== "closed" && decoder.close(); } catch {}
    try { await ctx.close(); } catch {}
  }

  return { pushFrame, flush, close };
}
