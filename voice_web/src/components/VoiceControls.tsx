import React from "react";

export function VoiceControls({ connected, onConnect, onDisconnect }: {
  connected: boolean; onConnect: () => void; onDisconnect: () => void;
}) {
  return (
    <div style={{ display: "flex", justifyContent: "center", padding: "16px", borderTop: "1px solid #E5E5EA" }}>
      <button
        onClick={() => {
          console.log("Button clicked, connected:", connected);
          if (connected) onDisconnect();
          else onConnect();
        }}
        style={{
          padding: "12px 32px", borderRadius: "24px", border: "none", fontSize: "16px", cursor: "pointer",
          background: connected ? "#FF3B30" : "#34C759", color: "#fff",
        }}
      >
        {connected ? "结束通话" : "开始通话"}
      </button>
    </div>
  );
}
