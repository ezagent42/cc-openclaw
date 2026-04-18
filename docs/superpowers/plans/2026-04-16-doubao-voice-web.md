# Doubao Realtime Voice Web App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Next.js web app that connects to the Volcengine doubao end-to-end realtime voice API, enabling browser-based voice conversations with mic capture, TTS playback, and ASR transcript display.

**Architecture:** Custom Next.js server (`server.ts`) acts as a transparent WebSocket proxy between the browser and the doubao API, injecting auth headers. All protocol encoding/decoding happens in the browser using a TypeScript port of the official Python binary protocol. Audio is captured via AudioWorklet (16kHz PCM) and played via AudioContext (24kHz PCM).

**Tech Stack:** Next.js 16, React 19, TypeScript, `ws` (server WebSocket), `pako` (browser gzip), Web Audio API (AudioWorklet + AudioContext)

**Spec:** `docs/superpowers/specs/2026-04-16-doubao-voice-web-design.md`

**Reference implementation:** `realtime_dialog_example/python3.7/` — especially `protocol.py` (binary protocol) and `realtime_dialog_client.py` (session lifecycle)

---

## File Structure

```
voice-web/
├── server.ts                       # Custom HTTP+WS server
├── src/
│   ├── app/
│   │   ├── page.tsx                # Call UI (client component)
│   │   ├── layout.tsx              # (existing, unchanged)
│   │   └── globals.css             # (existing, minor additions)
│   └── lib/
│       ├── doubao-protocol.ts      # Binary protocol encode/decode
│       ├── audio-capture.ts        # Mic → PCM 16kHz int16 chunks
│       ├── audio-playback.ts       # PCM s16le 24kHz → AudioContext playback
│       └── voice-session.ts        # Session state machine + WS lifecycle
├── public/
│   └── pcm-processor.js           # AudioWorklet processor (plain JS, served as static asset)
├── __tests__/
│   └── doubao-protocol.test.ts     # Protocol unit tests
├── .env.local                      # (existing) credentials
├── next.config.ts                  # (existing, unchanged)
├── package.json                    # Add: ws, pako, @types/ws, vitest
└── tsconfig.json                   # Adjust: include server.ts
```

---

### Task 1: Project Setup — Dependencies and Config

**Files:**
- Modify: `voice-web/package.json`
- Modify: `voice-web/tsconfig.json`

- [ ] **Step 1: Install dependencies**

```bash
cd voice-web && pnpm add ws pako && pnpm add -D @types/ws vitest
```

- [ ] **Step 2: Update tsconfig.json to include server.ts and tests**

Read `voice-web/tsconfig.json`, then update it. The key change: add `"server.ts"` to the `include` array, and ensure `compilerOptions` has `"module": "nodenext"` or compatible setting for server-side code. Since server.ts will be run with `tsx`, the existing tsconfig mostly works — just ensure `include` covers everything:

```json
{
  "compilerOptions": {
    "target": "ES2017",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": true,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [
      {
        "name": "next"
      }
    ],
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

- [ ] **Step 3: Add dev script for custom server**

In `package.json`, update the `scripts` section:

```json
{
  "scripts": {
    "dev": "tsx server.ts",
    "build": "next build",
    "start": "NODE_ENV=production tsx server.ts",
    "lint": "eslint",
    "test": "vitest run",
    "test:watch": "vitest"
  }
}
```

This requires `tsx` as a dev dependency:

```bash
cd voice-web && pnpm add -D tsx
```

- [ ] **Step 4: Commit**

```bash
cd voice-web && git add package.json pnpm-lock.yaml tsconfig.json && git commit -m "chore: add ws, pako, vitest, tsx dependencies and config"
```

---

### Task 2: Binary Protocol — doubao-protocol.ts

This is the most critical module. It encodes client frames and decodes server frames following the doubao custom binary protocol. Ported from `realtime_dialog_example/python3.7/protocol.py`.

**Files:**
- Create: `voice-web/src/lib/doubao-protocol.ts`
- Create: `voice-web/__tests__/doubao-protocol.test.ts`

- [ ] **Step 1: Write the test file**

```typescript
// voice-web/__tests__/doubao-protocol.test.ts
import { describe, it, expect } from 'vitest';
import {
  buildClientFrame,
  parseServerFrame,
  CLIENT_FULL_REQUEST,
  CLIENT_AUDIO_ONLY_REQUEST,
  SERVER_FULL_RESPONSE,
  SERVER_ACK,
  SERVER_ERROR_RESPONSE,
  JSON_SERIAL,
  NO_SERIALIZATION,
  GZIP,
  MSG_WITH_EVENT,
  PROTOCOL_VERSION,
} from '../src/lib/doubao-protocol';
import pako from 'pako';

