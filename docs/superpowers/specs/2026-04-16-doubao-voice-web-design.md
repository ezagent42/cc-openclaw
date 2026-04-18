# Doubao Realtime Voice Web App — Design Spec

## Goal

Build a Next.js web app that connects to the Volcengine doubao end-to-end realtime voice API via WebSocket, enabling browser-based voice conversations. MVP validates the full audio loop: mic capture → doubao API → TTS playback + ASR transcript display.

## Success Criteria

1. User clicks "Start Call" → receives an audio greeting from the bot
2. User speaks → bot echoes back "你说的是：{what the user said}"
3. Both sent and received text are displayed in real-time on the page
4. User clicks "End Call" → session cleanly terminates

## Architecture

```
Browser                          Custom Server                    Doubao API
───────                          ─────────────                    ─────────
getUserMedia                     server.ts                        wss://openspeech.bytedance.com
  ↓ PCM 16kHz int16                                               /api/v3/realtime/dialogue
AudioWorklet resample            /api/ws upgrade
  ↓                                ↓
WS binary frames ──────────→  transparent pipe ──────────→  binary frames (with headers)
WS binary frames ←──────────  transparent pipe ←──────────  TTS audio + ASR events
  ↓
AudioContext playback (PCM 24kHz s16le)
React state update (ASR text)
```

The custom server (`server.ts`) is a transparent WebSocket proxy. It:
- Listens on a single port (3000) for all traffic
- Upgrades `/api/ws` requests to WebSocket, connects to doubao with `X-Api-*` headers injected
- Pipes all binary frames bidirectionally without inspecting or modifying them
- Delegates all other HTTP requests to the standard Next.js request handler

All protocol encoding/decoding happens in the browser. The server has zero knowledge of the doubao binary protocol.

## Binary Protocol

Ported from the official Python `protocol.py`. The protocol uses a 4-byte header followed by optional fields and payload.

### Header (4 bytes)

| Byte | Bits 7-4 | Bits 3-0 |
|------|----------|----------|
| 0 | version=0b0001 | header_size=0b0001 |
| 1 | message_type | flags |
| 2 | serialization | compression |
| 3 | reserved=0x00 | |

### Message Types (client)

- `0b0001` — Full client request (JSON events: StartConnection, StartSession, SayHello, etc.)
- `0b0010` — Audio-only request (PCM audio data for TaskRequest)

### Message Types (server)

- `0b1001` — Full server response (JSON events: SessionStarted, ASRResponse, TTSSentenceStart, etc.)
- `0b1011` — Server ACK (binary audio data: TTSResponse)
- `0b1111` — Error response

### Flags

- `0b0100` (MSG_WITH_EVENT) — frame includes a 4-byte event ID after the header

### Frame Layout (client → server)

For JSON events (StartConnection, StartSession, SayHello, FinishSession, etc.):
```
[4B header] [4B event_id] [4B session_id_len] [session_id] [4B payload_len] [gzipped JSON payload]
```

For audio (TaskRequest event 200):
```
[4B header] [4B event_id] [4B session_id_len] [session_id] [4B payload_len] [gzipped PCM data]
```

Note: StartConnection (event 1) and FinishConnection (event 2) do NOT include session_id fields — only event_id + payload.

### Frame Layout (server → client)

```
[4B header] [optional 4B seq] [4B event_id] [4B session_id_len] [session_id] [4B payload_len] [payload]
```

**Important**: Optional fields (seq, event) are parsed sequentially with an advancing cursor. The Python reference `protocol.py` has a bug at lines 107-112 where it reads both seq and event from `payload[:4]` instead of advancing the offset. The TypeScript port MUST use a proper advancing cursor.

Payload is gzipped JSON for text events, raw binary for audio (TTSResponse event 352).

### Event → Session ID Rules

| Events | Session ID included? |
|--------|---------------------|
| StartConnection (1), FinishConnection (2) | No — connection-level |
| StartSession (100), FinishSession (102), TaskRequest (200), SayHello (300), ChatTTSText (500), ChatTextQuery (501) | Yes — session-level |

