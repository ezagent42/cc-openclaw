# Split Mode (ASR + LLM + TTS) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "split" mode alongside the existing E2E mode. Split mode uses separate Realtime API connections for ASR and TTS, with pseudo_llm (future: agent) as the LLM layer in between.

**Architecture:** Two independent WebSocket connections to Volcengine Realtime API — one for streaming ASR (`bigmodel`), one for streaming TTS (`doubao-tts`). Browser sends PCM audio to gateway, gateway forwards Base64-encoded audio to ASR WS, receives transcript, calls pseudo_llm, sends result text to TTS WS, receives Base64 audio back, forwards raw PCM to browser. The browser↔gateway protocol stays identical to E2E mode (JSON control + raw binary audio).

**Tech Stack:** Python 3.11+ (aiohttp, websockets), Volcengine Realtime API (OpenAI-compatible JSON WS), existing voice-web frontend

**Spec:** ASR: `docs/realtime_doubao_stt.md`, TTS: `docs/realtime_doubao_tts.md`

**Credentials:** `VOLCENGINE_API_KEY` from `.env.local` (already available). ASR URL: `wss://ai-gateway.vei.volces.com/v1/realtime?model=bigmodel`. TTS URL: `wss://ai-gateway.vei.volces.com/v1/realtime?model=doubao-tts`.

---

## File Structure

```
voice_gateway/
├── config.py               # MODIFY: add Realtime API URLs + credentials
├── asr_client.py            # NEW: ASR Realtime API client
├── tts_client.py            # NEW: TTS Realtime API client
├── session_split.py         # NEW: Split mode session (ASR → pseudo_llm → TTS)
├── session.py               # KEEP: E2E mode session (unchanged)
├── server.py                # MODIFY: route to correct session by mode param
├── pseudo_llm.py            # KEEP: unchanged
├── tests/
│   ├── test_asr_client.py   # NEW: ASR client unit tests
│   └── test_tts_client.py   # NEW: TTS client unit tests

voice-web/
├── src/lib/voice-client.ts  # MODIFY: pass mode in start config
├── src/app/page.tsx          # MODIFY: add mode selector (E2E / Split)
```

---

### Task 1: Config — Add Realtime API settings

**Files:**
- Modify: `voice_gateway/config.py`

- [ ] **Step 1: Add Realtime API config**

Add to the end of `voice_gateway/config.py`:

```python
# Realtime API (split mode: separate ASR + TTS)
REALTIME_ASR_URL = "wss://ai-gateway.vei.volces.com/v1/realtime?model=bigmodel"
REALTIME_TTS_URL = "wss://ai-gateway.vei.volces.com/v1/realtime?model=doubao-tts"
REALTIME_TTS_VOICE = "zh_female_vv_jupiter_bigtts"
REALTIME_TTS_SAMPLE_RATE = 24000


def get_realtime_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('VOLCENGINE_API_KEY', '')}",
    }
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/config.py
git commit -m "feat(config): add Realtime API URLs and credentials for split mode"
```

---

### Task 2: ASR Client — asr_client.py

Connects to ASR Realtime API, sends Base64-encoded PCM audio, yields transcript results.

**Files:**
- Create: `voice_gateway/asr_client.py`

- [ ] **Step 1: Create asr_client.py**

```python
# voice_gateway/asr_client.py
"""ASR client using Volcengine Realtime API (OpenAI-compatible)."""
import asyncio
import base64
import json
import logging
from typing import AsyncGenerator

import websockets

from config import REALTIME_ASR_URL, get_realtime_headers

log = logging.getLogger(__name__)


class ASRClient:
    def __init__(self):
        self.ws = None

    async def connect(self) -> None:
        headers = get_realtime_headers()
        self.ws = await websockets.connect(
            REALTIME_ASR_URL,
            additional_headers=headers,
            ping_interval=None,
        )
        # Send session config
        session_msg = {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm",
                "input_audio_codec": "raw",
                "input_audio_sample_rate": 16000,
                "input_audio_bits": 16,
                "input_audio_channel": 1,
                "input_audio_transcription": {
                    "model": "bigmodel",
                },
            },
        }
        await self.ws.send(json.dumps(session_msg))

        # Wait for session.updated confirmation
        msg = await self.ws.recv()
        event = json.loads(msg)
        if event.get("type") != "transcription_session.updated":
            raise ConnectionError(f"ASR session setup failed: {event}")
        log.info("ASR connected and session configured")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send raw PCM audio chunk (Base64-encoded for Realtime API)."""
        b64 = base64.b64encode(pcm_bytes).decode("ascii")
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64,
        }))

    async def commit(self) -> None:
        """Signal end of audio input for current utterance."""
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def receive(self) -> AsyncGenerator[dict, None]:
        """Yield ASR events. Types: transcription.result (interim), transcription.completed (final)."""
        async for message in self.ws:
            if isinstance(message, str):
                event = json.loads(message)
                yield event

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("ASR WS closed")
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/asr_client.py
git commit -m "feat(gateway): ASR Realtime API client"
```

