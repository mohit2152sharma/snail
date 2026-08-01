// src/ui/ControlsBar.jsx — session controls + a typed user turn (no mic needed).
import React, { useState } from "react";

export default function ControlsBar({
  status, muted, onStart, onStop, onToggleMute, onBargeIn, onSendText,
}) {
  const [text, setText] = useState("");
  const live = status === "live";
  const busy = status === "connecting";

  return (
    <div className="controls">
      {live ? (
        <button className="danger" onClick={onStop}>Stop</button>
      ) : (
        <button className="primary" onClick={onStart} disabled={busy}>
          {busy ? "Connecting…" : "Start"}
        </button>
      )}
      <button disabled={!live} onClick={() => onToggleMute(!muted)}>
        {muted ? "Unmute" : "Mute"}
      </button>
      <button disabled={!live} onClick={onBargeIn}>Barge-in</button>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) { onSendText(text); setText(""); }
        }}
      >
        <input
          type="text"
          placeholder="type a user turn (skips the mic)"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!live}
        />
        <button type="submit" disabled={!live}>Send</button>
      </form>
    </div>
  );
}
