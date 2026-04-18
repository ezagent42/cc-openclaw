# Voice Gateway — Design Spec

## Goal

Refactor the voice web app to move all doubao protocol logic, session management, and business logic into a Python voice gateway server. The browser becomes a thin UI + audio client. This enables the best-practice ChatTTSText + ChatRAGText flow and prepares for future actor model integration.

## Architecture

```
Browser (Next.js :13036)          Python Voice Gateway (:8089)          doubao E2E
  麦克风/扬声器/UI                  协议 + 会话 + 业务逻辑              ASR+LLM+TTS
  ←————— WS (简单协议) —————→     ←————— WS (doubao二进制) —————→
```

Cloudflare Tunnel (`voice.ezagent.chat`):
- `/ws` → `localhost:8089` (Python gateway)
- 其余 → `localhost:13036` (Next.js pages)

## Browser ↔ Python WS Protocol

### Browser → Python (上行)

| Type | Format | Description |
|------|--------|-------------|
| Start call | `{"type":"start"}` | Python connects to doubao, runs handshake + SayHello |
| Audio frame | Raw binary (PCM int16 16kHz mono) | Python wraps in doubao TaskRequest frame |
| Stop call | `{"type":"stop"}` | Python sends FinishSession + FinishConnection |

### Python → Browser (下行)

| Type | Format | Description |
|------|--------|-------------|
| State change | `{"type":"state","state":"<state>"}` | States: connecting, greeting, talking, ending, idle, error |
| TTS audio | Raw binary (PCM s16le 24kHz mono) | Browser plays directly via AudioContext |
| Transcript | `{"type":"transcript","role":"user\|bot","text":"...","interim":true\|false}` | ASR / ChatResponse text for UI display |
| Error | `{"type":"error","message":"..."}` | Error info |

## Python Voice Gateway

### Directory Structure

```
voice_gateway/
├── server.py              # aiohttp WS endpoint, serves /ws
├── session.py             # Per-call state machine, event dispatch, query orchestration
├── doubao_client.py       # doubao WS connection + binary protocol frame send/receive
├── protocol.py            # Binary protocol encode/decode (adapted from official example)
├── query.py               # Business query (hardcoded now, actor interface later)
└── config.py              # doubao credentials, session config, product catalog
```

### Module Responsibilities

**`server.py`** (~50 lines)
- aiohttp application with one WebSocket endpoint at `/ws`
- On new WS connection: create a `Session` instance, pass it the browser WS
- Load credentials from environment variables (same `.env.local` as before)

**`protocol.py`** (~130 lines)
- Adapted from `realtime_dialog_example/python3.7/protocol.py`
- **CRITICAL**: The official `parse_response` has a bug at lines 107-112 where both `seq` and `event` read from `payload[:4]` instead of advancing offset. Our implementation MUST use `payload[start:start+4]` with proper cursor advancement.
- `generate_header(message_type, serial_method, compression)` — 4-byte binary header
- `build_client_frame(event_id, session_id, payload, is_audio)` — full client frame. Connection-level events (1, 2) omit session_id; session-level events (100, 102, 200, 300, 500, 502) include it. Audio frames (event 200) use `CLIENT_AUDIO_ONLY_REQUEST` + `NO_SERIALIZATION` + `GZIP` (audio bytes are gzip-compressed).
- `parse_server_frame(data)` — parse server response, return dict with event, payload, etc.
- All constants (message types, flags, event IDs)

**`doubao_client.py`** (~120 lines)
- `DoubaoClient` class managing one WS connection to doubao
- `connect()` — open WS with auth headers, return logid
- `send_start_connection()` — event 1
- `send_start_session(config)` — event 100, with TTS/ASR/dialog config
- `send_say_hello(text)` — event 300
- `send_audio(pcm_bytes)` — event 200, CLIENT_AUDIO_ONLY_REQUEST
- `send_chat_tts_text(text, start, end)` — event 500
- `send_chat_rag_text(external_rag)` — event 502
- `send_finish_session()` — event 102
- `send_finish_connection()` — event 2
- `receive()` — async generator yielding parsed frames
- `close()` — clean disconnect

