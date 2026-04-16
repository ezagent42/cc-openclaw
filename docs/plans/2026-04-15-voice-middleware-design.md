# Voice Middleware — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a voice call interface — embedded as a Feishu webapp (H5) at `https://voice.ezagent.chat` — where users speak to Claude via real-time streaming ASR/TTS, with full-duplex support and barge-in, integrated as a first-class actor in the channel-server actor model.

**Architecture:** A LiveKit-based Voice Pipeline Agent registers as an actor (`voice:<user>`) in the channel-server runtime via the same WebSocket protocol used by CC sessions. The agent pipes browser audio through STT (Deepgram), forwards transcripts to Claude via existing CC actor routing, and streams Claude's text replies back through TTS (FishAudio) to the browser. The web client is embedded in Feishu as a webapp (webview), served locally on port 13036, exposed via Cloudflare Tunnel at `https://voice.ezagent.chat`. Feishu JSSDK provides user identity. Transcripts are synced to Feishu as text messages.

**Tech Stack:** Python 3.11+, livekit-agents / livekit-plugins-deepgram / livekit-plugins-fishaudio (MVP), React + LiveKit React SDK + Feishu JSSDK (web client), Cloudflare Tunnel (cloudflared), asyncio, websockets, httpx

**Branch:** `feature/actor-model` (builds on actor model architecture)

**Feishu Integration:** The voice web client is registered as a Feishu webapp (网页应用) in the developer console, with desktop/mobile homepage set to `https://voice.ezagent.chat`. Users open it from the Feishu sidebar or workbench. The webapp uses Feishu JSSDK to get the current user identity (`tt.requestAuthCode`) and passes it to the token server for LiveKit room assignment.

---

## File Structure

```
channel_server/
  core/
    handler.py                     — ADD VoiceSessionHandler to existing file
  adapters/
    voice/
      __init__.py
      agent.py                     — LiveKit AgentSession + channel bridge
      config.py                    — Voice config (LiveKit URL, API keys, STT/TTS settings)

voice_web/
  package.json
  index.html
  src/
    App.tsx                        — Main voice UI: connect button, transcript panel
    components/
      TranscriptPanel.tsx          — Real-time ASR transcript + conversation history
      VoiceControls.tsx            — Mute, hang up, connection status
    hooks/
      useVoiceSession.ts           — LiveKit room connection + data channel handling

tests/
  channel_server/
    adapters/
      test_voice_agent.py          — Voice agent ↔ channel server integration tests
    core/
      test_handler.py              — ADD VoiceSessionHandler tests
```

---

### Task 1: VoiceSessionHandler (`channel_server/core/handler.py`)

**Files:**
- Modify: `channel_server/core/handler.py` (on `feature/actor-model` branch)
- Test: `tests/channel_server/core/test_handler.py`

The voice actor needs a handler that:
- Forwards external messages (from CC/Feishu actors) as TransportSend to the voice agent
- Routes voice actor's own messages (ASR transcripts) to downstream CC actors
- Handles `transcript_sync` command by forwarding to the Feishu actor for text logging
- Handles `forward` command for routing messages to specific actors

- [ ] **Step 1: Write tests for VoiceSessionHandler**

Add to `tests/channel_server/core/test_handler.py`:

```python
# ---- VoiceSessionHandler tests ----

from channel_server.core.handler import VoiceSessionHandler

class TestVoiceSessionHandler:

    def _make_actor(self, address="voice:alice", downstream=None):
        return Actor(
            address=address,
            tag="voice",
            handler="voice_session",
            downstream=downstream or ["cc:alice.root"],
            transport=Transport(type="websocket", config={"session_id": "vs_123"}),
        )

    def test_external_message_forwards_to_transport(self):
        """Messages from CC/Feishu → push to voice agent via transport."""
        actor = self._make_actor()
        msg = Message(sender="cc:alice.root", type="text", payload={"text": "Hello from Claude"})
        handler = VoiceSessionHandler()
        actions = handler.handle(actor, msg)
        assert len(actions) == 1
        assert isinstance(actions[0], TransportSend)
        assert actions[0].payload["text"] == "Hello from Claude"

    def test_transcript_routes_to_downstream(self):
        """ASR transcript from voice actor → forward to CC actors."""
        actor = self._make_actor()
        msg = Message(
            sender="voice:alice",
            type="text",
            payload={"command": "transcript", "text": "你好"},
        )
        handler = VoiceSessionHandler()
        actions = handler.handle(actor, msg)
        assert len(actions) == 1
        assert isinstance(actions[0], Send)
        assert actions[0].to == "cc:alice.root"
        assert actions[0].message.payload["text"] == "你好"

    def test_transcript_sync_to_feishu(self):
        """transcript_sync command → forward to Feishu actor for text logging."""
        actor = self._make_actor(downstream=["cc:alice.root", "feishu:oc_xxx"])
        msg = Message(
            sender="voice:alice",
            type="text",
            payload={"command": "transcript_sync", "text": "[语音] 你好", "target": "feishu:oc_xxx"},
        )
        handler = VoiceSessionHandler()
        actions = handler.handle(actor, msg)
        assert any(isinstance(a, Send) and a.to == "feishu:oc_xxx" for a in actions)

    def test_forward_routes_to_target(self):
        """forward command → route to specified target actor."""
        actor = self._make_actor()
        msg = Message(
            sender="voice:alice",
            type="text",
            payload={"command": "forward", "target": "feishu:oc_xxx", "text": "[语音] 你好"},
        )
        handler = VoiceSessionHandler()
        actions = handler.handle(actor, msg)
        assert len(actions) == 1
        assert isinstance(actions[0], Send)
        assert actions[0].to == "feishu:oc_xxx"

    def test_no_transport_no_crash(self):
        """External message with no transport → empty actions."""
        actor = self._make_actor()
        actor.transport = None
        msg = Message(sender="cc:alice.root", type="text", payload={"text": "hello"})
        handler = VoiceSessionHandler()
        actions = handler.handle(actor, msg)
        assert actions == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/core/test_handler.py::TestVoiceSessionHandler -v`
