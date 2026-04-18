# Voice Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python voice gateway that manages the doubao E2E protocol, session lifecycle, and business query logic, with the browser reduced to a thin audio + UI client.

**Architecture:** Python aiohttp server on :8089 accepts browser WS connections with a simple JSON+binary protocol, manages the doubao binary WS connection (encode/decode, session lifecycle, ChatTTSText/ChatRAGText orchestration), and exposes hardcoded product queries. Next.js on :13036 only serves the frontend pages. Cloudflare tunnel routes `/ws` to Python, everything else to Next.js.

**Tech Stack:** Python 3.11+ (aiohttp, websockets), uv for package management, Next.js 16 (frontend only), existing audio-capture.ts + audio-playback.ts

**Spec:** `docs/superpowers/specs/2026-04-16-voice-gateway-design.md`

**Reference:** `realtime_dialog_example/python3.7/protocol.py` (binary protocol), `realtime_dialog_example/python3.7/realtime_dialog_client.py` (client pattern)

---

## File Structure

```
voice_gateway/
├── pyproject.toml          # uv project config, dependencies: aiohttp, websockets
├── config.py               # Credentials, doubao URL, StartSession config, product catalog
├── protocol.py             # Binary protocol encode/decode (fixed version of official)
├── doubao_client.py        # DoubaoClient class: WS connection + send/receive helpers
├── query.py                # Hardcoded product query
├── session.py              # Session state machine + event dispatch + query orchestration
├── server.py               # aiohttp app with /ws endpoint
└── tests/
    ├── test_protocol.py    # Protocol encode/decode tests
    └── test_query.py       # Query logic tests

voice-web/                  # Existing Next.js project
├── src/lib/
│   ├── voice-client.ts     # NEW: thin WS client (replaces voice-session.ts)
│   ├── audio-capture.ts    # KEEP: unchanged
│   └── audio-playback.ts   # KEEP: unchanged
├── src/app/
│   └── page.tsx            # MODIFY: swap VoiceSession → VoiceClient
├── server.ts               # MODIFY: remove WS proxy, keep Next.js serving only
└── package.json            # MODIFY: remove ws, @types/ws deps
```

---

### Task 1: Python Project Setup

**Files:**
- Create: `voice_gateway/pyproject.toml`
- Create: `voice_gateway/config.py`

- [ ] **Step 1: Initialize uv project**

```bash
cd /Users/h2oslabs/cc-openclaw-voice && mkdir -p voice_gateway && cd voice_gateway
uv init --no-readme
uv add aiohttp websockets
```

Then replace `pyproject.toml` with:

```toml
[project]
name = "voice-gateway"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "aiohttp>=3.11",
    "websockets>=15.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

```bash
uv add --dev pytest pytest-asyncio
```

- [ ] **Step 2: Create config.py**

```python
# voice_gateway/config.py
import os
import uuid

DOUBAO_WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"

