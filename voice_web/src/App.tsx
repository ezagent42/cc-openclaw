import React, { useEffect, useState } from "react";
import { useVoiceSession } from "./hooks/useVoiceSession";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { VoiceControls } from "./components/VoiceControls";

const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL || "wss://voice-lk.ezagent.chat";
const TOKEN_SERVER = import.meta.env.VITE_TOKEN_SERVER || "/api";
const FEISHU_APP_ID = import.meta.env.VITE_FEISHU_APP_ID || "";

declare global {
  interface Window {
    h5sdk?: {
      ready: (cb: () => void) => void;
      config: (opts: { appId: string; timestamp: string; nonceStr: string; signature: string; jsApiList: string[] }) => Promise<void>;
    };
    tt?: {
      requestAuthCode: (opts: { appId: string; success: (res: { code: string }) => void; fail: (err: unknown) => void }) => void;
    };
  }
}

async function initFeishuJssdk(): Promise<void> {
  if (!window.h5sdk) return;
  const res = await fetch(`${TOKEN_SERVER}/jssdk-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: window.location.href }),
  });
  if (!res.ok) throw new Error("JSSDK config failed");
  const { appId, timestamp, nonceStr, signature } = await res.json();
  await window.h5sdk.config({ appId, timestamp, nonceStr, signature, jsApiList: ["requestAuthCode"] });
}

async function getFeishuAuthCode(): Promise<string | null> {
  if (!window.tt) return null;
  return new Promise((resolve) => {
    window.tt!.requestAuthCode({
      appId: FEISHU_APP_ID,
      success: (res) => resolve(res.code),
      fail: () => resolve(null),
    });
  });
}

async function fetchToken(authCode: string): Promise<{ token: string; room: string; user: string }> {
  const res = await fetch(`${TOKEN_SERVER}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auth_code: authCode }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || "Authentication failed");
  }
  return res.json();
}

export default function App() {
  const [token, setToken] = useState("");
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");

  const { connected, transcript, partialText, connect, disconnect } =
    useVoiceSession({ livekitUrl: LIVEKIT_URL, token });

  useEffect(() => {
    (async () => {
      if (!window.h5sdk || !window.tt) {
        setError("请从飞书客户端打开此页面");
        return;
      }
      try {
        await initFeishuJssdk();
        const authCode = await getFeishuAuthCode();
        if (!authCode) { setError("飞书授权失败，请重试"); return; }
        const { token: tk } = await fetchToken(authCode);
        setToken(tk);
        setReady(true);
      } catch (e) {
        setError(e instanceof Error ? e.message : "认证失败");
      }
    })();
  }, []);

  if (error) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh",
        fontFamily: "-apple-system, sans-serif", textAlign: "center", padding: "32px", color: "#8E8E93" }}>
        <div>
          <p style={{ fontSize: "48px", margin: "0 0 16px" }}>&#x1f512;</p>
          <p style={{ fontSize: "16px" }}>{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", maxWidth: "480px",
      margin: "0 auto", fontFamily: "-apple-system, sans-serif" }}>
      <header style={{ padding: "16px", textAlign: "center", borderBottom: "1px solid #E5E5EA" }}>
        <h1 style={{ margin: 0, fontSize: "18px" }}>OpenClaw Voice</h1>
        <span style={{ fontSize: "12px", color: connected ? "#34C759" : "#8E8E93" }}>
          {connected ? "通话中" : ready ? "就绪" : "正在连接飞书..."}
        </span>
      </header>
      <TranscriptPanel transcript={transcript} partialText={partialText} />
      <VoiceControls connected={connected} onConnect={connect} onDisconnect={disconnect} />
    </div>
  );
}