Expected: FAIL — `ImportError: cannot import name 'VoiceSessionHandler'`

- [ ] **Step 3: Implement VoiceSessionHandler**

Add to `channel_server/core/handler.py` before the registry:

```python
# ---------------------------------------------------------------------------
# VoiceSessionHandler
# ---------------------------------------------------------------------------

class VoiceSessionHandler:
    """Bridge between actor messages and a voice agent (LiveKit pipeline).

    - External messages (sender != actor.address): push to voice agent via TransportSend (for TTS).
    - Voice actor's own messages: dispatch by command type.
      - transcript: ASR final transcript → forward to downstream CC actors.
      - transcript_sync: transcript text → forward to specific Feishu actor for logging.
      - forward: route message to a specific target actor.
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender != actor.address:
            # External message → forward to voice agent for TTS playback
            if actor.transport is None:
                return []
            return [TransportSend(payload=msg.payload)]

        # Message originated from voice agent — dispatch by command
        command = msg.payload.get("command", "")

        if command == "transcript":
            # ASR transcript → forward to downstream CC actors
            transcript_msg = Message(
                sender=actor.address,
                type="text",
                payload={"text": msg.payload.get("text", "")},
                metadata={"source": "voice"},
            )
            return [Send(to=addr, message=transcript_msg) for addr in actor.downstream]

        if command == "transcript_sync":
            # Sync transcript to a specific Feishu actor for text logging
            target = msg.payload.get("target", "")
            if not target:
                return []
            sync_msg = Message(
                sender=actor.address,
                type="text",
                payload={"text": msg.payload.get("text", "")},
            )
            return [Send(to=target, message=sync_msg)]

        if command == "forward":
            # Forward message to a specific target actor
            target = msg.payload.get("target", "")
            if not target:
                return []
            fwd_msg = Message(
                sender=actor.address,
                type=msg.type,
                payload=msg.payload,
            )
            return [Send(to=target, message=fwd_msg)]

        return []
```

Update the registry:

```python
HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
    "admin": AdminHandler(),
    "voice_session": VoiceSessionHandler(),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/core/test_handler.py::TestVoiceSessionHandler -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/handler.py tests/channel_server/core/test_handler.py
git commit -m "feat(actor): voice session handler for ASR/TTS actor bridge"
```

---

### Task 2: Voice Agent Config (`channel_server/adapters/voice/config.py`)

**Files:**
- Create: `channel_server/adapters/voice/__init__.py`
- Create: `channel_server/adapters/voice/config.py`

- [ ] **Step 1: Create the voice adapter package**

```python
# channel_server/adapters/voice/__init__.py
"""Voice adapter — LiveKit-based voice pipeline for the actor runtime."""
```

- [ ] **Step 2: Write voice config**

```python
# channel_server/adapters/voice/config.py
"""Voice middleware configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class VoiceConfig:
    """Configuration for the voice pipeline agent."""

    # LiveKit server
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # Deepgram STT
    deepgram_api_key: str

    # FishAudio TTS
    fish_api_key: str
    fish_model_id: str = "default"

    # Feishu app credentials (for auth code verification)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Channel server WebSocket (required — voice agent cannot function without it)
    channel_ws_url: str = ""

    # Voice actor settings
    user: str = ""
    language: str = "zh"

    @classmethod
    def from_env(cls) -> VoiceConfig:
        """Load config from environment variables. Raises ValueError for missing required vars."""
        required = [
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "DEEPGRAM_API_KEY",
            "FISH_API_KEY",
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "CHANNEL_WS_URL",
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
            feishu_app_id=os.environ["FEISHU_APP_ID"],
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
            channel_ws_url=os.environ["CHANNEL_WS_URL"],
            user=os.environ.get("OC_USER", ""),
            language=os.environ.get("VOICE_LANGUAGE", "zh"),
        )
```

- [ ] **Step 3: Commit**

```bash
git add channel_server/adapters/voice/
git commit -m "feat(voice): config module for LiveKit + STT/TTS credentials"
```

---

### Task 3: Voice Pipeline Agent (`channel_server/adapters/voice/agent.py`)

**Files:**
- Create: `channel_server/adapters/voice/agent.py`
- Test: `tests/channel_server/adapters/test_voice_agent.py`

This is the core component. Two classes:
- `ChannelBridge`: WebSocket bridge to channel-server actor runtime
- `VoiceAgent`: LiveKit `AgentServer` + `AgentSession` lifecycle, wired to the bridge

Note: The CC adapter's `_route_to_actor` sets `payload["command"] = msg_type`. So `send_transcript`
must send `type: "transcript"` (not `"reply"`) to match VoiceSessionHandler's dispatch. Similarly
`sync_transcript_to_feishu` sends `type: "forward"` which VoiceSessionHandler handles.

The CC adapter must also accept `"transcript"` as a routable message type (see Task 4).

- [ ] **Step 1: Write tests for the channel bridge**