def get_ws_headers() -> dict:
    return {
        "X-Api-App-ID": os.environ.get("DOUBAO_APP_ID", ""),
        "X-Api-Access-Key": os.environ.get("DOUBAO_ACCESS_TOKEN", ""),
        "X-Api-Resource-Id": "volc.speech.dialog",
        "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

START_SESSION_CONFIG = {
    "tts": {
        "audio_config": {
            "format": "pcm_s16le",
            "sample_rate": 24000,
            "channel": 1,
        },
        "speaker": "zh_female_vv_jupiter_bigtts",
    },
    "asr": {
        "audio_info": {
            "format": "pcm",
            "sample_rate": 16000,
            "channel": 1,
        },
        "extra": {
            "end_smooth_window_ms": 1500,
        },
    },
    # NOTE: asr.audio_info is explicitly included per spec review — do not rely on server defaults
    "dialog": {
        "bot_name": "OpenClaw助手",
        "system_role": "你是OpenClaw智能助手。当用户询问商品信息时，请基于提供的知识回答。如果没有相关知识，请如实告知。保持简洁友好。",
        "speaking_style": "语速适中，语调自然，简洁明了。",
        "extra": {
            "input_mod": "keep_alive",
            "recv_timeout": 30,
        },
    },
    "extra": {
        "model": "1.2.1.1",
    },
}

GREETING_TEXT = "你好，请问有什么可以帮你？"
COMFORT_TEXT = "稍等，我帮你查一下。"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/pyproject.toml voice_gateway/config.py voice_gateway/uv.lock voice_gateway/.python-version
git commit -m "feat(gateway): init Python voice gateway project with config"
```

---

### Task 2: Binary Protocol — protocol.py

Port and fix the official Python protocol. This is the foundation for all doubao communication.

**Files:**
- Create: `voice_gateway/protocol.py`
- Create: `voice_gateway/tests/__init__.py`
- Create: `voice_gateway/tests/test_protocol.py`

- [ ] **Step 1: Write tests**

```python
# voice_gateway/tests/__init__.py
# (empty)
```

```python
# voice_gateway/tests/test_protocol.py
import gzip
import json
import pytest
from protocol import (
    generate_header, build_client_frame, parse_server_frame,
    CLIENT_FULL_REQUEST, CLIENT_AUDIO_ONLY_REQUEST,
    SERVER_FULL_RESPONSE, SERVER_ACK, SERVER_ERROR_RESPONSE,
    JSON_SERIAL, NO_SERIALIZATION, GZIP_COMPRESSION, MSG_WITH_EVENT,
    EVENT_START_CONNECTION, EVENT_START_SESSION, EVENT_TASK_REQUEST,
)


def test_build_start_connection_frame_has_no_session_id():
    """Event 1 (StartConnection) should NOT include session_id fields."""
    frame = build_client_frame(EVENT_START_CONNECTION, session_id=None, payload={})
    # Header(4) + event_id(4) + payload_size(4) + gzipped_payload
    assert frame[0] == 0x11  # version=1, header_size=1
    assert (frame[1] >> 4) == CLIENT_FULL_REQUEST
    assert int.from_bytes(frame[4:8], "big") == 1  # event_id
    # Next 4 bytes should be payload_size (no session_id_len before it)
    payload_size = int.from_bytes(frame[8:12], "big")
    payload = gzip.decompress(frame[12:12 + payload_size])
    assert json.loads(payload) == {}


def test_build_start_session_frame_has_session_id():
    """Event 100 (StartSession) should include session_id fields."""
    sid = "test-session-abc"
    config = {"tts": {"speaker": "test"}}
    frame = build_client_frame(EVENT_START_SESSION, session_id=sid, payload=config)
    assert int.from_bytes(frame[4:8], "big") == 100
    sid_len = int.from_bytes(frame[8:12], "big")
    assert sid_len == len(sid)
    assert frame[12:12 + sid_len].decode() == sid
    offset = 12 + sid_len
    payload_size = int.from_bytes(frame[offset:offset + 4], "big")
    payload = gzip.decompress(frame[offset + 4:offset + 4 + payload_size])
    assert json.loads(payload) == config


def test_build_audio_frame_uses_audio_only_request():
    """Event 200 (TaskRequest) should use CLIENT_AUDIO_ONLY_REQUEST + NO_SERIALIZATION + GZIP."""
    audio = b"\x00\x01\x02\x03"
    frame = build_client_frame(EVENT_TASK_REQUEST, session_id="sid", payload=audio, is_audio=True)
    assert (frame[1] >> 4) == CLIENT_AUDIO_ONLY_REQUEST
    assert (frame[2] >> 4) == NO_SERIALIZATION
    assert (frame[2] & 0x0f) == GZIP_COMPRESSION


def _build_mock_server_frame(msg_type, event_id, session_id, payload_obj, is_audio=False):
    """Helper to construct a server frame for testing parse_server_frame."""
    buf = bytearray()
    buf.append(0x11)  # version=1, header_size=1
    buf.append((msg_type << 4) | MSG_WITH_EVENT)
    if is_audio:
        buf.append((NO_SERIALIZATION << 4) | GZIP_COMPRESSION)
    else:
        buf.append((JSON_SERIAL << 4) | GZIP_COMPRESSION)
    buf.append(0x00)
    buf.extend(event_id.to_bytes(4, "big"))
    sid_bytes = session_id.encode()
    buf.extend(len(sid_bytes).to_bytes(4, "big"))
    buf.extend(sid_bytes)
    if is_audio:
        compressed = gzip.compress(payload_obj)
    else:
        compressed = gzip.compress(json.dumps(payload_obj).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)
    return bytes(buf)


def test_parse_server_full_response():
    data = _build_mock_server_frame(SERVER_FULL_RESPONSE, 451, "s1",
                                     {"results": [{"text": "hello", "is_interim": True}]})
    result = parse_server_frame(data)
    assert result["message_type"] == "SERVER_FULL_RESPONSE"
    assert result["event"] == 451
    assert result["payload_msg"]["results"][0]["text"] == "hello"


def test_parse_server_ack_audio():
    audio = b"\x10\x20\x30\x40"
    data = _build_mock_server_frame(SERVER_ACK, 352, "s1", audio, is_audio=True)
    result = parse_server_frame(data)
    assert result["message_type"] == "SERVER_ACK"
    assert result["event"] == 352
    assert isinstance(result["payload_msg"], bytes)


def test_parse_server_error():
    buf = bytearray()
    buf.append(0x11)
    buf.append((SERVER_ERROR_RESPONSE << 4) | 0)
    buf.append((JSON_SERIAL << 4) | GZIP_COMPRESSION)
    buf.append(0x00)
    buf.extend((1001).to_bytes(4, "big"))
    compressed = gzip.compress(json.dumps({"error": "bad"}).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)
    result = parse_server_frame(bytes(buf))
    assert result["message_type"] == "SERVER_ERROR"
    assert result["code"] == 1001
    assert result["payload_msg"]["error"] == "bad"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run pytest tests/test_protocol.py -v
```

Expected: FAIL — `protocol` module does not exist.

- [ ] **Step 3: Implement protocol.py**

```python
# voice_gateway/protocol.py
"""
Doubao E2E binary protocol encode/decode.
Adapted from realtime_dialog_example/python3.7/protocol.py with bug fixes.
"""
import gzip
import json
from typing import Any

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message types
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Flags
NO_SEQUENCE = 0b0000
NEG_SEQUENCE = 0b0010
MSG_WITH_EVENT = 0b0100

# Serialization
NO_SERIALIZATION = 0b0000
JSON_SERIAL = 0b0001

# Compression
NO_COMPRESSION = 0b0000
GZIP_COMPRESSION = 0b0001

# Client event IDs
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200
EVENT_SAY_HELLO = 300
EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CHAT_RAG_TEXT = 502
EVENT_CLIENT_INTERRUPT = 515

# Server event IDs
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_USAGE_RESPONSE = 154
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559

# Connection-level events (no session_id in frame)
CONNECTION_EVENTS = {EVENT_START_CONNECTION, EVENT_FINISH_CONNECTION}


def generate_header(
    message_type=CLIENT_FULL_REQUEST,
    serial_method=JSON_SERIAL,
    compression_type=GZIP_COMPRESSION,
) -> bytearray:
    header = bytearray(4)
    header[0] = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    header[1] = (message_type << 4) | MSG_WITH_EVENT
    header[2] = (serial_method << 4) | compression_type
    header[3] = 0x00
    return header


def build_client_frame(
    event_id: int,
    session_id: str | None = None,
    payload: Any = None,
    is_audio: bool = False,
) -> bytes:
    msg_type = CLIENT_AUDIO_ONLY_REQUEST if is_audio else CLIENT_FULL_REQUEST
    serial = NO_SERIALIZATION if is_audio else JSON_SERIAL

    buf = bytearray(generate_header(msg_type, serial, GZIP_COMPRESSION))

    # Event ID (4 bytes big-endian)
    buf.extend(event_id.to_bytes(4, "big"))

    # Session ID (only for session-level events)
    if event_id not in CONNECTION_EVENTS and session_id is not None:
        sid_bytes = session_id.encode()
        buf.extend(len(sid_bytes).to_bytes(4, "big"))
        buf.extend(sid_bytes)

    # Payload — always gzip compressed
    if is_audio and isinstance(payload, (bytes, bytearray)):
        compressed = gzip.compress(payload)
    else:
        compressed = gzip.compress(json.dumps(payload or {}).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)

    return bytes(buf)


def parse_server_frame(data: bytes) -> dict:
    if len(data) < 4:
        return {}

    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F

    # Start reading after header (including any extensions)
    cursor = header_size * 4
    result: dict[str, Any] = {}

    if message_type in (SERVER_FULL_RESPONSE, SERVER_ACK):
        result["message_type"] = "SERVER_ACK" if message_type == SERVER_ACK else "SERVER_FULL_RESPONSE"

        # FIXED: use advancing cursor (official code has bug here)
        if flags & NEG_SEQUENCE:
            result["seq"] = int.from_bytes(data[cursor:cursor + 4], "big")
            cursor += 4
        if flags & MSG_WITH_EVENT:
            result["event"] = int.from_bytes(data[cursor:cursor + 4], "big")
            cursor += 4

        # Session ID
        sid_len = int.from_bytes(data[cursor:cursor + 4], "big", signed=True)
        cursor += 4
        if sid_len > 0:
            result["session_id"] = data[cursor:cursor + sid_len].decode()
            cursor += sid_len

        # Payload
        payload_size = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_msg = data[cursor:cursor + payload_size]

        if compression == GZIP_COMPRESSION and payload_size > 0:
            payload_msg = gzip.decompress(payload_msg)
        if serialization == JSON_SERIAL and payload_size > 0:
            payload_msg = json.loads(payload_msg.decode("utf-8"))

        result["payload_msg"] = payload_msg
        result["payload_size"] = payload_size

    elif message_type == SERVER_ERROR_RESPONSE:
        result["message_type"] = "SERVER_ERROR"
        result["code"] = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_size = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_msg = data[cursor:cursor + payload_size]

        if compression == GZIP_COMPRESSION and payload_size > 0:
            payload_msg = gzip.decompress(payload_msg)
        if serialization == JSON_SERIAL and payload_size > 0:
            payload_msg = json.loads(payload_msg.decode("utf-8"))

        result["payload_msg"] = payload_msg
        result["payload_size"] = payload_size

    return result
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run pytest tests/test_protocol.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/protocol.py voice_gateway/tests/
git commit -m "feat(gateway): doubao binary protocol encode/decode with tests"
```

---

### Task 3: Doubao Client — doubao_client.py

Wraps the protocol into a high-level async client.

**Files:**
- Create: `voice_gateway/doubao_client.py`

- [ ] **Step 1: Create doubao_client.py**

```python
# voice_gateway/doubao_client.py
"""High-level async client for doubao E2E realtime voice API."""
import json
import logging
from typing import AsyncGenerator

import websockets

from config import DOUBAO_WS_URL, get_ws_headers
from protocol import (
    build_client_frame, parse_server_frame,
    EVENT_START_CONNECTION, EVENT_FINISH_CONNECTION,
    EVENT_START_SESSION, EVENT_FINISH_SESSION,
    EVENT_TASK_REQUEST, EVENT_SAY_HELLO,
    EVENT_CHAT_TTS_TEXT, EVENT_CHAT_RAG_TEXT,
)

log = logging.getLogger(__name__)


class DoubaoClient:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.ws = None
        self.logid = ""

    async def connect(self) -> str:
        headers = get_ws_headers()
        self.ws = await websockets.connect(
            DOUBAO_WS_URL,
            extra_headers=headers,
            ping_interval=None,
        )
        self.logid = self.ws.response_headers.get("X-Tt-Logid", "")
        log.info(f"Connected to doubao, logid={self.logid}")
        return self.logid

    async def send_start_connection(self) -> None:
        frame = build_client_frame(EVENT_START_CONNECTION, payload={})
        await self.ws.send(frame)

    async def send_start_session(self, config: dict) -> None:
        frame = build_client_frame(EVENT_START_SESSION, self.session_id, config)
        await self.ws.send(frame)

    async def send_say_hello(self, text: str) -> None:
        frame = build_client_frame(EVENT_SAY_HELLO, self.session_id, {"content": text})
        await self.ws.send(frame)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        frame = build_client_frame(EVENT_TASK_REQUEST, self.session_id, pcm_bytes, is_audio=True)
        await self.ws.send(frame)

    async def send_chat_tts_text(self, content: str, start: bool = True, end: bool = True) -> None:
        frame = build_client_frame(EVENT_CHAT_TTS_TEXT, self.session_id, {
            "start": start, "content": content, "end": end,
        })
        await self.ws.send(frame)

    async def send_chat_rag_text(self, external_rag: str) -> None:
        frame = build_client_frame(EVENT_CHAT_RAG_TEXT, self.session_id, {
            "external_rag": external_rag,
        })
        await self.ws.send(frame)

    async def send_finish_session(self) -> None:
        frame = build_client_frame(EVENT_FINISH_SESSION, self.session_id, {})
        await self.ws.send(frame)

    async def send_finish_connection(self) -> None:
        frame = build_client_frame(EVENT_FINISH_CONNECTION, payload={})
        await self.ws.send(frame)

    async def receive(self) -> AsyncGenerator[dict, None]:
        async for message in self.ws:
            if isinstance(message, bytes):
                parsed = parse_server_frame(message)
                if parsed:
                    yield parsed
            # Ignore text messages

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("Doubao WS closed")
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/doubao_client.py
git commit -m "feat(gateway): DoubaoClient async WS wrapper"
```

---

### Task 4: Business Query — query.py

Hardcoded product catalog with simple text matching.

**Files:**
- Create: `voice_gateway/query.py`
- Create: `voice_gateway/tests/test_query.py`

- [ ] **Step 1: Write tests**

```python
# voice_gateway/tests/test_query.py
import json
import pytest
from query import query


@pytest.mark.asyncio
async def test_query_match_product1():
    result = await query("商品1多少钱")
    assert result is not None
    items = json.loads(result)
    assert len(items) == 1
    assert items[0]["title"] == "商品1"
    assert "100元" in items[0]["content"]


@pytest.mark.asyncio
async def test_query_match_product3():
    result = await query("我想了解一下商品3")
    assert result is not None
    items = json.loads(result)
    assert items[0]["title"] == "商品3"


@pytest.mark.asyncio
async def test_query_no_match():
    result = await query("今天天气怎么样")
    assert result is None


@pytest.mark.asyncio
async def test_query_multiple_match():
    result = await query("商品1和商品2都要")
    assert result is not None
    items = json.loads(result)
    assert len(items) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run pytest tests/test_query.py -v
```

Expected: FAIL — `query` module does not exist.

- [ ] **Step 3: Implement query.py**

```python
# voice_gateway/query.py
"""Business query module. Hardcoded product catalog for MVP."""
import json

PRODUCTS = {
    "商品1": {"title": "商品1", "content": "价格：100元，库存充足"},
    "商品2": {"title": "商品2", "content": "价格：200元，库存充足"},
    "商品3": {"title": "商品3", "content": "价格：300元，限时优惠"},
    "商品4": {"title": "商品4", "content": "价格：400元，需要预订"},
    "商品5": {"title": "商品5", "content": "价格：500元，新品上市"},
}


async def query(text: str) -> str | None:
    """Search for product mentions in text. Returns JSON array string or None."""
    matches = []
    for name, info in PRODUCTS.items():
        if name in text:
            matches.append(info)
    if matches:
        return json.dumps(matches, ensure_ascii=False)
    return None
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run pytest tests/test_query.py -v
```

Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/query.py voice_gateway/tests/test_query.py
git commit -m "feat(gateway): hardcoded product query with tests"
```

---

### Task 5: Session State Machine — session.py

The core orchestrator. Manages doubao lifecycle, event dispatch, and query flow.

Architecture note: Uses a **single doubao consumer** pattern. All doubao messages flow through one `_read_doubao` loop which dispatches events and resolves waiters via `asyncio.Event`. This avoids the bug where multiple consumers of the same async generator would drop messages.

**Files:**
- Create: `voice_gateway/session.py`

- [ ] **Step 1: Create session.py**

```python
# voice_gateway/session.py
"""Voice call session state machine."""
import asyncio
import json
import logging
import uuid
from typing import Any

import aiohttp

from config import START_SESSION_CONFIG, GREETING_TEXT, COMFORT_TEXT
from doubao_client import DoubaoClient
from protocol import (
    EVENT_CONNECTION_STARTED, EVENT_CONNECTION_FAILED,
    EVENT_SESSION_STARTED, EVENT_SESSION_FAILED, EVENT_SESSION_FINISHED,
    EVENT_CONNECTION_FINISHED,
    EVENT_TTS_SENTENCE_START, EVENT_TTS_SENTENCE_END,
    EVENT_TTS_RESPONSE, EVENT_TTS_ENDED,
    EVENT_USAGE_RESPONSE,
    EVENT_ASR_INFO, EVENT_ASR_RESPONSE, EVENT_ASR_ENDED,
    EVENT_CHAT_RESPONSE, EVENT_CHAT_ENDED,
)
from query import query

log = logging.getLogger(__name__)


class Session:
    def __init__(self, browser_ws: aiohttp.web.WebSocketResponse):
        self.browser_ws = browser_ws
        self.session_id = str(uuid.uuid4())
        self.doubao = DoubaoClient(self.session_id)
        self.state = "idle"
        self.is_user_querying = False
        self.is_sending_custom_tts = False  # True while ChatTTSText/RAG flow active
        self.asr_text = ""
        self.bot_text_accumulator = ""
        self._query_task: asyncio.Task | None = None
        self._doubao_task: asyncio.Task | None = None
        # Single-consumer event waiting: _event_waiters maps event_id → asyncio.Event
        self._event_waiters: dict[int, asyncio.Event] = {}
        self._last_waited_frame: dict = {}

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
        """Run the full call lifecycle. Called from server.py after browser sends 'start'."""
        try:
            # Start the single doubao reader — it runs for the entire session
            await self.doubao.connect()
            self._doubao_task = asyncio.create_task(self._read_doubao())

            await self._connect_and_greet()
            if self.state == "talking":
                await self._talking_loop()
        except Exception as e:
            log.exception("Session error")
            await self.send_error(str(e))
        finally:
            await self._cleanup()

    # --- Internal flows ---

    async def _connect_and_greet(self) -> None:
        await self.send_state("connecting")

        await self.doubao.send_start_connection()
        frame = await self._wait_for_event(EVENT_CONNECTION_STARTED,
                                            error_event=EVENT_CONNECTION_FAILED, timeout=10.0)
        if frame.get("event") == EVENT_CONNECTION_FAILED:
            raise ConnectionError(f"ConnectionFailed: {frame.get('payload_msg')}")

        await self.doubao.send_start_session(START_SESSION_CONFIG)
        frame = await self._wait_for_event(EVENT_SESSION_STARTED,
                                            error_event=EVENT_SESSION_FAILED, timeout=10.0)
        if frame.get("event") == EVENT_SESSION_FAILED:
            raise ConnectionError(f"SessionFailed: {frame.get('payload_msg')}")

        await self.send_state("greeting")
        await self.doubao.send_say_hello(GREETING_TEXT)

        # Wait for greeting TTS to finish
        await self._wait_for_event(EVENT_TTS_ENDED, timeout=30.0)
        await self.send_state("talking")

    async def _talking_loop(self) -> None:
        """Read browser messages: binary audio or JSON control."""
        async for msg in self.browser_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self.doubao.send_audio(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "stop":
                    await self._stop()
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    async def _stop(self) -> None:
        """Graceful stop."""
        await self.send_state("ending")
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        try:
            await self.doubao.send_finish_session()
            await self._wait_for_event(EVENT_SESSION_FINISHED, timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            await self.doubao.send_finish_connection()
            await self._wait_for_event(EVENT_CONNECTION_FINISHED, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        await self.send_state("idle")

    # --- Single doubao consumer ---

    async def _read_doubao(self) -> None:
        """Single consumer of all doubao messages. Dispatches events and resolves waiters."""
        try:
            async for frame in self.doubao.receive():
                event = frame.get("event")

                # Check if any waiter is waiting for this event
                if event in self._event_waiters:
                    self._last_waited_frame = frame
                    self._event_waiters[event].set()
                    # Still dispatch the event for side effects (e.g., TTS audio during greeting)

                await self._dispatch_doubao_event(frame)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Doubao read error: {e}")

    async def _wait_for_event(self, target_event: int, timeout: float = 10.0,
                               error_event: int | None = None) -> dict:
        """Wait for a specific event from doubao. Uses asyncio.Event resolved by _read_doubao."""
        evt = asyncio.Event()
        self._event_waiters[target_event] = evt
        if error_event:
            self._event_waiters[error_event] = evt

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        finally:
            self._event_waiters.pop(target_event, None)
            if error_event:
                self._event_waiters.pop(error_event, None)

        return self._last_waited_frame

    # --- Event dispatch ---

    async def _dispatch_doubao_event(self, frame: dict) -> None:
        event = frame.get("event")
        payload = frame.get("payload_msg")
        msg_type = frame.get("message_type")

        # TTS audio → forward to browser (suppress during custom TTS replacement)
        if event == EVENT_TTS_RESPONSE and isinstance(payload, bytes):
            if not self.is_sending_custom_tts:
                await self.browser_ws.send_bytes(payload)
            return

        # TTS sentence start → track tts_type, forward text for chat_tts_text/external_rag
        if event == EVENT_TTS_SENTENCE_START and isinstance(payload, dict):
            tts_type = payload.get("tts_type", "")
            if tts_type in ("chat_tts_text", "external_rag"):
                # Custom TTS is now playing — stop suppressing audio
                self.is_sending_custom_tts = False
                if payload.get("text"):
                    await self.send_transcript("bot", payload["text"], False)
            return

        # ASR text
        if event == EVENT_ASR_RESPONSE and isinstance(payload, dict):
            results = payload.get("results", [])
            if results:
                last = results[-1]
                text = last.get("text", "")
                is_interim = last.get("is_interim", True)
                if not is_interim:
                    self.asr_text = text
                await self.send_transcript("user", text, is_interim)
            return

        # ASR info → user interruption: signal browser to clear playback
        if event == EVENT_ASR_INFO:
            self.is_user_querying = True
            self.is_sending_custom_tts = False
            if self._query_task and not self._query_task.done():
                self._query_task.cancel()
            # Tell browser to clear audio queue
            await self.browser_ws.send_json({"type": "clear_audio"})
            return

        # ASR ended → trigger query
        if event == EVENT_ASR_ENDED:
            self.is_user_querying = False
            if self.asr_text:
                self._query_task = asyncio.create_task(self._run_query(self.asr_text))
                self.asr_text = ""
            return

        # ChatResponse → accumulate bot text
        if event == EVENT_CHAT_RESPONSE and isinstance(payload, dict):
            token = payload.get("content", "")
            if token:
                self.bot_text_accumulator += token
                await self.send_transcript("bot", self.bot_text_accumulator, True)
            return

        # ChatEnded → finalize bot text
        if event == EVENT_CHAT_ENDED:
            if self.bot_text_accumulator:
                await self.send_transcript("bot", self.bot_text_accumulator, False)
            self.bot_text_accumulator = ""
            return

        # Error events
        if event in (EVENT_CONNECTION_FAILED, EVENT_SESSION_FAILED):
            error_msg = payload.get("error", f"Event {event} failed") if isinstance(payload, dict) else str(payload)
            await self.send_error(error_msg)
            return

        if msg_type == "SERVER_ERROR":
            await self.send_error(f"Server error {frame.get('code')}: {payload}")
            return

    # --- Query orchestration ---

    async def _run_query(self, text: str) -> None:
        """Comfort text + RAG injection. Per best practice Scene 3."""
        try:
            result = await query(text)
            if result is None or self.is_user_querying:
                return

            # Suppress default LLM TTS audio while we send custom content
            self.is_sending_custom_tts = True

            # Send comfort text (two packets per official example)
            await self.doubao.send_chat_tts_text(COMFORT_TEXT, start=True, end=False)
            await self.doubao.send_chat_tts_text("", start=False, end=True)

            if self.is_user_querying:
                # User interrupted during comfort — skip RAG
                self.is_sending_custom_tts = False
                return

            # Inject RAG
            await self.doubao.send_chat_rag_text(result)
            log.info(f"RAG injected for: {text}")

        except asyncio.CancelledError:
            self.is_sending_custom_tts = False
            log.info("Query cancelled (user interrupted)")
        except Exception as e:
            self.is_sending_custom_tts = False
            log.error(f"Query error: {e}")

    async def _cleanup(self) -> None:
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        if self._doubao_task and not self._doubao_task.done():
            self._doubao_task.cancel()
            try:
                await self._doubao_task
            except asyncio.CancelledError:
                pass
        try:
            await self.doubao.close()
        except Exception:
            pass
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/session.py
git commit -m "feat(gateway): session state machine with query orchestration"
```

---

### Task 6: HTTP Server — server.py

The aiohttp entry point.

**Files:**
- Create: `voice_gateway/server.py`

- [ ] **Step 1: Create server.py**

```python
# voice_gateway/server.py
"""Voice gateway aiohttp server."""
import json
import logging
import os

import aiohttp.web

from session import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


async def ws_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    log.info("Browser connected")

    # Wait for the "start" message, but don't consume any other messages.
    # The session's _talking_loop will take over reading from this WS.
    msg = await ws.receive()
    if msg.type == aiohttp.WSMsgType.TEXT:
        data = json.loads(msg.data)
        if data.get("type") == "start":
            session = Session(ws)
            await session.run()

    log.info("Browser disconnected")
    return ws


def main():
    # Load .env.local from voice-web directory
    env_path = os.path.join(os.path.dirname(__file__), "..", "voice-web", ".env.local")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    port = int(os.environ.get("GATEWAY_PORT", "8089"))
    app = aiohttp.web.Application()
    app.router.add_get("/ws", ws_handler)

    log.info(f"Starting voice gateway on :{port}")
    aiohttp.web.run_app(app, port=port, print=None)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test server starts**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && timeout 5 uv run python server.py 2>&1 || true
```

Expected: `Starting voice gateway on :8089` then timeout kills it.

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/server.py
git commit -m "feat(gateway): aiohttp server with /ws endpoint"
```

---

### Task 7: Frontend — Replace VoiceSession with VoiceClient

Simplify the browser to a thin client.

**Files:**
- Create: `voice-web/src/lib/voice-client.ts`
- Modify: `voice-web/src/app/page.tsx`
- Modify: `voice-web/server.ts`
- Delete: `voice-web/src/lib/doubao-protocol.ts`
- Delete: `voice-web/src/lib/voice-session.ts`
- Delete: `voice-web/__tests__/doubao-protocol.test.ts`

- [ ] **Step 1: Create voice-client.ts**

```typescript
// voice-web/src/lib/voice-client.ts
import { startCapture, stopCapture } from './audio-capture';
import { createPlayer, enqueue, clearPlayback, closePlayer } from './audio-playback';

export type OnStateCallback = (state: string) => void;
export type OnTranscriptCallback = (role: 'user' | 'bot', text: string, interim: boolean) => void;
export type OnErrorCallback = (message: string) => void;

export class VoiceClient {
  private ws: WebSocket | null = null;
  private currentState: string = 'idle';

  public onState: OnStateCallback = () => {};
  public onTranscript: OnTranscriptCallback = () => {};
  public onError: OnErrorCallback = () => {};

  start(): void {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    this.ws = new WebSocket(wsUrl);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => {
      this.ws!.send(JSON.stringify({ type: 'start' }));
    };

    this.ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        // TTS audio → play
        enqueue(new Uint8Array(event.data));
      } else {
        const msg = JSON.parse(event.data);
        if (msg.type === 'state') {
          this.currentState = msg.state;
          this.onState(msg.state);
          this._handleStateChange(msg.state);
        } else if (msg.type === 'transcript') {
          this.onTranscript(msg.role, msg.text, msg.interim);
        } else if (msg.type === 'clear_audio') {
          clearPlayback();
        } else if (msg.type === 'error') {
          this.onError(msg.message);
        }
      }
    };

    this.ws.onclose = () => {
      if (this.currentState !== 'idle' && this.currentState !== 'ending') {
        this.onError('Connection lost');
      }
      this._stopAudio();
    };

    this.ws.onerror = () => {
      this.onError('WebSocket error');
    };
  }

  stop(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'stop' }));
    }
  }

  private _handleStateChange(state: string): void {
    if (state === 'talking') {
      createPlayer();
      startCapture((chunk: Uint8Array) => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN && this.currentState === 'talking') {
          this.ws.send(chunk.buffer);
        }
      });
    } else if (state === 'greeting') {
      createPlayer();
    } else if (state === 'idle' || state === 'ending' || state === 'error') {
      this._stopAudio();
    }
  }

  private _stopAudio(): void {
    stopCapture();
    closePlayer();
  }
}
```

- [ ] **Step 2: Update page.tsx**

Replace the entire `page.tsx` content:

```tsx
// voice-web/src/app/page.tsx
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { VoiceClient } from '@/lib/voice-client';