### Audio Frame Header Differences

JSON event frames use: `message_type=0b0001` (CLIENT_FULL_REQUEST), `serialization=0b0001` (JSON), `compression=0b0001` (GZIP).

Audio frames (TaskRequest event 200) use: `message_type=0b0010` (CLIENT_AUDIO_ONLY_REQUEST), `serialization=0b0000` (NO_SERIALIZATION), `compression=0b0001` (GZIP). The audio bytes are still gzip-compressed despite NO_SERIALIZATION.

## Session Lifecycle

```
idle → connecting → greeting → talking → ending → idle
                              ↘ error (on event 51/153)

1. idle:        User clicks "Start Call"
2. connecting:  Open WS to /api/ws
                Send StartConnection (event 1) → wait ConnectionStarted (event 50)
                  On ConnectionFailed (event 51) → error state, surface message via onError()
                Send StartSession (event 100) → wait SessionStarted (event 150)
                  On SessionFailed (event 153) → error state, surface message via onError()
3. greeting:    Send SayHello (event 300, content="你好，请问有什么可以帮你？")
                Receive TTSResponse (event 352) audio → play via AudioContext
                Wait TTSEnded (event 359) → transition to talking
4. talking:     Stream mic audio as TaskRequest (event 200) every 20ms
                Receive ASRResponse (event 451) → display transcript
                Receive TTSResponse (event 352) → play audio
                On ASRInfo (event 450) → clear audio queue (user interrupting)
5. ending:      User clicks "End Call"
                Send FinishSession (event 102) → wait SessionFinished (event 152)
                Send FinishConnection (event 2) → wait ConnectionFinished (event 52)
                Close WebSocket
```

## Audio Pipeline

### Input (Mic → Server)

1. `getUserMedia({ audio: { sampleRate: 16000, channelCount: 1 } })` — request mic
2. If browser resampling not available, use AudioWorklet to downsample to 16kHz
3. Convert Float32 samples to Int16 PCM (multiply by 32767, clamp)
4. Chunk into 640 bytes (20ms at 16kHz 16bit mono) — matching API recommended cadence
5. Wrap in doubao binary frame (event 200) with gzip compression
6. Send via WebSocket

### Output (Server → Speaker)

1. Parse doubao binary frame, extract PCM s16le 24kHz payload from TTSResponse (event 352)
2. Convert Int16 PCM to Float32 (divide by 32768)
3. Create AudioBuffer (24kHz, 1 channel)
4. Queue buffers and schedule playback via AudioContext with precise timing
5. On ASRInfo (event 450): clear playback queue (user is interrupting the bot)

## StartSession Configuration

```json
{
  "tts": {
    "audio_config": {
      "format": "pcm_s16le",
      "sample_rate": 24000,
      "channel": 1
    },
    "speaker": "zh_female_vv_jupiter_bigtts"
  },
  "asr": {
    "audio_info": {
      "format": "pcm",
      "sample_rate": 16000,
      "channel": 1
    },
    "extra": {
      "end_smooth_window_ms": 1500
    }
  },
  "dialog": {
    "bot_name": "小助手",
    "system_role": "你是一个语音回显测试助手。当用户说了一句话后，你需要重复用户说的内容，格式为：你说的是：{用户说的内容}。保持简洁，不要添加额外内容。",
    "speaking_style": "语速适中，语调自然，简洁明了。",
    "extra": {
      "input_mod": "keep_alive",
      "recv_timeout": 30
    }
  },
  "extra": {
    "model": "1.2.1.1"
  }
}
```

## File Structure