```python
# tests/channel_server/adapters/test_voice_agent.py
"""Tests for the voice agent's channel server bridge (not LiveKit internals)."""
import asyncio
import json
import pytest

from channel_server.adapters.voice.agent import ChannelBridge


class FakeWebSocket:
    """Minimal fake WebSocket for testing."""

    def __init__(self):
        self.sent: list[str] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await asyncio.wait_for(self.incoming.get(), timeout=0.1)
        except asyncio.TimeoutError:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_bridge_register_sends_correct_message():
    """Bridge sends register message with voice actor address."""
    ws = FakeWebSocket()
    bridge = ChannelBridge(ws, user="alice")
    # Simulate registered ack
    ws.incoming.put_nowait(json.dumps({"type": "registered", "address": "voice:alice"}))
    await bridge.register()
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "register"
    assert sent["instance_id"] == "voice:alice"
    assert sent["tag_name"] == "voice"


@pytest.mark.asyncio
async def test_bridge_send_transcript():
    """Bridge sends transcript command — type must be 'transcript' for VoiceSessionHandler dispatch."""
    ws = FakeWebSocket()
    bridge = ChannelBridge(ws, user="alice")
    bridge.registered = True
    await bridge.send_transcript("你好世界")
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "transcript"
    assert sent["text"] == "你好世界"


@pytest.mark.asyncio
async def test_bridge_send_transcript_sync():
    """Bridge sends forward command to log transcript in Feishu."""
    ws = FakeWebSocket()
    bridge = ChannelBridge(ws, user="alice", feishu_target="feishu:oc_xxx")
    bridge.registered = True
    await bridge.sync_transcript_to_feishu("你好世界")
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "forward"
    assert sent["target"] == "feishu:oc_xxx"
    assert "[语音]" in sent["text"]


@pytest.mark.asyncio
async def test_bridge_on_reply_callback():
    """Bridge invokes callback when Claude reply arrives."""
    ws = FakeWebSocket()
    bridge = ChannelBridge(ws, user="alice")
    replies = []
    bridge.on_reply = replies.append

    # Simulate inbound reply from channel server
    ws.incoming.put_nowait(json.dumps({"text": "Claude says hello"}))
    await bridge.receive_one()
    assert len(replies) == 1
    assert replies[0] == "Claude says hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_voice_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'channel_server.adapters.voice'`

- [ ] **Step 3: Implement ChannelBridge and VoiceAgent**

```python
# channel_server/adapters/voice/agent.py
"""Voice pipeline agent — LiveKit AgentSession + channel server bridge."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

from channel_server.adapters.voice.config import VoiceConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ChannelBridge — WebSocket bridge to channel-server actor runtime
# ---------------------------------------------------------------------------

class ChannelBridge:
    """WebSocket bridge between the voice agent and the channel-server actor runtime.

    Registers as voice:<user> actor, sends ASR transcripts,
    and receives Claude's text responses for TTS playback.
    """

    def __init__(self, ws, user: str, feishu_target: str = "") -> None:
        self.ws = ws
        self.user = user
        self.feishu_target = feishu_target
        self.address = f"voice:{user}"
        self.registered = False
        self.on_reply: Callable[[str], None] | None = None

    async def register(self) -> None:
        """Send register message and wait for ack."""
        msg = {
            "type": "register",
            "instance_id": self.address,
            "tag_name": "voice",
        }
        await self.ws.send(json.dumps(msg))
        raw = await self.ws.recv()
        data = json.loads(raw)
        if data.get("type") == "registered":
            self.registered = True
            log.info("Voice agent registered as %s", data.get("address"))

    async def send_transcript(self, text: str) -> None:
        """Send final ASR transcript to channel server.

        Uses type: "transcript" so the CC adapter sets command="transcript"
        in the actor message, matching VoiceSessionHandler dispatch.
        """
        if not self.registered:
            return
        msg = {"type": "transcript", "text": text}
        await self.ws.send(json.dumps(msg))

    async def sync_transcript_to_feishu(self, text: str) -> None:
        """Send transcript to Feishu actor for text logging.

        Uses type: "forward" with target — VoiceSessionHandler routes to the target actor.
        """
        if not self.registered or not self.feishu_target:
            return
        msg = {
            "type": "forward",
            "target": self.feishu_target,
            "text": f"[语音] {text}",
        }
        await self.ws.send(json.dumps(msg))

    async def receive_one(self) -> None:
        """Receive one message from channel server and invoke callback."""
        raw = await self.ws.recv()
        data = json.loads(raw)
        text = data.get("text", "")
        if text and self.on_reply:
            self.on_reply(text)

    async def receive_loop(self) -> None:
        """Continuously receive messages from channel server."""
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                    text = data.get("text", "")
                    if text and self.on_reply:
                        self.on_reply(text)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from channel server")
        except websockets.ConnectionClosed:
            log.info("Channel server connection closed")


# ---------------------------------------------------------------------------
# VoiceAgent — LiveKit AgentServer + AgentSession lifecycle
# ---------------------------------------------------------------------------

class VoiceAgent:
    """LiveKit voice agent that bridges audio ↔ channel server.

    Uses the new livekit-agents API (>= 1.0):
    - AgentServer with @server.rtc_session() entrypoint
    - AgentSession with STT/TTS plugins
    - Agent subclass for instructions/behavior
    - session.on("user_input_transcribed") for ASR events
    - session.say(text) for TTS injection from Claude replies
    """

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.bridge: ChannelBridge | None = None

    def run(self) -> None:
        """Main entry point — sets up LiveKit AgentServer and runs it."""
        from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli
        from livekit.plugins import deepgram, fishaudio, silero

        config = self.config
        bridge_holder: dict[str, ChannelBridge] = {}

        # --- Agent subclass (defines behavior/instructions) ---
        class VoiceBridgeAgent(Agent):
            def __init__(self) -> None:
                super().__init__(
                    instructions="You are a voice bridge. Do not generate responses. "
                    "All responses come from the channel server.",
                )

        # --- Server setup ---
        server = AgentServer()

        def prewarm(proc: JobProcess) -> None:
            proc.userdata["vad"] = silero.VAD.load()

        server.setup_fnc = prewarm

        @server.rtc_session()
        async def entrypoint(ctx: JobContext) -> None:
            # 1. Connect to channel server
            ws = await websockets.connect(config.channel_ws_url)
            bridge = ChannelBridge(
                ws,
                user=config.user,
                feishu_target="",  # Discovered after registration
            )
            await bridge.register()
            bridge_holder["bridge"] = bridge

            # 2. Create STT/TTS plugin instances
            stt = deepgram.STT(
                api_key=config.deepgram_api_key,
                language=config.language,
            )
            tts = fishaudio.TTS(
                api_key=config.fish_api_key,
                model_id=config.fish_model_id,
            )

            # 3. Create AgentSession
            session = AgentSession(
                stt=stt,
                tts=tts,
                vad=ctx.proc.userdata["vad"],
            )

            # 4. Subscribe to transcription events
            @session.on("user_input_transcribed")
            def on_user_transcribed(ev) -> None:
                """When STT produces final transcript, send to channel server."""
                if ev.is_final and ev.text.strip():
                    asyncio.ensure_future(_handle_transcript(bridge, ev.text))

            # 5. Set up reply callback: channel server → session.say() → TTS
            def on_reply(text: str) -> None:
                session.say(text)

            bridge.on_reply = on_reply

            # 6. Start session and bridge receive loop concurrently
            await session.start(agent=VoiceBridgeAgent(), room=ctx.room)

            # Run bridge receive loop as background task
            asyncio.ensure_future(bridge.receive_loop())

        async def _handle_transcript(bridge: ChannelBridge, text: str) -> None:
            """Handle final ASR transcript: send to CC and sync to Feishu."""
            await bridge.send_transcript(text)
            await bridge.sync_transcript_to_feishu(text)

        # Run the agent server (blocks)
        cli.run_app(server)


def main():
    """CLI entry point for the voice agent."""
    config = VoiceConfig.from_env()
    agent = VoiceAgent(config)
    agent.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_voice_agent.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/voice/agent.py tests/channel_server/adapters/test_voice_agent.py
git commit -m "feat(voice): voice agent with LiveKit AgentSession + channel bridge"
```