interface TranscriptEntry {
  role: 'user' | 'bot';
  text: string;
  isInterim: boolean;
  timestamp: number;
}

export default function Home() {
  const [state, setState] = useState('idle');
  const [transcripts, setTranscripts] = useState<TranscriptEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const clientRef = useRef<VoiceClient | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [transcripts]);

  const handleTranscript = useCallback((role: 'user' | 'bot', text: string, interim: boolean) => {
    setTranscripts((prev) => {
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].role === role) {
          if (prev[i].isInterim) {
            const updated = [...prev];
            updated[i] = { role, text, isInterim: interim, timestamp: Date.now() };
            return updated;
          }
          break;
        }
      }
      return [...prev, { role, text, isInterim: interim, timestamp: Date.now() }];
    });
  }, []);

  const handleStart = useCallback(() => {
    setError(null);
    setTranscripts([]);
    const client = new VoiceClient();
    clientRef.current = client;

    client.onState = (s) => setState(s);
    client.onTranscript = handleTranscript;
    client.onError = (msg) => setError(msg);

    client.start();
  }, [handleTranscript]);

  const handleStop = useCallback(() => {
    clientRef.current?.stop();
    clientRef.current = null;
  }, []);

  const isActive = state === 'connecting' || state === 'greeting' || state === 'talking' || state === 'ending';

  return (
    <div className="flex flex-col h-screen bg-zinc-50 dark:bg-zinc-950">
      <header className="flex items-center justify-center py-4 border-b border-zinc-200 dark:border-zinc-800">
        <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">Voice Test</h1>
      </header>

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
        {state === 'connecting' && <div className="text-center text-sm text-zinc-400">Connecting...</div>}
        {state === 'greeting' && <div className="text-center text-sm text-zinc-400">Bot is greeting...</div>}
      </div>

      {error && (
        <div className="mx-4 mb-2 px-4 py-2 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 text-sm rounded-lg">
          {error}
        </div>
      )}

      <div className="flex items-center justify-center py-6 border-t border-zinc-200 dark:border-zinc-800">
        {!isActive ? (
          <button onClick={handleStart} className="px-8 py-3 bg-green-500 hover:bg-green-600 text-white font-medium rounded-full transition-colors">
            Start Call
          </button>
        ) : (
          <button onClick={handleStop} disabled={state === 'ending'} className="px-8 py-3 bg-red-500 hover:bg-red-600 disabled:bg-zinc-400 text-white font-medium rounded-full transition-colors">
            {state === 'ending' ? 'Ending...' : 'End Call'}
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Simplify server.ts — remove WS proxy**

Replace `voice-web/server.ts` with:

```typescript
// voice-web/server.ts
import { createServer } from 'node:http';
import { parse } from 'node:url';
import next from 'next';

const dev = process.env.NODE_ENV !== 'production';
const port = parseInt(process.env.PORT || '13036', 10);

const app = next({ dev, hostname: 'localhost', port });
const handle = app.getRequestHandler();

app.prepare().then(() => {
  const server = createServer((req, res) => {
    const parsedUrl = parse(req.url!, true);
    handle(req, res, parsedUrl);
  });

  server.listen(port, () => {
    console.log(`> Next.js ready on http://localhost:${port}`);
  });

  const shutdown = () => {
    console.log('\n[server] Shutting down...');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 3000);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
});
```

- [ ] **Step 4: Delete old files**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web
rm -f src/lib/doubao-protocol.ts src/lib/voice-session.ts __tests__/doubao-protocol.test.ts
```