```
voice-web/
├── server.ts                  # Custom HTTP+WS server, transparent proxy to doubao
├── src/
│   ├── app/
│   │   ├── page.tsx           # Call UI: start/end button + transcript list
│   │   ├── layout.tsx         # (existing)
│   │   └── globals.css        # (existing)
│   └── lib/
│       ├── doubao-protocol.ts # Binary protocol encode/decode (port of protocol.py)
│       ├── audio-capture.ts   # getUserMedia → AudioWorklet → PCM 16kHz int16 chunks
│       ├── audio-playback.ts  # PCM s16le 24kHz → AudioContext queued playback
│       └── voice-session.ts   # Session state machine, WS lifecycle, event dispatch
├── .env.local                 # (existing) credentials
├── next.config.ts             # (existing)
├── package.json               # Add: ws dependency
└── tsconfig.json              # Adjust: include server.ts
```

### Module Responsibilities

**`server.ts`** (~70 lines)
- Create HTTP server, attach Next.js request handler
- On upgrade to `/api/ws`: connect to `wss://openspeech.bytedance.com/api/v3/realtime/dialogue` with headers from env vars (including `X-Api-Connect-Id: <uuid>` for tracing)
- Pipe frames bidirectionally between browser WS and doubao WS
- On either side close/error: close the other side

**`doubao-protocol.ts`** (~120 lines)
- `buildClientFrame(eventId, sessionId?, payload?, isAudio?)` — constructs the 4-byte header + optional fields + gzipped payload. For audio frames (isAudio=true): uses CLIENT_AUDIO_ONLY_REQUEST + NO_SERIALIZATION. For JSON events: uses CLIENT_FULL_REQUEST + JSON serialization. Connection-level events (1, 2) omit session_id; session-level events (100, 102, 200, 300, etc.) include it.
- `parseServerFrame(data: ArrayBuffer)` — parses header with advancing cursor (avoiding the Python reference bug), extracts event ID, session ID, payload (handles gzip decompression and JSON deserialization)
- Constants for all message types, flags, event IDs

**`audio-capture.ts`** (~80 lines)
- `startCapture()` — requests mic, creates AudioContext + AudioWorklet
- AudioWorklet processor: accumulates samples, outputs Int16 PCM chunks of 3200 bytes
- `onChunk` callback for each ready chunk
- `stopCapture()` — stops tracks, closes AudioContext

**`audio-playback.ts`** (~60 lines)
- `createPlayer()` — creates AudioContext at browser default sample rate; AudioBuffers are created at 24kHz and the browser handles resampling
- `enqueue(pcmInt16: ArrayBuffer)` — converts to Float32, creates AudioBuffer, schedules at correct time
- `clear()` — stops and disconnects all scheduled AudioBufferSourceNodes, uses a GainNode for instant mute during transition
- `close()` — close AudioContext

**`voice-session.ts`** (~150 lines)
- State machine: `idle | connecting | greeting | talking | ending`
- `start()` — opens WS, sends StartConnection + StartSession + SayHello
- `stop()` — sends FinishSession + FinishConnection, closes WS
- WS message handler: parses frames, dispatches to audio-playback or updates transcript state
- Exposes callbacks: `onTranscript(text, isInterim, role)`, `onStateChange(state)`, `onError(msg)`

**`page.tsx`** (~100 lines)
- Single page with:
  - "Start Call" / "End Call" button (toggles based on session state)
  - Status indicator (connecting, greeting, talking)
  - Scrollable transcript list showing user (ASR) and bot (TTS sentence text) messages
- Uses `voice-session.ts` via callbacks, stores transcript in React state

## Dependencies

- `ws` — WebSocket library for server.ts (Node.js side)
- `pako` — gzip compress/decompress in browser (for doubao protocol frames)

No other new dependencies needed. Audio capture and playback use native Web Audio API.

## Cloudflare Tunnel

Current config routes `voice.ezagent.chat` to `localhost:13036`. Update to point to `localhost:3000` (Next.js custom server port). The `/api/ws` path will be handled by the custom server's WebSocket upgrade.

## Out of Scope (for this MVP)

- Feishu JSSDK integration (Phase 2)
- Channel server actor integration (Phase 2, after actor-model branch)
- Pretty UI / animations
- Error retry / reconnection logic
- Multiple concurrent sessions
