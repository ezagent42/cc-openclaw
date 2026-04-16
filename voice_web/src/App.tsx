import React, { useEffect, useState } from "react";
import { useVoiceSession } from "./hooks/useVoiceSession";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { VoiceControls } from "./components/VoiceControls";

const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL || "ws://100.64.0.27:7880";
const TOKEN_SERVER = import.meta.env.VITE_TOKEN_SERVER || "/api";

async function fetchDevToken(): Promise<{ token: string; room: string; user: string }> {
  const res = await fetch(`${TOKEN_SERVER}/dev-token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: "dev-user" }),
  });
  if (!res.ok) throw new Error(`Token request failed: ${res.status}`);
  return res.json();
}

export default function App() {
  const [token, setToken] = useState("");
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("正在获取 token...");

  const { connected, transcript, partialText, connect, disconnect } =
    useVoiceSession({ livekitUrl: LIVEKIT_URL, token });

  useEffect(() => {
    (async () => {
      try {
        const { token: tk, room, user } = await fetchDevToken();
        setStatus(`Token OK, room=${room}, user=${user}`);
        setToken(tk);
        setReady(true);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  if (error) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh",
        fontFamily: "-apple-system, sans-serif", textAlign: "center", padding: "32px", color: "#FF3B30" }}>
        <div>
          <p style={{ fontSize: "18px", fontWeight: "bold" }}>Error</p>
          <p style={{ fontSize: "14px", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", maxWidth: "480px",
      margin: "0 auto", fontFamily: "-apple-system, sans-serif" }}>
      <header style={{ padding: "16px", textAlign: "center", borderBottom: "1px solid #E5E5EA" }}>
        <h1 style={{ margin: 0, fontSize: "18px" }}>OpenClaw Voice (Dev)</h1>
        <span style={{ fontSize: "12px", color: connected ? "#34C759" : "#8E8E93" }}>
          {connected ? "通话中" : ready ? "就绪 — 点击开始通话" : status}
        </span>
      </header>
      <TranscriptPanel transcript={transcript} partialText={partialText} />
      <VoiceControls connected={connected} onConnect={async () => {
        try {
          setStatus("正在连接 LiveKit...");
          await connect();
          setStatus("已连接");
        } catch (e) {
          setError(`连接失败: ${e instanceof Error ? e.message : JSON.stringify(e)}`);
        }
      }} onDisconnect={disconnect} />
    </div>
  );
}