- [ ] **Step 5: Remove ws dependency**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web && pnpm remove ws @types/ws
```

- [ ] **Step 6: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice-web/src/lib/voice-client.ts voice-web/src/app/page.tsx voice-web/server.ts voice-web/package.json voice-web/pnpm-lock.yaml
git rm voice-web/src/lib/doubao-protocol.ts voice-web/src/lib/voice-session.ts voice-web/__tests__/doubao-protocol.test.ts
git commit -m "refactor(web): replace VoiceSession with thin VoiceClient, remove WS proxy"
```

---

### Task 8: Cloudflare Tunnel + Makefile Update

Update routing and service management.

**Files:**
- Modify: `~/.cloudflared/config.yml`
- Modify: `voice-web/Makefile`

- [ ] **Step 1: Update Cloudflare tunnel config**

Replace `~/.cloudflared/config.yml` content:

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

- [ ] **Step 2: Update Makefile**

Replace `voice-web/Makefile`:

```makefile
PIDFILE_DEV = .pids/dev.pid
PIDFILE_TUNNEL = .pids/tunnel.pid
PIDFILE_GATEWAY = .pids/gateway.pid
GATEWAY_DIR = ../voice_gateway

.PHONY: start stop restart dev tunnel gateway logs

start: dev gateway tunnel
	@echo "All services started"

stop:
	@echo "Stopping services..."
	@if [ -f $(PIDFILE_GATEWAY) ]; then \
		kill $$(cat $(PIDFILE_GATEWAY)) 2>/dev/null; \
		rm -f $(PIDFILE_GATEWAY); \
		echo "  gateway stopped"; \
	fi
	@if [ -f $(PIDFILE_DEV) ]; then \
		kill $$(cat $(PIDFILE_DEV)) 2>/dev/null; \
		rm -f $(PIDFILE_DEV); \
		echo "  dev server stopped"; \
	fi
	@if [ -f $(PIDFILE_TUNNEL) ]; then \
		kill $$(cat $(PIDFILE_TUNNEL)) 2>/dev/null; \
		rm -f $(PIDFILE_TUNNEL); \
		echo "  tunnel stopped"; \
	fi
	@pkill -f "tsx server.ts" 2>/dev/null || true
	@pkill -f "next-server" 2>/dev/null || true
	@pkill -f "voice_gateway/server.py" 2>/dev/null || true
	@sleep 2
	@pkill -9 -f "tsx server.ts" 2>/dev/null || true
	@pkill -9 -f "next-server" 2>/dev/null || true
	@pkill -9 -f "voice_gateway/server.py" 2>/dev/null || true
	@sleep 1
	@rm -rf .next

restart: stop start

dev:
	@mkdir -p .pids
	@echo "Starting Next.js dev server..."
	@pnpm dev > /tmp/voice-web-dev.log 2>&1 & echo $$! > $(PIDFILE_DEV)
	@sleep 8
	@if kill -0 $$(cat $(PIDFILE_DEV)) 2>/dev/null; then \
		echo "  dev server running (PID $$(cat $(PIDFILE_DEV)))"; \
	else \
		echo "  dev server failed, check /tmp/voice-web-dev.log"; \
		rm -f $(PIDFILE_DEV); \
		exit 1; \
	fi

gateway:
	@mkdir -p .pids
	@echo "Starting Python voice gateway..."
	@cd $(GATEWAY_DIR) && uv run python server.py > /tmp/voice-gateway.log 2>&1 & echo $$! > $(CURDIR)/$(PIDFILE_GATEWAY)
	@sleep 3
	@if kill -0 $$(cat $(PIDFILE_GATEWAY)) 2>/dev/null; then \
		echo "  gateway running (PID $$(cat $(PIDFILE_GATEWAY)))"; \
	else \
		echo "  gateway failed, check /tmp/voice-gateway.log"; \
		rm -f $(PIDFILE_GATEWAY); \
		exit 1; \
	fi

tunnel:
	@mkdir -p .pids
	@echo "Starting Cloudflare tunnel..."
	@cloudflared tunnel run > /tmp/voice-web-tunnel.log 2>&1 & echo $$! > $(PIDFILE_TUNNEL)
	@sleep 5
	@if kill -0 $$(cat $(PIDFILE_TUNNEL)) 2>/dev/null; then \
		echo "  tunnel running (PID $$(cat $(PIDFILE_TUNNEL)))"; \
	else \
		echo "  tunnel failed, check /tmp/voice-web-tunnel.log"; \
		rm -f $(PIDFILE_TUNNEL); \
		exit 1; \
	fi

logs:
	@echo "=== Gateway ===" && tail -30 /tmp/voice-gateway.log 2>/dev/null || echo "(no log)"
	@echo ""
	@echo "=== Dev Server ===" && tail -15 /tmp/voice-web-dev.log 2>/dev/null || echo "(no log)"
	@echo ""
	@echo "=== Tunnel ===" && tail -10 /tmp/voice-web-tunnel.log 2>/dev/null || echo "(no log)"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice-web/Makefile
git commit -m "chore: update Makefile for 3-service architecture (gateway + next + tunnel)"
```