---

### Task 4: CC Adapter — Accept Voice Actor Registration (`channel_server/adapters/cc/adapter.py`)

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py` (on `feature/actor-model` branch)
- Test: `tests/channel_server/adapters/test_cc_adapter.py`

Two changes needed:
1. `_handle_register`: detect `voice:` prefix and use `voice_session` handler
2. `handle_message`: add `"transcript"` to the list of routable message types (so voice agent's transcript messages reach the actor)

- [ ] **Step 1: Write test for voice actor registration**

Add to `tests/channel_server/adapters/test_cc_adapter.py`:

```python
@pytest.mark.asyncio
async def test_voice_actor_registration(runtime, cc_adapter):
    """Voice actor registers with voice_session handler instead of cc_session."""
    ws = FakeWebSocket()
    await cc_adapter.handle_message(ws, {
        "type": "register",
        "instance_id": "voice:alice",
        "tag_name": "voice",
    })

    actor = runtime.lookup("voice:alice")
    assert actor is not None
    assert actor.handler == "voice_session"
    assert actor.tag == "voice"

    sent = json.loads(ws.sent[-1])
    assert sent["type"] == "registered"
    assert sent["address"] == "voice:alice"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_cc_adapter.py::test_voice_actor_registration -v`
Expected: FAIL — voice actor gets handler `cc_session` instead of `voice_session`

- [ ] **Step 3: Modify _handle_register to detect voice actors**

In `channel_server/adapters/cc/adapter.py`, update the `_handle_register` method. Change the auto-spawn block:

```python
        # Look up or auto-spawn
        actor = self.runtime.lookup(address)
        if actor is None or actor.state == "ended":
            tag = msg.get("tag_name", "") or instance_id.split(".")[-1]
            # Voice actors use voice_session handler
            handler = "voice_session" if address.startswith("voice:") else "cc_session"
            self.runtime.spawn(
                address,
                handler,
                tag=tag,
                state="active",
                transport=Transport(type="websocket", config={"instance_id": instance_id}),
            )
            log.info("Auto-spawned %s actor: %s", handler, address)