describe('buildClientFrame', () => {
  it('builds a StartConnection frame (event 1, no session ID)', () => {
    const frame = buildClientFrame(1, undefined, {});
    const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);

    // Byte 0: version=1, header_size=1
    expect(view.getUint8(0)).toBe(0x11);
    // Byte 1: message_type=CLIENT_FULL_REQUEST(0001), flags=MSG_WITH_EVENT(0100)
    expect(view.getUint8(1)).toBe((CLIENT_FULL_REQUEST << 4) | MSG_WITH_EVENT);
    // Byte 2: serialization=JSON(0001), compression=GZIP(0001)
    expect(view.getUint8(2)).toBe((JSON_SERIAL << 4) | GZIP);
    // Byte 3: reserved
    expect(view.getUint8(3)).toBe(0x00);

    // Bytes 4-7: event ID = 1
    expect(view.getUint32(4)).toBe(1);

    // No session ID fields — next is payload_size + payload
    const payloadSize = view.getUint32(8);
    const payloadBytes = new Uint8Array(frame.buffer, frame.byteOffset + 12, payloadSize);
    const decompressed = pako.inflate(payloadBytes);
    const json = JSON.parse(new TextDecoder().decode(decompressed));
    expect(json).toEqual({});
  });

  it('builds a StartSession frame (event 100, with session ID)', () => {
    const sessionId = 'test-session-123';
    const payload = { tts: { speaker: 'zh_female_vv_jupiter_bigtts' } };
    const frame = buildClientFrame(100, sessionId, payload);
    const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);

    // Header
    expect(view.getUint8(1)).toBe((CLIENT_FULL_REQUEST << 4) | MSG_WITH_EVENT);
    // Event ID
    expect(view.getUint32(4)).toBe(100);
    // Session ID length
    const sidLen = view.getUint32(8);
    expect(sidLen).toBe(sessionId.length);
    // Session ID
    const sidBytes = new Uint8Array(frame.buffer, frame.byteOffset + 12, sidLen);
    expect(new TextDecoder().decode(sidBytes)).toBe(sessionId);
    // Payload
    const payloadSize = view.getUint32(12 + sidLen);
    const payloadBytes = new Uint8Array(frame.buffer, frame.byteOffset + 16 + sidLen, payloadSize);
    const decompressed = pako.inflate(payloadBytes);
    const json = JSON.parse(new TextDecoder().decode(decompressed));
    expect(json).toEqual(payload);
  });

  it('builds an audio TaskRequest frame (event 200, audio mode)', () => {
    const sessionId = 'audio-session';
    const audioData = new Uint8Array([1, 2, 3, 4, 5]);
    const frame = buildClientFrame(200, sessionId, audioData, true);
    const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);

    // Byte 1: CLIENT_AUDIO_ONLY_REQUEST
    expect(view.getUint8(1)).toBe((CLIENT_AUDIO_ONLY_REQUEST << 4) | MSG_WITH_EVENT);
    // Byte 2: NO_SERIALIZATION + GZIP
    expect(view.getUint8(2)).toBe((NO_SERIALIZATION << 4) | GZIP);
  });
});

