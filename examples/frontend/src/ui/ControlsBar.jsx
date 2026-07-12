import React, { useState } from "react";

export default function ControlsBar({ status, muted, onStart, onStop, onToggleMute, onBargeIn, onSendText }) {
  const [text, setText] = useState("");
  const live = status === "live";
  return (
    <div style={{ display: "flex", gap: 8, padding: 8, borderBottom: "1px solid #ccc", flexWrap: "wrap", alignItems: "center" }}>
      {live ? <button onClick={onStop}>Stop</button> : <button onClick={onStart}>Start</button>}
      <button disabled={!live} onClick={() => onToggleMute(!muted)}>{muted ? "Unmute" : "Mute"}</button>
      <button disabled={!live} onClick={onBargeIn}>Barge-in</button>
      <span style={{ marginLeft: "auto", opacity: 0.6 }}>{status}</span>
      <form
        style={{ display: "flex", gap: 4, flexBasis: "100%" }}
        onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onSendText(text); setText(""); } }}
      >
        <input style={{ flex: 1 }} placeholder="type a user turn" value={text} onChange={(e) => setText(e.target.value)} disabled={!live} />
        <button type="submit" disabled={!live}>Send</button>
      </form>
    </div>
  );
}