```

- [ ] **Step 4: Add `transcript` to routable message types**

In `channel_server/adapters/cc/adapter.py`, update the `handle_message` method. Add `"transcript"` to the routable types tuple:

```python
        elif msg_type in ("reply", "forward", "send_summary", "update_title",
                          "send_file", "react", "tool_notify", "transcript"):
            self._route_to_actor(ws, msg)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_cc_adapter.py -v`
Expected: All tests PASS (including new voice registration test)

- [ ] **Step 6: Commit**

```bash
git add channel_server/adapters/cc/adapter.py tests/channel_server/adapters/test_cc_adapter.py
git commit -m "feat(voice): CC adapter voice actor handler + transcript routing"
```

---

### Task 5: Dependencies and Entry Point

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add voice dependencies to pyproject.toml**

Add an optional `voice` extra to pyproject.toml:

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

Add a script entry point:

```toml
[project.scripts]
voice-agent = "channel_server.adapters.voice.agent:main"
```

- [ ] **Step 2: Verify install**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv pip install -e ".[voice]" --dry-run`
Expected: resolves all dependencies without errors

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat(voice): add livekit + STT/TTS dependencies and voice-agent entry point"
```

---

### Task 6: Web Voice Client — Project Setup

**Files:**
- Create: `voice_web/package.json`
- Create: `voice_web/index.html`
- Create: `voice_web/src/App.tsx`
- Create: `voice_web/src/components/TranscriptPanel.tsx`
- Create: `voice_web/src/components/VoiceControls.tsx`
- Create: `voice_web/src/hooks/useVoiceSession.ts`

- [ ] **Step 1: Initialize project**

```bash
cd /Users/h2oslabs/cc-openclaw
mkdir -p voice_web/src/components voice_web/src/hooks
```

```json
// voice_web/package.json
{
  "name": "openclaw-voice",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --port 13036",
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

- [ ] **Step 2: Create index.html**

```html
<!-- voice_web/index.html -->
<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>OpenClaw Voice</title>
  <!-- Feishu JSSDK for webapp identity -->
  <script src="https://lf1-cdn-tos.bytegoofy.com/goofy/lark/op/h5-js-sdk-1.5.30/h5-js-sdk-1.5.30.js"></script>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.tsx"></script>
</body>
</html>
```

Note: The Feishu JSSDK (`window.h5sdk` / `tt`) provides `tt.requestAuthCode()` to get the current user's identity inside the Feishu webview. The web client will call this on startup to identify the user, then pass the auth code to the token server to get a LiveKit room token bound to the correct user.

- [ ] **Step 3: Create main.tsx entry**

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

- [ ] **Step 4: Create tsconfig.json**

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

- [ ] **Step 5: Create useVoiceSession hook**

```tsx
// voice_web/src/hooks/useVoiceSession.ts
import { useCallback, useEffect, useRef, useState } from "react";
import { Room, RoomEvent } from "livekit-client";

export interface TranscriptEntry {
  role: "user" | "assistant";
  text: string;
  timestamp: number;
  partial?: boolean;
}

interface UseVoiceSessionOptions {
  livekitUrl: string;
  token: string;
}

export function useVoiceSession({ livekitUrl, token }: UseVoiceSessionOptions) {
  const [connected, setConnected] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [partialText, setPartialText] = useState("");
  const roomRef = useRef<Room | null>(null);

  // Cleanup on unmount
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
        setTranscript((prev) => [
          ...prev,
          { role: "user", text: msg.text, timestamp: Date.now() },
        ]);
      } else if (msg.type === "assistant_reply") {
        setTranscript((prev) => [
          ...prev,
          { role: "assistant", text: msg.text, timestamp: Date.now() },
        ]);
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

- [ ] **Step 6: Create TranscriptPanel component**

```tsx
// voice_web/src/components/TranscriptPanel.tsx
import React, { useEffect, useRef } from "react";
import type { TranscriptEntry } from "../hooks/useVoiceSession";

interface Props {
  transcript: TranscriptEntry[];
  partialText: string;
}

export function TranscriptPanel({ transcript, partialText }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript, partialText]);

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "16px" }}>
      {transcript.map((entry, i) => (
        <div
          key={i}
          style={{
            marginBottom: "12px",
            textAlign: entry.role === "user" ? "right" : "left",
          }}
        >
          <span
            style={{
              display: "inline-block",
              padding: "8px 12px",
              borderRadius: "12px",
              maxWidth: "80%",
              background: entry.role === "user" ? "#007AFF" : "#E5E5EA",
              color: entry.role === "user" ? "#fff" : "#000",
            }}
          >
            {entry.text}
          </span>
        </div>
      ))}
      {partialText && (
        <div style={{ marginBottom: "12px", textAlign: "right", opacity: 0.6 }}>
          <span
            style={{
              display: "inline-block",
              padding: "8px 12px",
              borderRadius: "12px",
              background: "#007AFF",
              color: "#fff",
            }}
          >
            {partialText}...
          </span>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
```

- [ ] **Step 7: Create VoiceControls component**

```tsx
// voice_web/src/components/VoiceControls.tsx
import React from "react";

interface Props {
  connected: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
}

export function VoiceControls({ connected, onConnect, onDisconnect }: Props) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "center",
        padding: "16px",
        borderTop: "1px solid #E5E5EA",
      }}
    >
      {connected ? (
        <button
          onClick={onDisconnect}
          style={{
            padding: "12px 32px",
            borderRadius: "24px",
            border: "none",
            background: "#FF3B30",
            color: "#fff",
            fontSize: "16px",
            cursor: "pointer",
          }}
        >
          结束通话
        </button>
      ) : (
        <button
          onClick={onConnect}
          style={{
            padding: "12px 32px",
            borderRadius: "24px",
            border: "none",
            background: "#34C759",
            color: "#fff",
            fontSize: "16px",
            cursor: "pointer",
          }}
        >
          开始通话
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 8: Create App component**

```tsx
// voice_web/src/App.tsx
import React, { useEffect, useState } from "react";
import { useVoiceSession } from "./hooks/useVoiceSession";
import { TranscriptPanel } from "./components/TranscriptPanel";
import { VoiceControls } from "./components/VoiceControls";

const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL || "wss://voice-lk.ezagent.chat";
const TOKEN_SERVER = import.meta.env.VITE_TOKEN_SERVER || "https://voice.ezagent.chat/api";
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

/** Step 1: Call server to get JSSDK config signature, then call h5sdk.config(). */
async function initFeishuJssdk(): Promise<void> {
  if (!window.h5sdk) return;

  // Get signature from our server
  const res = await fetch(`${TOKEN_SERVER}/jssdk-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: window.location.href }),
  });
  if (!res.ok) throw new Error("Failed to get JSSDK config");
  const { appId, timestamp, nonceStr, signature } = await res.json();

  // Configure the JSSDK — must succeed before requestAuthCode works
  await window.h5sdk.config({
    appId,
    timestamp,
    nonceStr,
    signature,
    jsApiList: ["requestAuthCode"],
  });
}

/** Step 2: After JSSDK config, request auth code for user identity. */
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