---

### Task 3: TTS Client — tts_client.py

Connects to TTS Realtime API, sends text, yields Base64-encoded audio chunks.

**Files:**
- Create: `voice_gateway/tts_client.py`

- [ ] **Step 1: Create tts_client.py**

```python
# voice_gateway/tts_client.py
"""TTS client using Volcengine Realtime API (OpenAI-compatible)."""
import asyncio
import base64
import json
import logging
from typing import AsyncGenerator

import websockets

from config import REALTIME_TTS_URL, REALTIME_TTS_VOICE, REALTIME_TTS_SAMPLE_RATE, get_realtime_headers

log = logging.getLogger(__name__)


class TTSClient:
    def __init__(self):
        self.ws = None

    async def connect(self) -> None:
        headers = get_realtime_headers()
        self.ws = await websockets.connect(
            REALTIME_TTS_URL,
            additional_headers=headers,
            ping_interval=None,
        )
        session_msg = {
            "type": "tts_session.update",
            "session": {
                "voice": REALTIME_TTS_VOICE,
                "output_audio_format": "pcm",
                "output_audio_sample_rate": REALTIME_TTS_SAMPLE_RATE,
                "text_to_speech": {
                    "model": "doubao-tts",
                },
            },
        }
        await self.ws.send(json.dumps(session_msg))

        msg = await self.ws.recv()
        event = json.loads(msg)
        if event.get("type") != "tts_session.updated":
            raise ConnectionError(f"TTS session setup failed: {event}")
        log.info("TTS connected and session configured")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Send text and yield PCM audio chunks as they arrive."""
        # Send text in one chunk
        await self.ws.send(json.dumps({
            "type": "input_text.append",
            "delta": text,
        }))
        await self.ws.send(json.dumps({"type": "input_text.done"}))

        # Read audio responses
        async for message in self.ws:
            if isinstance(message, str):
                event = json.loads(message)
                if event["type"] == "response.audio.delta":
                    yield base64.b64decode(event["delta"])
                elif event["type"] == "response.audio.done":
                    break

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("TTS WS closed")
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/tts_client.py
git commit -m "feat(gateway): TTS Realtime API client"
```

---

### Task 4: Split Session — session_split.py

Orchestrates the ASR → pseudo_llm → TTS pipeline. Same browser↔gateway protocol as E2E mode.

**Files:**
- Create: `voice_gateway/session_split.py`

- [ ] **Step 1: Create session_split.py**

```python
# voice_gateway/session_split.py
"""Split mode session: ASR → pseudo_llm → TTS (no E2E dialog)."""
import asyncio
import json
import logging

import aiohttp

from config import GREETING_TEXT, COMFORT_TEXT
from asr_client import ASRClient
from tts_client import TTSClient
from pseudo_llm import pseudo_llm

log = logging.getLogger(__name__)


class SplitSession:
    def __init__(self, browser_ws: aiohttp.web.WebSocketResponse, start_config: dict | None = None):
        self.browser_ws = browser_ws
        self.asr = ASRClient()
        self.tts = TTSClient()
        self.state = "idle"
        self.asr_text = ""
        self._asr_task: asyncio.Task | None = None
        self._query_task: asyncio.Task | None = None

        start_config = start_config or {}
        self._greeting_text = start_config.get("greeting") or GREETING_TEXT
        self._comfort_text = start_config.get("comfortText") or COMFORT_TEXT
        self._system_role = start_config.get("systemRole") or ""

    async def send_state(self, state: str) -> None:
        self.state = state
        await self.browser_ws.send_json({"type": "state", "state": state})

    async def send_transcript(self, role: str, text: str, interim: bool) -> None:
        if not text:
            return
        await self.browser_ws.send_json({
            "type": "transcript", "role": role, "text": text, "interim": interim,
        })

    async def send_error(self, message: str) -> None:
        await self.browser_ws.send_json({"type": "error", "message": message})

    async def run(self) -> None:
        try:
            await self.send_state("connecting")
            await self.asr.connect()
            await self.tts.connect()

            # Greeting via TTS
            await self.send_state("greeting")
            await self._speak(self._greeting_text)
            await self.send_transcript("bot", self._greeting_text, False)

            # Start ASR reader
            await self.send_state("talking")
            self._asr_task = asyncio.create_task(self._read_asr())

            # Main loop: read browser audio
            await self._browser_loop()
        except Exception as e:
            log.exception("SplitSession error")
            await self.send_error(str(e))
        finally:
            await self._cleanup()

    async def _speak(self, text: str) -> None:
        """Synthesize text and send PCM audio to browser."""
        async for audio_chunk in self.tts.synthesize(text):
            await self.browser_ws.send_bytes(audio_chunk)

    async def _read_asr(self) -> None:
        """Single consumer of ASR events."""
        try:
            async for event in self.asr.receive():
                evt_type = event.get("type", "")

                if evt_type == "conversation.item.input_audio_transcription.result":
                    text = event.get("transcript", "")
                    if text:
                        self.asr_text = text
                        await self.send_transcript("user", text, True)

                elif evt_type == "conversation.item.input_audio_transcription.completed":
                    text = event.get("transcript", "")
                    if text:
                        self.asr_text = text
                        await self.send_transcript("user", text, False)
                        # Trigger LLM + TTS
                        if self._query_task and not self._query_task.done():
                            self._query_task.cancel()
                        self._query_task = asyncio.create_task(self._run_query(text))
                        self.asr_text = ""

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"ASR read error: {e}")

    async def _browser_loop(self) -> None:
        """Read browser messages: binary audio or JSON control."""
        async for msg in self.browser_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self.asr.send_audio(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "stop":
                    await self._stop()
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    async def _run_query(self, text: str) -> None:
        """pseudo_llm → TTS pipeline."""
        try:
            log.info(f"Split query: '{text}'")
            result = await pseudo_llm(text)

            # Send LLM response as bot transcript
            await self.send_transcript("bot", result, False)

            # Speak the result via TTS
            # For now pseudo_llm returns JSON, so speak a summary
            # Future: LLM returns natural language text directly
            await self._speak(f"查询结果如下：{result[:200]}")

        except asyncio.CancelledError:
            log.info("Split query cancelled")
        except Exception as e:
            log.error(f"Split query error: {e}")

    async def _stop(self) -> None:
        await self.send_state("ending")
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        await self.asr.commit()
        await self.send_state("idle")

    async def _cleanup(self) -> None:
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        if self._asr_task and not self._asr_task.done():
            self._asr_task.cancel()
            try:
                await self._asr_task
            except asyncio.CancelledError:
                pass
        try:
            await self.asr.close()
        except Exception:
            pass
        try:
            await self.tts.close()
        except Exception:
            pass
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/session_split.py
git commit -m "feat(gateway): SplitSession — ASR → pseudo_llm → TTS pipeline"
```