describe('parseServerFrame', () => {
  function buildMockServerFrame(
    messageType: number,
    eventId: number,
    sessionId: string,
    payload: object | Uint8Array,
    isAudio: boolean = false
  ): ArrayBuffer {
    const parts: number[] = [];
    // Header
    parts.push(0x11); // version + header_size
    parts.push((messageType << 4) | MSG_WITH_EVENT);
    if (isAudio) {
      parts.push((NO_SERIALIZATION << 4) | GZIP);
    } else {
      parts.push((JSON_SERIAL << 4) | GZIP);
    }
    parts.push(0x00);

    const header = new Uint8Array(parts);
    const buffers: Uint8Array[] = [header];

    // Event ID (4 bytes big-endian)
    const eventBuf = new Uint8Array(4);
    new DataView(eventBuf.buffer).setUint32(0, eventId);
    buffers.push(eventBuf);

    // Session ID
    const sidBytes = new TextEncoder().encode(sessionId);
    const sidLenBuf = new Uint8Array(4);
    new DataView(sidLenBuf.buffer).setUint32(0, sidBytes.length);
    buffers.push(sidLenBuf);
    buffers.push(sidBytes);

    // Payload
    let payloadBytes: Uint8Array;
    if (isAudio) {
      payloadBytes = pako.gzip(payload as Uint8Array);
    } else {
      const jsonStr = JSON.stringify(payload);
      payloadBytes = pako.gzip(new TextEncoder().encode(jsonStr));
    }
    const plLenBuf = new Uint8Array(4);
    new DataView(plLenBuf.buffer).setUint32(0, payloadBytes.length);
    buffers.push(plLenBuf);
    buffers.push(payloadBytes);

    // Concatenate
    const total = buffers.reduce((s, b) => s + b.length, 0);
    const result = new Uint8Array(total);
    let offset = 0;
    for (const buf of buffers) {
      result.set(buf, offset);
      offset += buf.length;
    }
    return result.buffer;
  }

  it('parses a SERVER_FULL_RESPONSE with JSON payload', () => {
    const payload = { results: [{ text: '你好', is_interim: false }] };
    const frame = buildMockServerFrame(SERVER_FULL_RESPONSE, 451, 'sess-1', payload);
    const parsed = parseServerFrame(frame);

    expect(parsed.messageType).toBe('SERVER_FULL_RESPONSE');
    expect(parsed.event).toBe(451);
    expect(parsed.sessionId).toBe('sess-1');
    expect(parsed.payload).toEqual(payload);
  });

  it('parses a SERVER_ACK with binary audio payload', () => {
    const audioData = new Uint8Array([10, 20, 30, 40]);
    const frame = buildMockServerFrame(SERVER_ACK, 352, 'sess-1', audioData, true);
    const parsed = parseServerFrame(frame);

    expect(parsed.messageType).toBe('SERVER_ACK');
    expect(parsed.event).toBe(352);
    expect(parsed.payload).toBeInstanceOf(Uint8Array);
  });

  it('parses an error response', () => {
    // Error frame: [4B header] [4B error_code] [4B payload_size] [payload]
    const parts: Uint8Array[] = [];
    // Header
    parts.push(new Uint8Array([0x11, (SERVER_ERROR_RESPONSE << 4) | 0, (JSON_SERIAL << 4) | GZIP, 0x00]));
    // Error code
    const codeBuf = new Uint8Array(4);
    new DataView(codeBuf.buffer).setUint32(0, 1001);
    parts.push(codeBuf);
    // Payload
    const errPayload = pako.gzip(new TextEncoder().encode(JSON.stringify({ error: 'bad request' })));
    const plLen = new Uint8Array(4);
    new DataView(plLen.buffer).setUint32(0, errPayload.length);
    parts.push(plLen);
    parts.push(errPayload);

    const total = parts.reduce((s, b) => s + b.length, 0);
    const result = new Uint8Array(total);
    let offset = 0;
    for (const buf of parts) { result.set(buf, offset); offset += buf.length; }

    const parsed = parseServerFrame(result.buffer);
    expect(parsed.messageType).toBe('SERVER_ERROR');
    expect(parsed.code).toBe(1001);
    expect(parsed.payload).toEqual({ error: 'bad request' });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd voice-web && pnpm test
```

Expected: FAIL — `doubao-protocol` module does not exist yet.

- [ ] **Step 3: Implement doubao-protocol.ts**

```typescript
// voice-web/src/lib/doubao-protocol.ts
import pako from 'pako';

// Protocol constants
export const PROTOCOL_VERSION = 0b0001;
export const DEFAULT_HEADER_SIZE = 0b0001;

// Message types
export const CLIENT_FULL_REQUEST = 0b0001;
export const CLIENT_AUDIO_ONLY_REQUEST = 0b0010;
export const SERVER_FULL_RESPONSE = 0b1001;
export const SERVER_ACK = 0b1011;
export const SERVER_ERROR_RESPONSE = 0b1111;

// Flags
export const MSG_WITH_EVENT = 0b0100;
export const NEG_SEQUENCE = 0b0010;

// Serialization
export const NO_SERIALIZATION = 0b0000;
export const JSON_SERIAL = 0b0001;

// Compression
export const NO_COMPRESSION = 0b0000;
export const GZIP = 0b0001;

// Client event IDs
export const EVENT_START_CONNECTION = 1;
export const EVENT_FINISH_CONNECTION = 2;
export const EVENT_START_SESSION = 100;
export const EVENT_FINISH_SESSION = 102;
export const EVENT_TASK_REQUEST = 200;
export const EVENT_SAY_HELLO = 300;
export const EVENT_CHAT_TTS_TEXT = 500;
export const EVENT_CHAT_TEXT_QUERY = 501;
export const EVENT_CLIENT_INTERRUPT = 515;

// Server event IDs
export const EVENT_CONNECTION_STARTED = 50;
export const EVENT_CONNECTION_FAILED = 51;
export const EVENT_CONNECTION_FINISHED = 52;
export const EVENT_SESSION_STARTED = 150;
export const EVENT_SESSION_FINISHED = 152;
export const EVENT_SESSION_FAILED = 153;
export const EVENT_TTS_SENTENCE_START = 350;
export const EVENT_TTS_SENTENCE_END = 351;
export const EVENT_TTS_RESPONSE = 352;
export const EVENT_TTS_ENDED = 359;
export const EVENT_ASR_INFO = 450;
export const EVENT_ASR_RESPONSE = 451;
export const EVENT_ASR_ENDED = 459;

// Connection-level events (no session ID in frame)
const CONNECTION_EVENTS = new Set([
  EVENT_START_CONNECTION,
  EVENT_FINISH_CONNECTION,
]);

/**
 * Build a client frame for sending to the doubao WebSocket.
 *
 * @param eventId - The event ID (1, 2, 100, 102, 200, 300, etc.)
 * @param sessionId - Session ID string (omit for connection-level events 1, 2)
 * @param payload - JSON object or Uint8Array (for audio)
 * @param isAudio - If true, uses CLIENT_AUDIO_ONLY_REQUEST + NO_SERIALIZATION
 */
export function buildClientFrame(
  eventId: number,
  sessionId?: string,
  payload?: object | Uint8Array,
  isAudio: boolean = false,
): Uint8Array {
  const messageType = isAudio ? CLIENT_AUDIO_ONLY_REQUEST : CLIENT_FULL_REQUEST;
  const serialization = isAudio ? NO_SERIALIZATION : JSON_SERIAL;

  // Header (4 bytes)
  const header = new Uint8Array(4);
  header[0] = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE;
  header[1] = (messageType << 4) | MSG_WITH_EVENT;
  header[2] = (serialization << 4) | GZIP;
  header[3] = 0x00;

  const parts: Uint8Array[] = [header];

  // Event ID (4 bytes, big-endian)
  const eventBuf = new Uint8Array(4);
  new DataView(eventBuf.buffer).setUint32(0, eventId);
  parts.push(eventBuf);

  // Session ID (only for session-level events)
  const includeSessionId = !CONNECTION_EVENTS.has(eventId) && sessionId != null;
  if (includeSessionId) {
    const sidBytes = new TextEncoder().encode(sessionId);
    const sidLenBuf = new Uint8Array(4);
    new DataView(sidLenBuf.buffer).setUint32(0, sidBytes.length);
    parts.push(sidLenBuf);
    parts.push(sidBytes);
  }

  // Payload — gzip compressed
  let payloadBytes: Uint8Array;
  if (isAudio && payload instanceof Uint8Array) {
    payloadBytes = pako.gzip(payload);
  } else {
    const jsonStr = JSON.stringify(payload ?? {});
    payloadBytes = pako.gzip(new TextEncoder().encode(jsonStr));
  }
  const plLenBuf = new Uint8Array(4);
  new DataView(plLenBuf.buffer).setUint32(0, payloadBytes.length);
  parts.push(plLenBuf);
  parts.push(payloadBytes);

  // Concatenate all parts
  const totalLen = parts.reduce((sum, p) => sum + p.length, 0);
  const result = new Uint8Array(totalLen);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

export interface ParsedServerFrame {
  messageType: 'SERVER_FULL_RESPONSE' | 'SERVER_ACK' | 'SERVER_ERROR' | 'UNKNOWN';
  event?: number;
  sessionId?: string;
  payload?: any;
  code?: number;
  seq?: number;
}

/**
 * Parse a server frame received from the doubao WebSocket.
 * Uses an advancing cursor to correctly handle optional fields
 * (fixes the bug in the Python reference at protocol.py:107-112).
 */
export function parseServerFrame(data: ArrayBuffer): ParsedServerFrame {
  const bytes = new Uint8Array(data);
  const view = new DataView(data);

  if (bytes.length < 4) {
    return { messageType: 'UNKNOWN' };
  }

  const headerSize = bytes[0] & 0x0f;
  const messageType = bytes[1] >> 4;
  const flags = bytes[1] & 0x0f;
  const serialization = bytes[2] >> 4;
  const compression = bytes[2] & 0x0f;

  // Skip header extension bytes
  let cursor = headerSize * 4;
  const result: ParsedServerFrame = { messageType: 'UNKNOWN' };

  if (messageType === SERVER_FULL_RESPONSE || messageType === SERVER_ACK) {
    result.messageType = messageType === SERVER_ACK ? 'SERVER_ACK' : 'SERVER_FULL_RESPONSE';

    // Optional: sequence number (if NEG_SEQUENCE flag set)
    if ((flags & NEG_SEQUENCE) > 0) {
      result.seq = view.getUint32(cursor);
      cursor += 4;
    }

    // Optional: event ID (if MSG_WITH_EVENT flag set)
    if ((flags & MSG_WITH_EVENT) > 0) {
      result.event = view.getUint32(cursor);
      cursor += 4;
    }

    // Session ID
    const sidLen = view.getInt32(cursor);
    cursor += 4;
    if (sidLen > 0) {
      result.sessionId = new TextDecoder().decode(bytes.slice(cursor, cursor + sidLen));
      cursor += sidLen;
    }

    // Payload
    const payloadSize = view.getUint32(cursor);
    cursor += 4;
    let payloadMsg: Uint8Array | string | object = bytes.slice(cursor, cursor + payloadSize);

    if (compression === GZIP && payloadSize > 0) {
      payloadMsg = pako.inflate(payloadMsg as Uint8Array);
    }

    if (serialization === JSON_SERIAL && payloadSize > 0) {
      const text = new TextDecoder().decode(payloadMsg as Uint8Array);
      payloadMsg = JSON.parse(text);
    }
    // For NO_SERIALIZATION (audio), keep as Uint8Array

    result.payload = payloadMsg;
  } else if (messageType === SERVER_ERROR_RESPONSE) {
    result.messageType = 'SERVER_ERROR';
    result.code = view.getUint32(cursor);
    cursor += 4;
    const payloadSize = view.getUint32(cursor);
    cursor += 4;
    let payloadMsg: Uint8Array | object = bytes.slice(cursor, cursor + payloadSize);

    if (compression === GZIP && payloadSize > 0) {
      payloadMsg = pako.inflate(payloadMsg as Uint8Array);
    }
    if (serialization === JSON_SERIAL && payloadSize > 0) {
      const text = new TextDecoder().decode(payloadMsg as Uint8Array);
      payloadMsg = JSON.parse(text);
    }
    result.payload = payloadMsg;
  }

  return result;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd voice-web && pnpm test
```

Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd voice-web && git add src/lib/doubao-protocol.ts __tests__/doubao-protocol.test.ts && git commit -m "feat: doubao binary protocol encoder/decoder with tests"
```

---

### Task 3: Custom Server — server.ts

A WebSocket proxy server that also serves the Next.js app.

**Files:**
- Create: `voice-web/server.ts`

**Reference:** The custom server pattern from Next.js docs uses `next({ dev })`, `app.prepare()`, `app.getRequestHandler()`.

- [ ] **Step 1: Create server.ts**

```typescript
// voice-web/server.ts
import { createServer } from 'node:http';
import { parse } from 'node:url';
import { randomUUID } from 'node:crypto';
import next from 'next';
import { WebSocketServer, WebSocket } from 'ws';

const dev = process.env.NODE_ENV !== 'production';
const port = parseInt(process.env.PORT || '3000', 10);

const app = next({ dev, hostname: 'localhost', port });
const handle = app.getRequestHandler();

const DOUBAO_WS_URL = 'wss://openspeech.bytedance.com/api/v3/realtime/dialogue';

app.prepare().then(() => {
  const server = createServer((req, res) => {
    const parsedUrl = parse(req.url!, true);
    handle(req, res, parsedUrl);
  });

  const wss = new WebSocketServer({ noServer: true });

  server.on('upgrade', (req, socket, head) => {
    const { pathname } = parse(req.url!, true);

    if (pathname === '/api/ws') {
      wss.handleUpgrade(req, socket, head, (browserWs) => {
        const connectId = randomUUID();
        console.log(`[ws-proxy] New connection ${connectId}`);

        // Connect to doubao with auth headers
        const doubaoWs = new WebSocket(DOUBAO_WS_URL, {
          headers: {
            'X-Api-App-ID': process.env.DOUBAO_APP_ID || '',
            'X-Api-Access-Key': process.env.DOUBAO_ACCESS_TOKEN || '',
            'X-Api-Resource-Id': 'volc.speech.dialog',
            'X-Api-App-Key': 'PlgvMymc7f3tQnJ6',
            'X-Api-Connect-Id': connectId,
          },
        });

        doubaoWs.on('open', () => {
          console.log(`[ws-proxy] Connected to doubao (${connectId})`);
        });

        // Browser → Doubao
        browserWs.on('message', (data: Buffer) => {
          if (doubaoWs.readyState === WebSocket.OPEN) {
            doubaoWs.send(data);
          }
        });

        // Doubao → Browser
        doubaoWs.on('message', (data: Buffer) => {
          if (browserWs.readyState === WebSocket.OPEN) {
            browserWs.send(data);
          }
        });

        // Close handling
        browserWs.on('close', () => {
          console.log(`[ws-proxy] Browser disconnected (${connectId})`);
          if (doubaoWs.readyState === WebSocket.OPEN) {
            doubaoWs.close();
          }
        });

        doubaoWs.on('close', () => {
          console.log(`[ws-proxy] Doubao disconnected (${connectId})`);
          if (browserWs.readyState === WebSocket.OPEN) {
            browserWs.close();
          }
        });

        doubaoWs.on('error', (err) => {
          console.error(`[ws-proxy] Doubao error (${connectId}):`, err.message);
          if (browserWs.readyState === WebSocket.OPEN) {
            browserWs.close();
          }
        });

        browserWs.on('error', (err) => {
          console.error(`[ws-proxy] Browser error (${connectId}):`, err.message);
          if (doubaoWs.readyState === WebSocket.OPEN) {
            doubaoWs.close();
          }
        });
      });
    }
  });

  server.listen(port, () => {
    console.log(`> Ready on http://localhost:${port}`);
  });
});
```

- [ ] **Step 2: Test that the server starts**

```bash
cd voice-web && timeout 10 pnpm dev 2>&1 || true
```

Expected: Server starts, prints `> Ready on http://localhost:3000`. The `timeout` will kill it after 10 seconds.

- [ ] **Step 3: Commit**

```bash
cd voice-web && git add server.ts && git commit -m "feat: custom server with WebSocket proxy to doubao API"
```

---

### Task 4: Audio Capture — AudioWorklet PCM Processor

Captures microphone audio and delivers 640-byte (20ms) PCM int16 chunks.

**Files:**
- Create: `voice-web/public/pcm-processor.js`
- Create: `voice-web/src/lib/audio-capture.ts`

- [ ] **Step 1: Create the AudioWorklet processor as a static JS file**

AudioWorklet processors are loaded via `addModule()` which fetches over HTTP. Next.js does not serve raw `.ts` files, so this MUST be a plain JavaScript file in `public/`. No imports allowed — it runs in a separate thread.

```javascript
// voice-web/public/pcm-processor.js
// AudioWorklet processor — plain JS, served as static asset.
// Accumulates Float32 audio samples, converts to Int16 PCM, outputs 640-byte chunks.
// 320 samples = 20ms at 16kHz, each sample is 2 bytes int16 = 640 bytes.

class PcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(320);
    this.writeIndex = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;

    for (let i = 0; i < input.length; i++) {
      this.buffer[this.writeIndex++] = input[i];

      if (this.writeIndex >= 320) {
        const pcm = new Int16Array(320);
        for (let j = 0; j < 320; j++) {
          const s = Math.max(-1, Math.min(1, this.buffer[j]));
          pcm[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(new Uint8Array(pcm.buffer), [pcm.buffer]);
        this.writeIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-processor', PcmProcessor);
```

- [ ] **Step 2: Create audio-capture.ts**

```typescript
// voice-web/src/lib/audio-capture.ts

export type OnChunkCallback = (pcmChunk: Uint8Array) => void;

let audioContext: AudioContext | null = null;
let mediaStream: MediaStream | null = null;
let sourceNode: MediaStreamAudioSourceNode | null = null;
let workletNode: AudioWorkletNode | null = null;

/**
 * Start capturing microphone audio.
 * Delivers 640-byte PCM int16 16kHz mono chunks via the callback.
 */
export async function startCapture(onChunk: OnChunkCallback): Promise<void> {
  // Request mic — try to get 16kHz natively, fall back to default
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      sampleRate: 16000,
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });

  // Create AudioContext at 16kHz for direct capture without resampling
  audioContext = new AudioContext({ sampleRate: 16000 });

  // Load the AudioWorklet processor from static asset
  await audioContext.audioWorklet.addModule('/pcm-processor.js');

  sourceNode = audioContext.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioContext, 'pcm-processor');

  workletNode.port.onmessage = (event: MessageEvent<Uint8Array>) => {
    onChunk(event.data);
  };

  sourceNode.connect(workletNode);
  // AudioWorklet doesn't need to connect to destination for processing,
  // but some browsers require it. Connect to a silent destination.
  workletNode.connect(audioContext.destination);
}

/**
 * Stop capturing microphone audio and release resources.
 */
export function stopCapture(): void {
  if (workletNode) {
    workletNode.disconnect();
    workletNode = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
}
```

- [ ] **Step 3: Commit**

```bash
cd voice-web && git add public/pcm-processor.js src/lib/audio-capture.ts && git commit -m "feat: audio capture with AudioWorklet PCM processor"
```

---

### Task 5: Audio Playback — PCM s16le 24kHz Player

Plays PCM s16le 24kHz audio chunks received from the doubao API.

**Files:**
- Create: `voice-web/src/lib/audio-playback.ts`

- [ ] **Step 1: Create audio-playback.ts**

```typescript
// voice-web/src/lib/audio-playback.ts

interface ScheduledSource {
  source: AudioBufferSourceNode;
  startTime: number;
}

let audioCtx: AudioContext | null = null;
let gainNode: GainNode | null = null;
let scheduledSources: ScheduledSource[] = [];
let nextStartTime = 0;

/**
 * Create the playback AudioContext. Call once before enqueue().
 * Uses the browser's default sample rate; AudioBuffers at 24kHz are
 * resampled automatically by the browser.
 */
export function createPlayer(): void {
  audioCtx = new AudioContext();
  gainNode = audioCtx.createGain();
  gainNode.connect(audioCtx.destination);
  nextStartTime = 0;
}

/**
 * Enqueue a PCM s16le chunk for playback.
 * @param pcmData - Raw PCM bytes (Int16, little-endian, 24kHz, mono)
 */
export function enqueue(pcmData: Uint8Array): void {
  if (!audioCtx || !gainNode) return;

  // Copy to aligned buffer (pako output may have non-aligned byteOffset)
  const aligned = new Uint8Array(pcmData);
  const int16 = new Int16Array(aligned.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }

  // Create AudioBuffer at 24kHz
  const buffer = audioCtx.createBuffer(1, float32.length, 24000);
  buffer.getChannelData(0).set(float32);

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(gainNode);

  // Schedule playback at the correct time
  const now = audioCtx.currentTime;
  if (nextStartTime < now) {
    nextStartTime = now;
  }
  source.start(nextStartTime);

  scheduledSources.push({ source, startTime: nextStartTime });
  nextStartTime += buffer.duration;

  // Clean up finished sources
  scheduledSources = scheduledSources.filter((s) => s.startTime + 1 > now);
}

/**
 * Clear all scheduled audio (for interruption when user starts speaking).
 * Instantly mutes via GainNode, stops all sources, resets timing.
 */
export function clearPlayback(): void {
  if (!audioCtx || !gainNode) return;

  // Instant mute
  gainNode.gain.setValueAtTime(0, audioCtx.currentTime);

  // Stop all scheduled sources
  for (const s of scheduledSources) {
    try {
      s.source.stop();
      s.source.disconnect();
    } catch {
      // Already stopped
    }
  }
  scheduledSources = [];
  nextStartTime = 0;

  // Restore volume
  gainNode.gain.setValueAtTime(1, audioCtx.currentTime + 0.05);
}

/**
 * Close the playback AudioContext and release resources.
 */
export function closePlayer(): void {
  clearPlayback();
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }
  gainNode = null;
}
```

- [ ] **Step 2: Commit**

```bash
cd voice-web && git add src/lib/audio-playback.ts && git commit -m "feat: PCM s16le 24kHz audio playback with interruption support"
```

---

### Task 6: Voice Session — State Machine and WS Lifecycle

Orchestrates the entire call: WebSocket connection, protocol events, audio capture/playback, and transcript callbacks.

**Files:**
- Create: `voice-web/src/lib/voice-session.ts`

- [ ] **Step 1: Create voice-session.ts**

```typescript
// voice-web/src/lib/voice-session.ts
import {
  buildClientFrame,
  parseServerFrame,
  EVENT_START_CONNECTION,
  EVENT_FINISH_CONNECTION,
  EVENT_START_SESSION,
  EVENT_FINISH_SESSION,
  EVENT_TASK_REQUEST,
  EVENT_SAY_HELLO,
  EVENT_CONNECTION_STARTED,
  EVENT_CONNECTION_FAILED,
  EVENT_SESSION_STARTED,
  EVENT_SESSION_FAILED,
  EVENT_SESSION_FINISHED,
  EVENT_TTS_SENTENCE_START,
  EVENT_TTS_RESPONSE,
  EVENT_TTS_ENDED,
  EVENT_ASR_INFO,
  EVENT_ASR_RESPONSE,
  EVENT_ASR_ENDED,
} from './doubao-protocol';
import { startCapture, stopCapture } from './audio-capture';
import { createPlayer, enqueue, clearPlayback, closePlayer } from './audio-playback';

export type SessionState = 'idle' | 'connecting' | 'greeting' | 'talking' | 'ending' | 'error';

export interface TranscriptEntry {
  role: 'user' | 'bot';
  text: string;
  isInterim: boolean;
  timestamp: number;
}

export type OnStateChange = (state: SessionState) => void;
export type OnTranscript = (entry: TranscriptEntry) => void;
export type OnError = (message: string) => void;

const SESSION_CONFIG = {
  tts: {
    audio_config: {
      format: 'pcm_s16le',
      sample_rate: 24000,
      channel: 1,
    },
    speaker: 'zh_female_vv_jupiter_bigtts',
  },
  asr: {
    audio_info: {
      format: 'pcm',
      sample_rate: 16000,
      channel: 1,
    },
    extra: {
      end_smooth_window_ms: 1500,
    },
  },
  dialog: {
    bot_name: '小助手',
    system_role:
      '你是一个语音回显测试助手。当用户说了一句话后，你需要重复用户说的内容，格式为：你说的是：{用户说的内容}。保持简洁，不要添加额外内容。',
    speaking_style: '语速适中，语调自然，简洁明了。',
    extra: {
      input_mod: 'keep_alive',
      recv_timeout: 30,
    },
  },
  extra: {
    model: '1.2.1.1',
  },
};

export class VoiceSession {
  private ws: WebSocket | null = null;
  private sessionId: string;
  private state: SessionState = 'idle';

  public onStateChange: OnStateChange = () => {};
  public onTranscript: OnTranscript = () => {};
  public onError: OnError = () => {};

  constructor() {
    this.sessionId = crypto.randomUUID();
  }

  getState(): SessionState {
    return this.state;
  }

  private setState(newState: SessionState): void {
    this.state = newState;
    this.onStateChange(newState);
  }

  async start(): Promise<void> {
    if (this.state !== 'idle') return;

    this.sessionId = crypto.randomUUID();
    this.setState('connecting');

    try {
      // Determine WS URL based on current page location
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/api/ws`;

      this.ws = new WebSocket(wsUrl);
      this.ws.binaryType = 'arraybuffer';

      await new Promise<void>((resolve, reject) => {
        this.ws!.onopen = () => resolve();
        this.ws!.onerror = () => reject(new Error('WebSocket connection failed'));
      });

      this.ws.onmessage = (event: MessageEvent) => {
        this.handleServerMessage(event.data as ArrayBuffer);
      };

      this.ws.onclose = () => {
        if (this.state !== 'idle' && this.state !== 'ending') {
          this.setState('error');
          this.onError('WebSocket closed unexpectedly');
        }
      };

      // StartConnection
      this.sendFrame(EVENT_START_CONNECTION);

      // Wait for ConnectionStarted (event 50)
      await this.waitForEvent(EVENT_CONNECTION_STARTED);

      // StartSession
      this.sendFrame(EVENT_START_SESSION, SESSION_CONFIG);

      // Wait for SessionStarted (event 150)
      await this.waitForEvent(EVENT_SESSION_STARTED);

      // Greeting
      this.setState('greeting');
      this.sendFrame(EVENT_SAY_HELLO, { content: '你好，请问有什么可以帮你？' });

      // Init audio playback
      createPlayer();

      // Wait for greeting TTS to finish (event 359)
      await this.waitForEvent(EVENT_TTS_ENDED);

      // Transition to talking — start mic capture
      this.setState('talking');
      await startCapture((chunk: Uint8Array) => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN && this.state === 'talking') {
          const frame = buildClientFrame(EVENT_TASK_REQUEST, this.sessionId, chunk, true);
          this.ws.send(frame);
        }
      });
    } catch (err: any) {
      this.setState('error');
      this.onError(err.message || 'Failed to start session');
      this.cleanup();
    }
  }

  async stop(): Promise<void> {
    if (this.state !== 'talking' && this.state !== 'greeting') return;

    this.setState('ending');
    stopCapture();

    try {
      // FinishSession
      this.sendFrame(EVENT_FINISH_SESSION, {});

      // Brief wait for server to acknowledge, then close
      await this.waitForEvent(EVENT_SESSION_FINISHED, 3000).catch(() => {});

      // FinishConnection
      this.sendFrame(EVENT_FINISH_CONNECTION);

      // Brief wait then close
      await new Promise((r) => setTimeout(r, 500));
    } catch {
      // Best effort
    }

    this.cleanup();
    this.setState('idle');
  }

  private sendFrame(eventId: number, payload?: object | Uint8Array, isAudio: boolean = false): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    const needsSession = eventId !== EVENT_START_CONNECTION && eventId !== EVENT_FINISH_CONNECTION;
    const frame = buildClientFrame(
      eventId,
      needsSession ? this.sessionId : undefined,
      payload,
      isAudio,
    );
    this.ws.send(frame);
  }

  // Pending event waiters
  private eventWaiters = new Map<number, { resolve: () => void; reject: (err: Error) => void }>();

  private waitForEvent(eventId: number, timeoutMs: number = 10000): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.eventWaiters.delete(eventId);
        reject(new Error(`Timeout waiting for event ${eventId}`));
      }, timeoutMs);

      this.eventWaiters.set(eventId, {
        resolve: () => {
          clearTimeout(timer);
          this.eventWaiters.delete(eventId);
          resolve();
        },
        reject: (err: Error) => {
          clearTimeout(timer);
          this.eventWaiters.delete(eventId);
          reject(err);
        },
      });
    });
  }

  private handleServerMessage(data: ArrayBuffer): void {
    const parsed = parseServerFrame(data);

    // Check event waiters
    if (parsed.event != null) {
      const waiter = this.eventWaiters.get(parsed.event);
      if (waiter) {
        waiter.resolve();
      }
    }

    // Handle error events
    if (parsed.event === EVENT_CONNECTION_FAILED || parsed.event === EVENT_SESSION_FAILED) {
      const errorMsg = parsed.payload?.error || `Event ${parsed.event} failed`;
      this.setState('error');
      this.onError(errorMsg);
      // Reject any pending waiters
      for (const [, waiter] of this.eventWaiters) {
        waiter.reject(new Error(errorMsg));
      }
      this.eventWaiters.clear();
      this.cleanup();
      return;
    }

    if (parsed.messageType === 'SERVER_ERROR') {
      this.setState('error');
      this.onError(`Server error ${parsed.code}: ${JSON.stringify(parsed.payload)}`);
      this.cleanup();
      return;
    }

    // Handle TTS audio
    if (parsed.event === EVENT_TTS_RESPONSE && parsed.payload instanceof Uint8Array) {
      enqueue(parsed.payload);
    }

    // Handle TTS sentence start (bot text)
    if (parsed.event === EVENT_TTS_SENTENCE_START && parsed.payload) {
      const text = parsed.payload.text;
      if (text) {
        this.onTranscript({
          role: 'bot',
          text,
          isInterim: false,
          timestamp: Date.now(),
        });
      }
    }

    // Handle ASR response (user text)
    if (parsed.event === EVENT_ASR_RESPONSE && parsed.payload?.results) {
      for (const result of parsed.payload.results) {
        this.onTranscript({
          role: 'user',
          text: result.text,
          isInterim: result.is_interim ?? false,
          timestamp: Date.now(),
        });
      }
    }

    // Handle ASR info — user interruption, clear playback
    if (parsed.event === EVENT_ASR_INFO) {
      clearPlayback();
    }
  }

  private cleanup(): void {
    stopCapture();
    closePlayer();
    if (this.ws) {
      this.ws.onmessage = null;
      this.ws.onclose = null;
      this.ws.onerror = null;
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.close();
      }
      this.ws = null;
    }
  }
}
```

- [ ] **Step 2: Commit**

```bash
cd voice-web && git add src/lib/voice-session.ts && git commit -m "feat: voice session state machine with doubao protocol lifecycle"
```

---

### Task 7: Page UI — Call Interface with Transcript

The main page with start/end call button and real-time transcript display.

**Files:**
- Modify: `voice-web/src/app/page.tsx`
- Modify: `voice-web/src/app/globals.css`

- [ ] **Step 1: Replace page.tsx with call UI**

```tsx
// voice-web/src/app/page.tsx
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { VoiceSession, type SessionState, type TranscriptEntry } from '@/lib/voice-session';