/** Step 3: Exchange auth code for LiveKit room token. */
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
  const [userName, setUserName] = useState("");

  const { connected, transcript, partialText, connect, disconnect } =
    useVoiceSession({ livekitUrl: LIVEKIT_URL, token });

  useEffect(() => {
    (async () => {
      // Must be inside Feishu webview
      if (!window.h5sdk || !window.tt) {
        setError("请从飞书客户端打开此页面");
        return;
      }
      try {
        // Step 1: JSSDK config (server-side signature)
        await initFeishuJssdk();
        // Step 2: Get auth code (user identity)
        const authCode = await getFeishuAuthCode();
        if (!authCode) {
          setError("飞书授权失败，请重试");
          return;
        }
        // Step 3: Exchange auth code for LiveKit token
        const { token: tk, user } = await fetchToken(authCode);
        setToken(tk);
        setUserName(user);
        setReady(true);
      } catch (e) {
        setError(e instanceof Error ? e.message : "认证失败");
      }
    })();
  }, []);

  if (error) {
    return (
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        height: "100vh", fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
        textAlign: "center", padding: "32px", color: "#8E8E93",
      }}>
        <div>
          <p style={{ fontSize: "48px", margin: "0 0 16px" }}>🔒</p>
          <p style={{ fontSize: "16px" }}>{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        maxWidth: "480px",
        margin: "0 auto",
        fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
      }}
    >
      <header
        style={{
          padding: "16px",
          textAlign: "center",
          borderBottom: "1px solid #E5E5EA",
        }}
      >
        <h1 style={{ margin: 0, fontSize: "18px" }}>OpenClaw Voice</h1>
        <span
          style={{
            fontSize: "12px",
            color: connected ? "#34C759" : "#8E8E93",
          }}
        >
          {connected ? "通话中" : ready ? "就绪" : "正在连接飞书..."}
        </span>
      </header>
      <TranscriptPanel transcript={transcript} partialText={partialText} />
      <VoiceControls
        connected={connected}
        onConnect={connect}
        onDisconnect={disconnect}
      />
    </div>
  );
}
```

- [ ] **Step 9: Create vite.config.ts**

```ts
// voice_web/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 13036,
  },
});
```

注意：`/api` → token server 的路由由 cloudflared path-based ingress 处理，Vite 不需要 proxy 配置。

- [ ] **Step 10: Commit**

```bash
git add voice_web/
git commit -m "feat(voice): web voice client with Feishu auth, LiveKit, and Cloudflare Tunnel support"
```

---

### Task 7: Token Server with Feishu Auth (`channel_server/adapters/voice/token_server.py`)

**Files:**
- Create: `channel_server/adapters/voice/token_server.py`
- Test: `tests/channel_server/adapters/test_token_server.py`

The web client needs a LiveKit access token to join a room. The token server **verifies the Feishu auth code server-side** before issuing a token — this is the security gate that ensures only Feishu users can connect.

Uses `httpx` (async, already a project dependency) instead of `requests` for consistency with the async architecture. Runs as an `aiohttp` web server.

- [ ] **Step 1: Write tests for token server auth logic**

Tests mock two sequential httpx calls (app_access_token + OIDC exchange) using `side_effect`:

```python
# tests/channel_server/adapters/test_token_server.py
"""Tests for Feishu auth code verification in the token server."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from channel_server.adapters.voice.token_server import verify_feishu_auth_code


def _mock_response(json_data):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_verify_valid_auth_code():
    """Valid auth code: two API calls succeed, returns user info."""
    app_token_resp = _mock_response({
        "code": 0,
        "app_access_token": "a-xxx",
    })
    user_token_resp = _mock_response({
        "code": 0,
        "data": {
            "access_token": "u-xxx",
            "open_id": "ou_abc123",
            "name": "林懿伦",
        },
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_token_resp, user_token_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("channel_server.adapters.voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("valid_code", "cli_app123", "secret123")
    assert result is not None
    assert result["open_id"] == "ou_abc123"
    assert result["name"] == "林懿伦"
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_verify_invalid_auth_code():
    """Invalid auth code: app token succeeds, OIDC exchange fails."""
    app_token_resp = _mock_response({
        "code": 0,
        "app_access_token": "a-xxx",
    })
    oidc_fail_resp = _mock_response({
        "code": 10012,
        "msg": "invalid code",
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[app_token_resp, oidc_fail_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("channel_server.adapters.voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("bad_code", "cli_app123", "secret123")
    assert result is None


@pytest.mark.asyncio
async def test_verify_app_token_fails():
    """App token request fails → returns None (fail closed)."""
    app_fail_resp = _mock_response({"code": 10003, "msg": "invalid app"})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=app_fail_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("channel_server.adapters.voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "cli_app123", "secret123")
    assert result is None


@pytest.mark.asyncio
async def test_verify_network_error():
    """Network error returns None (fail closed)."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("channel_server.adapters.voice.token_server.httpx.AsyncClient", return_value=mock_client):
        result = await verify_feishu_auth_code("code", "cli_app123", "secret123")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_token_server.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement async token server with httpx**

```python
# channel_server/adapters/voice/token_server.py
"""Async token endpoint — verifies Feishu auth code via httpx, issues LiveKit room tokens."""
from __future__ import annotations

import json
import logging

import httpx
from aiohttp import web
from livekit.api import AccessToken, VideoGrants

from channel_server.adapters.voice.config import VoiceConfig

log = logging.getLogger(__name__)

FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_OIDC_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_JSAPI_TICKET_URL = "https://open.feishu.cn/open-apis/jssdk/ticket/get"


async def get_app_access_token(app_id: str, app_secret: str) -> str | None:
    """Get app_access_token from Feishu. Returns token string or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("Failed to get app_access_token: %s", data.get("msg"))
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
                log.warning("Failed to get jsapi_ticket: %s", data.get("msg"))
                return None
            return data["data"]["ticket"]
    except Exception as e:
        log.warning("get_jsapi_ticket error: %s", e)
        return None


def generate_jssdk_signature(ticket: str, nonce: str, timestamp: str, url: str) -> str:
    """Generate SHA1 signature for Feishu JSSDK config.

    Signature string format: jsapi_ticket=<ticket>&noncestr=<nonce>&timestamp=<timestamp>&url=<url>
    """
    import hashlib
    sign_str = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(sign_str.encode()).hexdigest()


async def verify_feishu_auth_code(code: str, app_id: str, app_secret: str) -> dict | None:
    """Exchange Feishu auth code for user info. Returns user dict or None on failure.

    Two-step flow:
    1. get_app_access_token() → app_access_token
    2. POST authen/v1/oidc/access_token → exchange auth code for user token
    """
    try:
        app_access_token = await get_app_access_token(app_id, app_secret)
        if not app_access_token:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_OIDC_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_access_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("Feishu auth code verification failed: %s", data.get("msg"))
                return None

            return {
                "open_id": data["data"]["open_id"],
                "name": data["data"].get("name", ""),
                "access_token": data["data"]["access_token"],
            }
    except Exception as e:
        log.warning("Feishu auth verification error: %s", e)
        return None


def create_token_app(config: VoiceConfig) -> web.Application:
    """Create an aiohttp web application for the token endpoint."""

    async def handle_token(request: web.Request) -> web.Response:
        body = await request.json()
        auth_code = body.get("auth_code", "")

        if not auth_code:
            return web.json_response(
                {"error": "Missing auth_code. Please open from Feishu."},
                status=403,
                headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
            )

        user_info = await verify_feishu_auth_code(
            auth_code, config.feishu_app_id, config.feishu_app_secret,
        )
        if user_info is None:
            return web.json_response(
                {"error": "Invalid or expired auth code."},
                status=403,
                headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
            )

        open_id = user_info["open_id"]
        name = user_info.get("name", open_id)
        room = f"voice-{open_id}"

        token = AccessToken(
            api_key=config.livekit_api_key,
            api_secret=config.livekit_api_secret,
        )
        token.identity = open_id
        token.name = name
        token.add_grant(VideoGrants(room_join=True, room=room))

        jwt = token.to_jwt()
        return web.json_response(
            {"token": jwt, "room": room, "user": name},
            headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
        )

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "https://voice.ezagent.chat",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    async def handle_jssdk_config(request: web.Request) -> web.Response:
        """Return JSSDK config params (signature) for h5sdk.config().

        Frontend sends { url: window.location.href } so the signature is bound to the page URL.
        """
        import time
        import secrets

        body = await request.json()
        url = body.get("url", "")
        if not url:
            return web.json_response(
                {"error": "Missing url"},
                status=400,
                headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
            )

        app_access_token = await get_app_access_token(config.feishu_app_id, config.feishu_app_secret)
        if not app_access_token:
            return web.json_response(
                {"error": "Failed to get app token"},
                status=500,
                headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
            )

        ticket = await get_jsapi_ticket(app_access_token)
        if not ticket:
            return web.json_response(
                {"error": "Failed to get jsapi ticket"},
                status=500,
                headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
            )

        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(8)
        signature = generate_jssdk_signature(ticket, nonce, timestamp, url)

        return web.json_response(
            {
                "appId": config.feishu_app_id,
                "timestamp": timestamp,
                "nonceStr": nonce,
                "signature": signature,
            },
            headers={"Access-Control-Allow-Origin": "https://voice.ezagent.chat"},
        )

    app = web.Application()
    app.router.add_post("/api/jssdk-config", handle_jssdk_config)
    app.router.add_route("OPTIONS", "/api/jssdk-config", handle_options)
    app.router.add_post("/api/token", handle_token)
    app.router.add_route("OPTIONS", "/api/token", handle_options)
    return app


async def run_token_server(config: VoiceConfig, port: int = 8089) -> web.AppRunner:
    """Start the async token server. Returns the runner for lifecycle management."""
    app = create_token_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("Token server started on port %d", port)
    return runner
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/adapters/test_token_server.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/voice/token_server.py tests/channel_server/adapters/test_token_server.py
git commit -m "feat(voice): async token server with Feishu auth code verification (httpx)"
```