Note: `~/.cloudflared/config.yml` is outside the repo, not committed.

---

### Task 9: Integration Test

Manual end-to-end test of the complete flow.

- [ ] **Step 1: Update Cloudflare tunnel config**

Manually update `~/.cloudflared/config.yml` as specified in Task 8 Step 1.

- [ ] **Step 2: Start all services**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web && make start
```

Expected: All three services start (dev, gateway, tunnel).

- [ ] **Step 3: Verify gateway responds**

```bash
curl -s http://localhost:8089/ws
```

Expected: Some response (likely 400 since it expects a WebSocket upgrade).

- [ ] **Step 4: Test via browser**

Open `https://voice.ezagent.chat`:
1. Click "Start Call" → should see "Connecting..." then "Bot is greeting..." then hear greeting
2. Say "你好" → see ASR text, hear bot reply, see bot text
3. Say "商品1多少钱" → hear "稍等，我帮你查一下" then hear RAG-based response about 100元
4. Say "今天天气怎么样" → no RAG match, E2E internal LLM responds naturally
5. Click "End Call" → clean disconnect

- [ ] **Step 5: Check logs**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice-web && make logs
```

Verify:
- Gateway log shows `Browser connected`, event flow, `RAG injected for: 商品1多少钱`
- No errors in any log

- [ ] **Step 6: Commit any fixes**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add -A && git commit -m "fix(gateway): integration test fixes"
```
