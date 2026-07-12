// src/config.js — parameterize the shared frontend per example.

const DEFAULTS = {
  wsUrl: "ws://localhost:8000/ws",
  agents: ["host", "echo", "translate"],
  title: "Multi-Agent",
};

export function loadConfig() {
  const q = new URLSearchParams(window.location.search);
  const env = import.meta.env ?? {};
  const agentsRaw = q.get("agents") ?? env.VITE_AGENTS;
  return {
    wsUrl: q.get("ws") ?? env.VITE_WS_URL ?? DEFAULTS.wsUrl,
    agents: agentsRaw ? agentsRaw.split(",").map((s) => s.trim()).filter(Boolean) : DEFAULTS.agents,
    title: q.get("title") ?? env.VITE_TITLE ?? DEFAULTS.title,
  };
}
