# Voice Phase 1 — Standalone LiveKit + Feishu Validation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate that LiveKit voice pipeline (Deepgram STT + FishAudio TTS) and Feishu webapp embedding work end-to-end, independent of the channel-server actor model. Two test milestones: (1) voice echo on internal network 100.64.0.27, (2) Feishu webapp with auth via Cloudflare Tunnel at `voice.ezagent.chat`.

**Architecture:** A standalone LiveKit agent echoes back user speech via TTS (Phase 1a), then connects to a simple reply handler (Phase 1b). No channel-server dependency. Token server with Feishu JSSDK auth runs on port 8089. Web client on port 13036. Cloudflare Tunnel exposes both.

**Tech Stack:** Python 3.11+, livekit-agents / livekit-plugins-deepgram / livekit-plugins-fishaudio / livekit-plugins-silero, React + livekit-client + Feishu JSSDK, httpx, aiohttp, Cloudflare Tunnel

**Branch:** `feature/voice-middleware` (worktree at `/Users/h2oslabs/cc-openclaw-voice`)

---

## File Structure

```
voice/
  __init__.py
  agent.py                       — LiveKit AgentServer + echo agent
  config.py                      — Environment-based config
  token_server.py                — Feishu auth + JSSDK config + LiveKit token

voice_web/
  package.json
  tsconfig.json
  vite.config.ts
  index.html                     — Feishu JSSDK script tag
  src/
    main.tsx
    App.tsx                      — JSSDK config → auth code → token → connect
    components/
      TranscriptPanel.tsx
      VoiceControls.tsx
    hooks/
      useVoiceSession.ts

tests/
  test_token_server.py           — Feishu auth verification tests
```

---

### Task 1: Project Setup + Config (`voice/config.py`)

