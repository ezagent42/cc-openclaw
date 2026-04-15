import React, { useEffect, useRef } from "react";
import type { TranscriptEntry } from "../hooks/useVoiceSession";

export function TranscriptPanel({ transcript, partialText }: { transcript: TranscriptEntry[]; partialText: string }) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [transcript, partialText]);

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "16px" }}>
      {transcript.map((e, i) => (
        <div key={i} style={{ marginBottom: "12px", textAlign: e.role === "user" ? "right" : "left" }}>
          <span style={{
            display: "inline-block", padding: "8px 12px", borderRadius: "12px", maxWidth: "80%",
            background: e.role === "user" ? "#007AFF" : "#E5E5EA",
            color: e.role === "user" ? "#fff" : "#000",
          }}>{e.text}</span>
        </div>
      ))}
      {partialText && (
        <div style={{ marginBottom: "12px", textAlign: "right", opacity: 0.6 }}>
          <span style={{ display: "inline-block", padding: "8px 12px", borderRadius: "12px", background: "#007AFF", color: "#fff" }}>
            {partialText}...
          </span>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
