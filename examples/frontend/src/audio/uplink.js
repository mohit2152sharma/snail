// src/audio/uplink.js — mic -> worklet -> Opus -> onFrame(bytes).
import workletUrl from "./capture-worklet.js?url";

const SAMPLE_RATE = 48000;

export async function createUplink(onFrame) {
  let ctx = null;
  let stream = null;
  let node = null;
  let encoder = null;
  let muted = false;
  let baseTimeUs = 0;

  async function start() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: SAMPLE_RATE, echoCancellation: true },
    });
    ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await ctx.audioWorklet.addModule(workletUrl);
    const src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, "capture");

    encoder = new AudioEncoder({
      output: (chunk) => {
        const buf = new Uint8Array(chunk.byteLength);
        chunk.copyTo(buf);
        onFrame(buf);
      },
      error: (e) => console.error("[uplink] encoder error", e),
    });
    encoder.configure({ codec: "opus", sampleRate: SAMPLE_RATE, numberOfChannels: 1 });

    node.port.onmessage = (ev) => {
      if (muted || !encoder || encoder.state !== "configured") return;
      const samples = ev.data; // Float32Array, one channel
      const audioData = new AudioData({
        format: "f32",
        sampleRate: SAMPLE_RATE,
        numberOfFrames: samples.length,
        numberOfChannels: 1,
        timestamp: baseTimeUs,
        data: samples,
      });
      baseTimeUs += Math.round((samples.length / SAMPLE_RATE) * 1e6);
      encoder.encode(audioData);
      audioData.close();
    };

    src.connect(node);
    // Do not connect node to destination: we don't want to hear our own mic.
  }

  function setMuted(on) {
    muted = on;
  }

  async function stop() {
    try { node && (node.port.onmessage = null); } catch {}
    try { encoder && encoder.state !== "closed" && encoder.close(); } catch {}
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch {}
    try { ctx && (await ctx.close()); } catch {}
    ctx = stream = node = encoder = null;
    baseTimeUs = 0;
  }

  return { start, stop, setMuted };
}