**`session.py`** (~200 lines)
- `Session` class — one per active call
- State machine: `idle → connecting → greeting → talking → ending → idle`
- Holds references to browser WS + DoubaoClient
- **Start flow:**
  1. Receive `{"type":"start"}` from browser
  2. Send `{"type":"state","state":"connecting"}` to browser
  3. Create DoubaoClient, connect, StartConnection, wait ConnectionStarted (50)
     - On ConnectionFailed (51) → send `{"type":"error"}` to browser, tear down
  4. StartSession (with config from config.py), wait SessionStarted (150)
     - On SessionFailed (153) → send `{"type":"error"}` to browser, tear down
  5. Send state "greeting", SayHello
  6. Forward TTS audio to browser, forward TTSSentenceStart (350, tts_type="chat_tts_text") text as bot transcript
  7. On TTSEnded (359) → send state "talking"
- **Talking flow (main loop):**
  - Session tracks `is_user_querying: bool` — set True on ASRInfo (450), reset False on ASREnded (459)
  - Browser binary → `doubao_client.send_audio()`
  - doubao ASRResponse (451) → extract text → send transcript to browser (role=user, interim based on is_interim)
  - doubao ChatResponse (550) → accumulate tokens → send transcript to browser (role=bot, interim=true)
  - doubao ChatEnded (559) → send transcript (role=bot, interim=false), reset accumulator
  - doubao TTSResponse (352) → extract PCM → send binary to browser
  - doubao TTSSentenceStart (350) → track `tts_type` for distinguishing default vs chat_tts_text/external_rag responses
  - doubao TTSSentenceEnd (351) → log/ignore
  - doubao UsageResponse (154) → log/ignore
  - doubao ASRInfo (450) → set `is_user_querying=True`, cancel pending query task if any
  - doubao ASREnded (459) → set `is_user_querying=False`, **trigger query orchestration** (see below)
  - doubao SERVER_ERROR → send `{"type":"error"}` to browser, tear down
- **Query orchestration (on ASREnded):**
  1. Get full ASR text from accumulated ASRResponse results
  2. Call `query.query(text)` — returns RAG JSON string or None
  3. If result is not None AND `is_user_querying` is False:
     a. Send ChatTTSText (start=true, content="稍等，我帮你查一下", end=true)
     b. Send ChatRAGText (external_rag=result)
  4. If result is None: do nothing, let E2E internal LLM handle it
  5. **CRITICAL**: ChatTTSText must ONLY be sent AFTER ASREnded (459). Never before.
  6. If ASRInfo (450) arrives after ChatTTSText was sent but before ChatRAGText: skip ChatRAGText, do not send the end packet (per official best practice Scene 2)
- **Stop flow:**
  1. Receive `{"type":"stop"}` from browser
  2. Send state "ending"
  3. FinishSession, wait SessionFinished (152, 3s timeout)
  4. FinishConnection, wait ConnectionFinished (52)
  5. Close doubao WS
  6. Send state "idle"

**`query.py`** (~30 lines)
- Hardcoded product catalog:
  ```python
  PRODUCTS = {
      "商品1": {"title": "商品1", "content": "价格：100元，库存充足"},
      "商品2": {"title": "商品2", "content": "价格：200元，库存充足"},
      "商品3": {"title": "商品3", "content": "价格：300元，限时优惠"},
      "商品4": {"title": "商品4", "content": "价格：400元，需要预订"},
      "商品5": {"title": "商品5", "content": "价格：500元，新品上市"},
  }
  ```
- `async def query(text: str) -> str | None` — scan text for product names, return JSON array string or None
- Future: replace with actor route call (`Send` to LLM actor, await response)

**`config.py`** (~40 lines)
- Load from environment: `DOUBAO_APP_ID`, `DOUBAO_ACCESS_TOKEN`
- doubao WS URL and fixed headers
- StartSession config (TTS format, speaker, ASR format, dialog system_role, model version)
- System role: "你是OpenClaw智能助手。当用户询问商品信息时，请基于提供的知识回答。如果没有相关知识，请如实告知。"