export default function Home() {
  const [state, setState] = useState<SessionState>('idle');
  const [transcripts, setTranscripts] = useState<TranscriptEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef<VoiceSession | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll transcript
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [transcripts]);

  const handleTranscript = useCallback((entry: TranscriptEntry) => {
    setTranscripts((prev) => {
      // For interim ASR results, replace the last interim entry from the same role
      if (entry.isInterim && entry.role === 'user') {
        const lastIdx = prev.length - 1;
        if (lastIdx >= 0 && prev[lastIdx].role === 'user' && prev[lastIdx].isInterim) {
          const updated = [...prev];
          updated[lastIdx] = entry;
          return updated;
        }
      }
      return [...prev, entry];
    });
  }, []);

  const handleStart = useCallback(async () => {
    setError(null);
    setTranscripts([]);
    const session = new VoiceSession();
    sessionRef.current = session;

    session.onStateChange = (s) => setState(s);
    session.onTranscript = handleTranscript;
    session.onError = (msg) => setError(msg);

    await session.start();
  }, [handleTranscript]);

  const handleStop = useCallback(async () => {
    await sessionRef.current?.stop();
    sessionRef.current = null;
  }, []);

  const isActive = state === 'connecting' || state === 'greeting' || state === 'talking' || state === 'ending';

  return (
    <div className="flex flex-col h-screen bg-zinc-50 dark:bg-zinc-950">
      {/* Header */}
      <header className="flex items-center justify-center py-4 border-b border-zinc-200 dark:border-zinc-800">
        <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
          Voice Test
        </h1>
      </header>

      {/* Transcript area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6 space-y-3">
        {transcripts.map((t, i) => (
          <div
            key={i}
            className={`max-w-[80%] px-4 py-2 rounded-2xl text-sm ${
              t.role === 'user'
                ? 'ml-auto bg-blue-500 text-white'
                : 'mr-auto bg-zinc-200 dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100'
            } ${t.isInterim ? 'opacity-60' : ''}`}
          >
            {t.text}
          </div>
        ))}

        {state === 'connecting' && (
          <div className="text-center text-sm text-zinc-400">Connecting...</div>
        )}
        {state === 'greeting' && (
          <div className="text-center text-sm text-zinc-400">Bot is greeting...</div>
        )}
      </div>

      {/* Error display */}
      {error && (
        <div className="mx-4 mb-2 px-4 py-2 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 text-sm rounded-lg">
          {error}
        </div>
      )}

      {/* Controls */}
      <div className="flex items-center justify-center py-6 border-t border-zinc-200 dark:border-zinc-800">
        {!isActive ? (
          <button
            onClick={handleStart}
            className="px-8 py-3 bg-green-500 hover:bg-green-600 text-white font-medium rounded-full transition-colors"
          >
            Start Call
          </button>
        ) : (
          <button
            onClick={handleStop}
            disabled={state === 'ending'}
            className="px-8 py-3 bg-red-500 hover:bg-red-600 disabled:bg-zinc-400 text-white font-medium rounded-full transition-colors"
          >
            {state === 'ending' ? 'Ending...' : 'End Call'}
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
cd voice-web && git add src/app/page.tsx && git commit -m "feat: call UI with start/end button and real-time transcript"
```

---

### Task 8: Integration Test — End-to-End Smoke Test

Verify the complete flow works: start the server, open in browser, click Start Call, speak, see transcript.

**Files:** No new files — this is a manual integration test.

- [ ] **Step 1: Start the dev server**

```bash
cd voice-web && pnpm dev
```

Expected: `> Ready on http://localhost:3000`

- [ ] **Step 2: Open browser and test**

Open `http://localhost:3000` in Chrome/Edge. The page should show:
- A "Voice Test" header
- An empty transcript area
- A green "Start Call" button

- [ ] **Step 3: Test the call flow**

1. Click "Start Call" — browser should request mic permission, button changes to red "End Call"
2. Status should show "Connecting..." then "Bot is greeting..."
3. Bot audio greeting should play through speakers
4. After greeting, status clears — you're in talking mode
5. Speak into mic — you should see your words appear in blue bubbles (ASR transcript)
6. Bot should respond with "你说的是：..." in gray bubbles with TTS audio
7. Click "End Call" — session terminates cleanly

- [ ] **Step 4: Debug any issues**

Check browser console for errors (protocol parsing, audio context issues).
Check terminal for server logs (`[ws-proxy]` messages).

Common issues to check:
- If no audio plays: verify AudioContext was not suspended (Chrome requires user gesture)
- If WS connection fails: check `.env.local` credentials are correct
- If protocol parse errors: check binary frame structure in browser console

- [ ] **Step 5: Commit all fixes**

```bash
cd voice-web && git add -A && git commit -m "feat: integration fixes for end-to-end voice call"
```
