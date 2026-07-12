// src/audio/capture-worklet.js — posts Float32 render quanta to the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (ch && ch.length) {
      this.port.postMessage(ch.slice(0)); // copy: buffer is reused by the graph
    }
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);