**Files:**
- Create: `voice/__init__.py`
- Create: `voice/config.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Create voice package**

```python
# voice/__init__.py
"""Standalone voice agent for LiveKit + Feishu validation."""
```

- [ ] **Step 2: Write config module**

```python
# voice/config.py
"""Voice agent configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class VoiceConfig:
    """Configuration for voice agent and token server."""

    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # Deepgram STT
    deepgram_api_key: str

    # FishAudio TTS
    fish_api_key: str
    fish_model_id: str = "default"

    # Feishu app (for JSSDK config + auth code verification)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Network
    host: str = "0.0.0.0"
    token_port: int = 8089
    language: str = "zh"

    @classmethod
    def from_env(cls) -> VoiceConfig:
        """Load from environment. Raises ValueError for missing required vars."""
        required = [
            "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
            "DEEPGRAM_API_KEY", "FISH_API_KEY",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")

        return cls(
            livekit_url=os.environ["LIVEKIT_URL"],
            livekit_api_key=os.environ["LIVEKIT_API_KEY"],
            livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
            deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
            fish_api_key=os.environ["FISH_API_KEY"],
            fish_model_id=os.environ.get("FISH_MODEL_ID", "default"),
            feishu_app_id=os.environ.get("FEISHU_APP_ID", ""),
            feishu_app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
            host=os.environ.get("VOICE_HOST", "0.0.0.0"),
            token_port=int(os.environ.get("TOKEN_PORT", "8089")),
            language=os.environ.get("VOICE_LANGUAGE", "zh"),
        )
```

- [ ] **Step 3: Add dependencies to pyproject.toml**

```toml
[project.optional-dependencies]
voice = [
    "livekit-agents>=1.0",
    "livekit-plugins-deepgram",
    "livekit-plugins-fishaudio",
    "livekit-plugins-silero",
    "httpx",
    "aiohttp",
]
```

- [ ] **Step 4: Install and verify**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv pip install -e ".[voice]"`
Expected: all dependencies resolve

- [ ] **Step 5: Commit**

```bash
git add voice/ pyproject.toml
git commit -m "feat(voice): config module and dependencies for standalone validation"
```

---

### Task 2: Echo Voice Agent (`voice/agent.py`)

**Files:**
- Create: `voice/agent.py`

A minimal LiveKit agent that:
1. Listens to user speech via STT
2. Echoes the transcript back via TTS ("你说了：<transcript>")

This validates the full audio pipeline without any external integration.

- [ ] **Step 1: Implement echo agent**

```python
# voice/agent.py
"""Standalone LiveKit voice agent — echo mode for validation."""
from __future__ import annotations

import logging

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli
from livekit.plugins import deepgram, fishaudio, silero

from voice.config import VoiceConfig

log = logging.getLogger("voice-agent")


class EchoAgent(Agent):
    """Simple echo agent: repeats back what the user says via TTS."""

    def __init__(self) -> None:
        super().__init__(
            instructions="你是一个语音回声测试。用户说什么，你就重复一遍。"
            "回复格式：'你说了：<用户原话>'。保持简短。",
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="用中文跟用户打招呼，说'你好，我是语音测试助手，请说话'",
        )


def create_server(config: VoiceConfig) -> AgentServer:
    """Create and configure the LiveKit AgentServer."""
    server = AgentServer()

    def prewarm(proc: JobProcess) -> None:
        proc.userdata["vad"] = silero.VAD.load()
        proc.userdata["config"] = config

    server.setup_fnc = prewarm

    @server.rtc_session()
    async def entrypoint(ctx: JobContext) -> None:
        cfg = ctx.proc.userdata["config"]

        stt = deepgram.STT(
            api_key=cfg.deepgram_api_key,
            language=cfg.language,
        )
        tts = fishaudio.TTS(
            api_key=cfg.fish_api_key,
            model_id=cfg.fish_model_id,
        )

        session = AgentSession(
            stt=stt,
            tts=tts,
            vad=ctx.proc.userdata["vad"],
        )

        await session.start(agent=EchoAgent(), room=ctx.room)

    return server


def main():
    """CLI entry point."""
    config = VoiceConfig.from_env()
    server = create_server(config)
    cli.run_app(server)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add voice/agent.py
git commit -m "feat(voice): echo agent for LiveKit pipeline validation"
```

---

### Task 3: Token Server with Feishu Auth (`voice/token_server.py`)

**Files:**
- Create: `voice/token_server.py`
- Test: `tests/test_token_server.py`

Two endpoints:
- `POST /api/jssdk-config` — returns JSSDK signature for `h5sdk.config()`
- `POST /api/token` — verifies Feishu auth code, issues LiveKit room token

- [ ] **Step 1: Write tests**

```python
# tests/test_token_server.py
"""Tests for Feishu auth verification in the token server."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from voice.token_server import verify_feishu_auth_code, generate_jssdk_signature


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
async def test_verify_valid_auth_code():
    """Two-step flow succeeds: app_access_token + OIDC exchange."""
    app_resp = _mock_response({"code": 0, "app_access_token": "a-xxx"})
    user_resp = _mock_response({
        "code": 0,
        "data": {"access_token": "u-xxx", "open_id": "ou_abc", "name": "Test"},
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_resp, user_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result["open_id"] == "ou_abc"
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_verify_invalid_auth_code():
    """App token OK, OIDC exchange fails → None."""
    app_resp = _mock_response({"code": 0, "app_access_token": "a-xxx"})
    fail_resp = _mock_response({"code": 10012, "msg": "invalid code"})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_resp, fail_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("bad", "app_id", "secret")
    assert result is None


@pytest.mark.asyncio
async def test_verify_app_token_fails():
    """App token request fails → None (fail closed)."""
    fail_resp = _mock_response({"code": 10003, "msg": "bad app"})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=fail_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result is None


@pytest.mark.asyncio
async def test_verify_network_error():
    """Network error → None."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "app_id", "secret")
    assert result is None


def test_jssdk_signature():
    """Signature matches expected SHA1 format."""
    sig = generate_jssdk_signature("ticket123", "nonce456", "1700000000", "https://voice.ezagent.chat")
    assert len(sig) == 40  # SHA1 hex
    # Deterministic
    assert sig == generate_jssdk_signature("ticket123", "nonce456", "1700000000", "https://voice.ezagent.chat")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/test_token_server.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement token server**

```python
# voice/token_server.py
"""Async token server — Feishu JSSDK config + auth code verification + LiveKit token."""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

import httpx
from aiohttp import web
from livekit.api import AccessToken, VideoGrants

from voice.config import VoiceConfig

log = logging.getLogger(__name__)

FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_OIDC_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_JSAPI_TICKET_URL = "https://open.feishu.cn/open-apis/jssdk/ticket/get"

CORS_ORIGIN = "https://voice.ezagent.chat"


async def get_app_access_token(app_id: str, app_secret: str) -> str | None:
    """Get app_access_token from Feishu."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("app_access_token failed: %s", data.get("msg"))
                return None
            return data["app_access_token"]
    except Exception as e:
        log.warning("get_app_access_token error: %s", e)
        return None


async def get_jsapi_ticket(app_access_token: str) -> str | None:
    """Get jsapi_ticket for JSSDK config signature."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_JSAPI_TICKET_URL,
                headers={"Authorization": f"Bearer {app_access_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("jsapi_ticket failed: %s", data.get("msg"))
                return None
            return data["data"]["ticket"]
    except Exception as e:
        log.warning("get_jsapi_ticket error: %s", e)
        return None


def generate_jssdk_signature(ticket: str, nonce: str, timestamp: str, url: str) -> str:
    """SHA1 signature for h5sdk.config()."""
    sign_str = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(sign_str.encode()).hexdigest()


async def verify_feishu_auth_code(code: str, app_id: str, app_secret: str) -> dict | None:
    """Exchange auth code for user info. Returns {open_id, name, access_token} or None."""
    try:
        app_token = await get_app_access_token(app_id, app_secret)
        if not app_token:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_OIDC_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("auth code verification failed: %s", data.get("msg"))
                return None
            return {
                "open_id": data["data"]["open_id"],
                "name": data["data"].get("name", ""),
                "access_token": data["data"]["access_token"],
            }
    except Exception as e:
        log.warning("verify_feishu_auth_code error: %s", e)
        return None


def create_token_app(config: VoiceConfig) -> web.Application:
    """Create aiohttp app with /api/jssdk-config and /api/token endpoints."""

    async def handle_jssdk_config(request: web.Request) -> web.Response:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return web.json_response({"error": "Missing url"}, status=400,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        app_token = await get_app_access_token(config.feishu_app_id, config.feishu_app_secret)
        if not app_token:
            return web.json_response({"error": "Failed to get app token"}, status=500,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        ticket = await get_jsapi_ticket(app_token)
        if not ticket:
            return web.json_response({"error": "Failed to get jsapi ticket"}, status=500,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        sig = generate_jssdk_signature(ticket, nonce, ts, url)

        return web.json_response(
            {"appId": config.feishu_app_id, "timestamp": ts, "nonceStr": nonce, "signature": sig},
            headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
        )

    async def handle_token(request: web.Request) -> web.Response:
        body = await request.json()
        auth_code = body.get("auth_code", "")

        if not auth_code:
            return web.json_response(
                {"error": "Missing auth_code. Please open from Feishu."},
                status=403, headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
            )

        user_info = await verify_feishu_auth_code(
            auth_code, config.feishu_app_id, config.feishu_app_secret,
        )
        if user_info is None:
            return web.json_response(
                {"error": "Invalid or expired auth code."},
                status=403, headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
            )

        open_id = user_info["open_id"]
        name = user_info.get("name", open_id)
        room = f"voice-{open_id}"

        token = AccessToken(api_key=config.livekit_api_key, api_secret=config.livekit_api_secret)
        token.identity = open_id
        token.name = name
        token.add_grant(VideoGrants(room_join=True, room=room))

        return web.json_response(
            {"token": token.to_jwt(), "room": room, "user": name},
            headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
        )

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(status=200, headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    app = web.Application()
    app.router.add_post("/api/jssdk-config", handle_jssdk_config)
    app.router.add_route("OPTIONS", "/api/jssdk-config", handle_options)
    app.router.add_post("/api/token", handle_token)
    app.router.add_route("OPTIONS", "/api/token", handle_options)
    return app


async def run_token_server(config: VoiceConfig) -> web.AppRunner:
    """Start the token server. Returns runner for lifecycle management."""
    app = create_token_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.token_port)
    await site.start()
    log.info("Token server on %s:%d", config.host, config.token_port)
    return runner
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/test_token_server.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add voice/token_server.py tests/test_token_server.py
git commit -m "feat(voice): async token server with Feishu JSSDK + auth verification"
```

---

### Task 4: Web Voice Client (`voice_web/`)

**Files:**
- Create: `voice_web/package.json`, `voice_web/tsconfig.json`, `voice_web/vite.config.ts`
- Create: `voice_web/index.html`, `voice_web/src/main.tsx`, `voice_web/src/App.tsx`
- Create: `voice_web/src/hooks/useVoiceSession.ts`
- Create: `voice_web/src/components/TranscriptPanel.tsx`, `voice_web/src/components/VoiceControls.tsx`

- [ ] **Step 1: Initialize project structure**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
mkdir -p voice_web/src/components voice_web/src/hooks
```

```json
// voice_web/package.json
{
  "name": "openclaw-voice",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --port 13036 --host",
    "build": "tsc && vite build",
    "preview": "vite preview --port 13036"
  },
  "dependencies": {
    "livekit-client": "^2.0.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0"
  }
}
```

```json
// voice_web/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true
  },
  "include": ["src"]
}
```

```ts
// voice_web/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 13036, host: true },
});
```

- [ ] **Step 2: Create index.html + main.tsx**

```html
<!-- voice_web/index.html -->
<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>OpenClaw Voice</title>
  <script src="https://lf1-cdn-tos.bytegoofy.com/goofy/lark/op/h5-js-sdk-1.5.30/h5-js-sdk-1.5.30.js"></script>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.tsx"></script>
</body>
</html>
```

```tsx
// voice_web/src/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 3: Create useVoiceSession hook**

```tsx
// voice_web/src/hooks/useVoiceSession.ts
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
  }, [livekitUrl, token]);

  const disconnect = useCallback(async () => {
    if (roomRef.current) {
      await roomRef.current.disconnect();
      roomRef.current = null;
    }
  }, []);

  return { connected, transcript, partialText, connect, disconnect };
}
```

- [ ] **Step 4: Create TranscriptPanel**

```tsx
// voice_web/src/components/TranscriptPanel.tsx
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
```

- [ ] **Step 5: Create VoiceControls**

```tsx
// voice_web/src/components/VoiceControls.tsx
import React from "react";

export function VoiceControls({ connected, onConnect, onDisconnect }: {
  connected: boolean; onConnect: () => void; onDisconnect: () => void;
}) {
  return (
    <div style={{ display: "flex", justifyContent: "center", padding: "16px", borderTop: "1px solid #E5E5EA" }}>
      <button
        onClick={connected ? onDisconnect : onConnect}
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
```

- [ ] **Step 6: Create App.tsx with Feishu auth flow**

```tsx
// voice_web/src/App.tsx
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
```

- [ ] **Step 7: Install npm dependencies**

Run: `cd /Users/h2oslabs/cc-openclaw-voice/voice_web && npm install`
Expected: dependencies installed

- [ ] **Step 8: Commit**

```bash
git add voice_web/
git commit -m "feat(voice): web client with Feishu JSSDK auth + LiveKit connection"
```

---

### Task 5: Internal Network Test (100.64.0.27)

**Files:** None (manual testing)

This task validates the audio pipeline on the internal network before involving Cloudflare.

- [ ] **Step 1: Create .env file for local testing**

```bash
# voice/.env (DO NOT COMMIT)
LIVEKIT_URL=ws://100.64.0.27:7880
LIVEKIT_API_KEY=<your-key>
LIVEKIT_API_SECRET=<your-secret>
DEEPGRAM_API_KEY=<your-key>
FISH_API_KEY=<your-key>
VOICE_HOST=0.0.0.0
TOKEN_PORT=8089
```

- [ ] **Step 2: Start LiveKit server (if self-hosting)**

```bash
docker run --rm -p 7880:7880 -p 7881:7881 \
  -e LIVEKIT_KEYS="devkey: secret" \
  livekit/livekit-server
```

Or use LiveKit Cloud and set `LIVEKIT_URL=wss://your-project.livekit.cloud`.

- [ ] **Step 3: Start voice agent**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
source voice/.env
uv run python -m voice.agent dev
```

Expected: agent starts, logs "Worker registered"

- [ ] **Step 4: Start web client (without Feishu auth for local test)**

For internal testing, temporarily bypass Feishu auth by adding a dev mode to App.tsx that accepts a `?dev=1` query param. Or generate a test token manually:

```bash
# Generate a test token for direct browser access
uv run python -c "
from livekit.api import AccessToken, VideoGrants
t = AccessToken(api_key='devkey', api_secret='secret')
t.identity = 'test-user'
t.add_grant(VideoGrants(room_join=True, room='voice-test'))
print(t.to_jwt())
"
```

Start Vite dev server:
```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_web
VITE_LIVEKIT_URL=ws://100.64.0.27:7880 npm run dev
```

- [ ] **Step 5: Test in browser**

Open `http://100.64.0.27:13036?dev=1` (or use the test token).
- Speak into microphone
- Verify STT transcript appears
- Verify TTS echo plays back
- Test barge-in (speak while agent is replying)

- [ ] **Step 6: Document results**

Record what works / what doesn't. Note latencies.

---

### Task 6: Cloudflare Tunnel + Feishu Webapp Test

**Files:** None (infrastructure setup)

- [ ] **Step 1: Install cloudflared**

```bash
brew install cloudflared
cloudflared tunnel login
# Select ezagent.chat zone
```

- [ ] **Step 2: Create tunnel**

```bash
cloudflared tunnel create openclaw-voice
cloudflared tunnel route dns openclaw-voice voice.ezagent.chat
```

- [ ] **Step 3: Write tunnel config**

```yaml
# ~/.cloudflared/config.yml
tunnel: <TUNNEL_ID>
credentials-file: /Users/h2oslabs/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: voice.ezagent.chat
    path: /api/*
    service: http://localhost:8089
  - hostname: voice.ezagent.chat
    service: http://localhost:13036
  - service: http_status:404
```

- [ ] **Step 4: Start all services**

```bash
# Terminal 1: Token server
cd /Users/h2oslabs/cc-openclaw-voice && source voice/.env && uv run python -c "
import asyncio
from voice.config import VoiceConfig
from voice.token_server import run_token_server
async def main():
    config = VoiceConfig.from_env()
    await run_token_server(config)
    await asyncio.Event().wait()
asyncio.run(main())
"

# Terminal 2: Voice agent
cd /Users/h2oslabs/cc-openclaw-voice && source voice/.env && uv run python -m voice.agent dev

# Terminal 3: Web client
cd /Users/h2oslabs/cc-openclaw-voice/voice_web && npm run dev

# Terminal 4: Tunnel
cloudflared tunnel run openclaw-voice
```

- [ ] **Step 5: Configure Feishu webapp**

1. https://open.feishu.cn/app → 选择应用
2. **应用功能** → **网页应用** → 桌面端/移动端主页: `https://voice.ezagent.chat`
3. **安全设置** → 重定向 URL: `https://voice.ezagent.chat`
4. **权限管理** → `contact:user.base:readonly`
5. 发布版本

- [ ] **Step 6: Test from Feishu**

1. 在飞书工作台打开语音应用
2. 验证 JSSDK config 成功（无报错）
3. 验证 auth code 获取成功
4. 点击"开始通话"
5. 说话，验证回声
6. 测试打断

- [ ] **Step 7: Verify security — direct browser access rejected**

Open `https://voice.ezagent.chat` in a regular browser (not Feishu).
Expected: 显示"请从飞书客户端打开此页面"

---

## Summary

| Task | What | Milestone |
|------|------|-----------|
| 1 | Config + dependencies | Project scaffolding |
| 2 | Echo voice agent | LiveKit pipeline works |
| 3 | Token server + Feishu auth | JSSDK + auth code verification |
| 4 | Web voice client | Frontend with Feishu flow |
| 5 | Internal network test | Audio pipeline on 100.64.0.27 |
| 6 | Cloudflare + Feishu test | End-to-end via voice.ezagent.chat |