---

### Task 5: Server — Route by mode

**Files:**
- Modify: `voice_gateway/server.py`

- [ ] **Step 1: Update server.py to accept mode**

Replace the ws_handler to route based on `mode` field in the start message:

```python
# In ws_handler, replace the session creation block:
        if data.get("type") == "start":
            mode = data.get("mode", "e2e")
            if mode == "split":
                from session_split import SplitSession
                session = SplitSession(ws, start_config=data)
            else:
                session = Session(ws, start_config=data)
            await session.run()
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/server.py
git commit -m "feat(gateway): route to E2E or Split session by mode param"
```

---

### Task 6: Frontend — Mode selector

**Files:**
- Modify: `voice-web/src/lib/voice-client.ts`
- Modify: `voice-web/src/app/page.tsx`

- [ ] **Step 1: Update voice-client.ts**

Add `mode` to the start config type:

```typescript
start(config?: { systemRole?: string; greeting?: string; comfortText?: string; mode?: string }): void {
```

- [ ] **Step 2: Update page.tsx**

Add a mode selector (radio group or select) in the Configuration tab. Add state:

```typescript
const [mode, setMode] = useState<'e2e' | 'split'>('e2e');
```

Pass it in handleStart:

```typescript
client.start({ systemRole, greeting, comfortText, mode });
```

Add UI between Comfort Text and Products sections:

```tsx
<div className="space-y-2">
  <Label>Mode</Label>
  <div className="flex gap-4">
    <label className="flex items-center gap-2 text-sm">
      <input type="radio" name="mode" value="e2e" checked={mode === 'e2e'}
        onChange={() => setMode('e2e')} disabled={isActive} />
      E2E (端到端语音对话)
    </label>
    <label className="flex items-center gap-2 text-sm">
      <input type="radio" name="mode" value="split" checked={mode === 'split'}
        onChange={() => setMode('split')} disabled={isActive} />
      Split (ASR + LLM + TTS)
    </label>
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice-web/src/lib/voice-client.ts voice-web/src/app/page.tsx
git commit -m "feat(web): add mode selector — E2E or Split"
```

---

### Task 7: Integration test

- [ ] **Step 1: Restart gateway**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web && make stop && make start
```

- [ ] **Step 2: Test E2E mode** — verify existing behavior unchanged

- [ ] **Step 3: Test Split mode** — select Split, Start Call, verify:
- Greeting plays via TTS
- ASR shows interim + final transcript
- pseudo_llm is called after ASR completion
- TTS speaks the response

- [ ] **Step 4: Check logs**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web && make logs
```

Verify: `ASR connected`, `TTS connected`, `Split query:`, transcript flow.

- [ ] **Step 5: Commit any fixes**
