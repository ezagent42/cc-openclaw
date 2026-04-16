import { useCallback, useEffect, useRef, useState } from "react";
import { Room, RoomEvent } from "livekit-client";

export interface TranscriptEntry {
  role: "user" | "assistant";
  text: string;
  timestamp: number;
}

interface Options {
  livekitUrl: string;
  token: string;
}

export function useVoiceSession({ livekitUrl, token }: Options) {
  const [connected, setConnected] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [partialText, setPartialText] = useState("");
  const roomRef = useRef<Room | null>(null);

  useEffect(() => {
    return () => {
      roomRef.current?.disconnect();
      roomRef.current = null;
    };
  }, []);

  const connect = useCallback(async () => {
    const room = new Room();
    roomRef.current = room;

    room.on(RoomEvent.DataReceived, (payload: Uint8Array) => {
      const msg = JSON.parse(new TextDecoder().decode(payload));
      if (msg.type === "partial_transcript") {
        setPartialText(msg.text);
      } else if (msg.type === "final_transcript") {
        setPartialText("");
        setTranscript((prev) => [...prev, { role: "user", text: msg.text, timestamp: Date.now() }]);
      } else if (msg.type === "assistant_reply") {
        setTranscript((prev) => [...prev, { role: "assistant", text: msg.text, timestamp: Date.now() }]);
      }
    });

    room.on(RoomEvent.Connected, () => setConnected(true));
    room.on(RoomEvent.Disconnected, () => setConnected(false));

    await room.connect(livekitUrl, token);

    // Enable microphone — this triggers the browser permission prompt
    await room.localParticipant.setMicrophoneEnabled(true);
  }, [livekitUrl, token]);

  const disconnect = useCallback(async () => {
    if (roomRef.current) {
      await roomRef.current.disconnect();
      roomRef.current = null;
    }
  }, []);

  return { connected, transcript, partialText, connect, disconnect };
}