## Frontend Changes

### Files to Delete
- `src/lib/doubao-protocol.ts` — moved to Python
- `src/lib/voice-session.ts` — moved to Python
- `__tests__/doubao-protocol.test.ts` — tests move to Python
- `server.ts` WS proxy code — simplify to pure Next.js server (remove `ws` dependency)

### Files to Keep (unchanged)
- `src/lib/audio-capture.ts` — mic → PCM chunks
- `src/lib/audio-playback.ts` — PCM → AudioContext playback
- `public/pcm-processor.js` — AudioWorklet processor

### Files to Create
- `src/lib/voice-client.ts` — thin WS client replacing voice-session.ts

### Files to Modify
- `src/app/page.tsx` — swap VoiceSession for VoiceClient, handleTranscript logic stays
- `server.ts` — remove WS proxy, keep only Next.js page serving
- `package.json` — remove `ws` and `@types/ws` dependencies

### `voice-client.ts` Interface

```typescript
export class VoiceClient {
  onState: (state: string) => void;
  onTranscript: (role: 'user' | 'bot', text: string, interim: boolean) => void;
  onError: (message: string) => void;

  start(): void;   // Opens WS, sends {"type":"start"}, starts mic on "talking" state
  stop(): void;    // Sends {"type":"stop"}, stops mic
}
```

Handles:
- WS connect to `/ws` (routed to Python via Cloudflare tunnel or direct)
- Incoming JSON → dispatch to onState/onTranscript/onError callbacks
- Incoming binary → enqueue for audio playback
- "talking" state → startCapture, send PCM chunks as binary over WS
- "idle"/"ending" state → stopCapture

### `page.tsx` Changes

Minimal — replace `VoiceSession` import with `VoiceClient`. The `handleTranscript` unified logic (search backwards for same-role interim) stays exactly the same. The `TranscriptEntry` type stays the same.

## Cloudflare Tunnel Config

Update `~/.cloudflared/config.yml`:

```yaml
tunnel: ef0247e4-04da-4f61-aa2e-9a9f1490ba8d
credentials-file: /Users/h2oslabs/.cloudflared/ef0247e4-04da-4f61-aa2e-9a9f1490ba8d.json

ingress:
  - hostname: voice.ezagent.chat
    path: /ws
    service: http://localhost:8089
    originRequest:
      noTLSVerify: true
  - hostname: voice.ezagent.chat
    service: http://localhost:13036
  - service: http_status:404
```

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
    "bot_name": "OpenClaw助手",
    "system_role": "你是OpenClaw智能助手。当用户询问商品信息时，请基于提供的知识回答。如果没有相关知识，请如实告知。保持简洁友好。",
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

## Query Orchestration Flow

```
ASREnded (459)
  │
  ├── Accumulate full ASR text from prior ASRResponse events
  │
  ├── Call query.query(text)
  │     ├── Match found → returns '[{"title":"商品1","content":"价格：100元"}]'
  │     └── No match → returns None
  │
  ├── If result is not None:
  │     ├── Send ChatTTSText(start=true, content="稍等，我帮你查一下", end=true)
  │     ├── Send ChatRAGText(external_rag=result)
  │     └── doubao generates oral response based on RAG content
  │
  └── If result is None:
        └── Do nothing, let E2E internal LLM respond naturally
```

If ASRInfo (450) arrives during this flow (user interrupts), cancel the pending ChatTTSText/ChatRAGText — the E2E model will handle the new utterance.

## Makefile Updates

```makefile
# Add Python gateway to start/stop/restart
gateway:
    cd voice_gateway && uv run python server.py &

stop:
    # existing next.js + tunnel stop
    # + kill voice_gateway
```

## Out of Scope

- Actor model integration (future — replace query.py with actor route)
- Authentication / user identity
- Feishu JSSDK / card integration
- Multiple concurrent sessions on one gateway
- Persistent conversation history