---

### Task 8: Integration Test — Full Voice Loop

**Files:**
- Create: `tests/channel_server/test_voice_integration.py`

End-to-end test without LiveKit: verify that a voice actor can register, send a transcript that routes to a CC actor, and receive a reply back.

- [ ] **Step 1: Write integration test**

```python
# tests/channel_server/test_voice_integration.py
"""End-to-end test: voice actor → channel server → CC actor → reply back."""
import asyncio
import json
import pytest

from channel_server.core.actor import Actor, Message, Send, Transport, TransportSend
from channel_server.core.runtime import ActorRuntime
from channel_server.core.handler import get_handler


@pytest.fixture
def runtime():
    rt = ActorRuntime()
    # Capture transport sends for assertion
    rt._transport_log = []
    original_exec_ts = rt._execute_transport_send
    def logging_transport_send(actor, action):
        rt._transport_log.append((actor.address, action.payload))
        # Don't actually send (no real WS)
    rt._execute_transport_send = logging_transport_send
    return rt


def test_voice_handler_registered():
    """voice_session handler exists in the registry."""
    handler = get_handler("voice_session")
    assert handler is not None


@pytest.mark.asyncio
async def test_voice_transcript_reaches_cc_mailbox(runtime):
    """Full runtime loop: voice transcript → runtime.send → CC actor mailbox."""
    # Spawn voice and CC actors
    runtime.spawn("voice:alice", "voice_session", tag="voice",
                  downstream=["cc:alice.root"],
                  transport=Transport(type="websocket", config={}))
    runtime.spawn("cc:alice.root", "cc_session", tag="root",
                  downstream=["feishu:oc_xxx"],
                  transport=Transport(type="websocket", config={}))

    # Send transcript message to voice actor (simulates CC adapter _route_to_actor)
    transcript_msg = Message(
        sender="voice:alice",
        type="text",
        payload={"command": "transcript", "text": "你好"},
    )
    runtime.send("voice:alice", transcript_msg)

    # Start runtime briefly to process mailboxes
    import asyncio
    run_task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.1)
    await runtime.shutdown()

    # CC actor should have received the forwarded message via TransportSend
    cc_sends = [(addr, p) for addr, p in runtime._transport_log if addr == "cc:alice.root"]
    assert len(cc_sends) == 1
    assert cc_sends[0][1]["text"] == "你好"


@pytest.mark.asyncio
async def test_cc_reply_reaches_voice_transport(runtime):
    """Full runtime loop: CC reply → voice actor → TransportSend (for TTS)."""
    runtime.spawn("voice:alice", "voice_session", tag="voice",
                  transport=Transport(type="websocket", config={}))

    # CC sends a reply that arrives at voice actor
    reply = Message(
        sender="cc:alice.root",
        type="text",
        payload={"text": "I'm Claude, how can I help?"},
    )
    runtime.send("voice:alice", reply)

    import asyncio
    run_task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.1)
    await runtime.shutdown()

    # Voice actor should push to transport (for TTS playback)
    voice_sends = [(addr, p) for addr, p in runtime._transport_log if addr == "voice:alice"]
    assert len(voice_sends) == 1
    assert voice_sends[0][1]["text"] == "I'm Claude, how can I help?"
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw-voice && uv run pytest tests/channel_server/test_voice_integration.py -v`
Expected: All 3 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/channel_server/test_voice_integration.py
git commit -m "test(voice): runtime-level integration tests for voice ↔ CC message flow"
```

---

## Summary

| Task | Component | Key deliverable |
|------|-----------|-----------------|
| 1 | VoiceSessionHandler | Actor handler for ASR→CC and CC→TTS routing |
| 2 | VoiceConfig | Env-based config for LiveKit, Deepgram, FishAudio |
| 3 | VoiceAgent + ChannelBridge | LiveKit AgentSession lifecycle + channel server WebSocket bridge |
| 4 | CC Adapter modification | Detect `voice:` prefix, add `transcript` to routable types |
| 5 | Dependencies + entry point | pyproject.toml extras (livekit-agents, httpx, fishaudio, silero) |
| 6 | Web Voice Client | React app with LiveKit SDK, Feishu JSSDK auth, transcript panel |
| 7 | Token server + Feishu auth | Async httpx token server, Feishu auth_code 服务端校验 |
| 8 | Integration tests | Runtime-level mailbox delivery tests for voice ↔ CC flow |

## Prerequisites

### LiveKit Server

The voice pipeline requires a running LiveKit server. Options:
- **LiveKit Cloud** (recommended for MVP): sign up at https://cloud.livekit.io, get URL + API key/secret
- **Self-hosted**: `docker run --rm -p 7880:7880 -p 7881:7881 -e LIVEKIT_KEYS="devkey: secret" livekit/livekit-server`

Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` env vars accordingly.

## Cloudflare Tunnel 配置

语音服务通过 `voice.ezagent.chat` 对外提供，使用 Cloudflare Tunnel 将本机 13036 端口映射到公网。

### 前置条件

```bash
# 安装 cloudflared
brew install cloudflared

# 登录 Cloudflare（首次）
cloudflared tunnel login
# 浏览器会打开 Cloudflare 授权页面，选择 ezagent.chat 所在的 zone
```

### 创建持久隧道

```bash
# 1. 创建隧道（名称 openclaw-voice）
cloudflared tunnel create openclaw-voice
# 输出: Created tunnel openclaw-voice with id <TUNNEL_ID>

# 2. 配置 DNS 记录
#    Cloudflare Dashboard → ezagent.chat → DNS → 添加记录:
#    Type: CNAME
#    Name: voice
#    Target: <TUNNEL_ID>.cfargotunnel.com
#    Proxy: ON (橙色云朵)
#
#    或用命令行:
cloudflared tunnel route dns openclaw-voice voice.ezagent.chat
```

### 隧道配置文件

Cloudflare Tunnel 直连本地服务，使用 path-based ingress 路由，不经过 Caddy：

```yaml
# ~/.cloudflared/config.yml
tunnel: <TUNNEL_ID>
credentials-file: /Users/h2oslabs/.cloudflared/<TUNNEL_ID>.json

ingress:
  # /api/* → Token Server（飞书认证 + LiveKit token 签发）
  - hostname: voice.ezagent.chat
    path: /api/*
    service: http://localhost:8089
  # 其余请求 → Vite dev server（语音 Web 客户端）
  - hostname: voice.ezagent.chat
    service: http://localhost:13036
  # 兜底
  - service: http_status:404
```

> 注意：cloudflared path-based routing 会保留完整路径转发。
> 例如 `https://voice.ezagent.chat/api/token` → `http://localhost:8089/api/token`
> 因此 token server 需要监听 `/api/token` 而不是 `/token`。

> 本机 Caddy 服务（`/etc/caddy/Caddyfile`）继续管理 `*.inside.h2os.cloud`，与 cloudflared 互不干扰。
> 未来如需整合，可将 cloudflared 改为指向 Caddy，由 Caddy 统一路由。

### 启动隧道

```bash
# 前台运行（开发）
cloudflared tunnel run openclaw-voice

# 或作为服务安装（生产）
sudo cloudflared service install
```

### Cloudflare Dashboard 操作步骤

1. 登录 https://dash.cloudflare.com → 选择 **ezagent.chat** 域名
2. 左侧 **DNS** → **Records** → 添加记录：
   - Type: `CNAME`
   - Name: `voice`
   - Target: `<TUNNEL_ID>.cfargotunnel.com`
   - Proxy: **ON**（橙色云朵）
3. 左侧 **Zero Trust** → **Networks** → **Tunnels** → 确认 `openclaw-voice` 状态为 Healthy
4. （可选）**Zero Trust** → **Access** → **Applications** → 可额外添加 Cloudflare Access 策略作为双重保护

### 安全说明

```
请求链路:
飞书 Webview → voice.ezagent.chat (Cloudflare CDN/TLS)
    → Cloudflare Tunnel (cloudflared)
        → /api/* → Token Server (localhost:8089)
            → 飞书 API 验证 auth_code ← 必须通过才签发 LiveKit token
        → /* → Vite (localhost:13036)

无 auth_code → 403 拒绝 → 无法通话
伪造 auth_code → 飞书 API 验证失败 → 403 拒绝
直接浏览器访问 → 无 tt 对象 → 无 auth_code → 显示 "请从飞书客户端打开"
```

---

## Feishu Webapp Setup (Manual)

在飞书开发者后台配置网页应用:

1. 打开 https://open.feishu.cn/app → 选择应用
2. 左侧 **应用功能** → **网页应用**
3. 设置 **桌面端主页** 为 `https://voice.ezagent.chat`
4. 设置 **移动端主页** 为 `https://voice.ezagent.chat`
5. **安全设置** → **重定向 URL** 添加 `https://voice.ezagent.chat`
6. 确保应用已开启 **网页应用** 能力
7. **权限管理** → 确认已申请 `contact:user.base:readonly` 权限（用于获取用户信息）
8. 发布应用版本

用户通过飞书工作台或侧边栏打开此网页应用即可使用语音通话。

### 远程调试（开发阶段）

在 `index.html` 的 `<head>` 中添加:
```html
<script src="https://sf1-scmcdn-cn.feishucdn.com/obj/feishu-static/op/fe/devtools_frontend/remote-debug-0.0.1-alpha.6.js"></script>
```
然后在开发者后台 → **网页应用** → **网页应用调试工具** 中进行远程调试。

---

## Post-MVP: Phase 2 & 3

After the MVP is working with Deepgram + FishAudio:
- **Phase 2**: Write `livekit-plugins-volcengine` to replace Deepgram/FishAudio with 火山引擎
- **Phase 3**: Barge-in tuning, conversation history persistence, multi-user rooms
