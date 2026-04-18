# Voice Actor Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pseudo_llm with a CC session via the actor model. Voice gateway connects to channel_server WS, dynamically spawns a paired Claude Code session, and uses it for LLM processing.

**Architecture:** Voice gateway opens WS to channel_server, registers with `voice:` prefix. CCAdapter spawns voice actor + CC actor pair (CC with tmux). ActorBridge provides `query(text) -> str` interface. Both E2E and Split modes use the bridge. E2E mode extends recv_timeout to 60s.

**Tech Stack:** Python 3.11+ (aiohttp, websockets), existing channel_server actor runtime, existing voice_gateway

**Spec:** `docs/superpowers/specs/2026-04-17-voice-actor-integration-design.md`

---

## File Structure

```
channel_server/
├── core/handlers/voice.py       # NEW: VoiceSessionHandler
├── core/handler.py              # MODIFY: register voice_session handler
├── adapters/cc/adapter.py       # MODIFY: voice: prefix registration logic

voice_gateway/
├── actor_bridge.py              # NEW: WS client to channel_server
├── config.py                    # MODIFY: add CHANNEL_SERVER_WS_URL, extend recv_timeout
├── session.py                   # MODIFY: pseudo_llm → bridge.query
├── session_split.py             # MODIFY: pseudo_llm → bridge.query
├── server.py                    # MODIFY: create bridge on startup, pass to sessions
```

---

### Task 1: VoiceSessionHandler + Registration

**Files:**
- Create: `channel_server/core/handlers/voice.py`
- Modify: `channel_server/core/handler.py`

- [ ] **Step 1: Create VoiceSessionHandler**

```python
# channel_server/core/handlers/voice.py
"""Handler for voice gateway actors. Routes messages between voice gateway and CC session."""
import logging
from core.actor import Actor, Message, Action, Send, TransportSend

log = logging.getLogger(__name__)


class VoiceSessionHandler:
    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        if msg.sender == actor.address:
            # From voice gateway itself: route query to paired CC session
            cc_target = actor.metadata.get("cc_target")
            if not cc_target:
                log.warning("Voice actor %s has no cc_target", actor.address)
                return []
            log.info("Voice %s → CC %s: %s", actor.address, cc_target,
                     msg.payload.get("text", "")[:80])
            return [Send(to=cc_target, message=Message(
                sender=actor.address, payload=msg.payload, metadata=msg.metadata,
            ))]
        else:
            # From CC session (response): forward to voice gateway via transport
            log.info("CC → Voice %s: %s", actor.address,
                     msg.payload.get("text", "")[:80])
            return [TransportSend(payload={"action": "response", **msg.payload})]

    def on_spawn(self, actor: Actor) -> list[Action]:
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        return []
```

- [ ] **Step 2: Register handler**

In `channel_server/core/handler.py`, add to HANDLER_REGISTRY:

```python
from core.handlers.voice import VoiceSessionHandler
# In HANDLER_REGISTRY dict:
"voice_session": VoiceSessionHandler(),
```

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw
git add channel_server/core/handlers/voice.py channel_server/core/handler.py
git commit -m "feat: VoiceSessionHandler — routes voice ↔ CC session messages"
```

---

### Task 2: CCAdapter — Voice Registration Logic

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`

- [ ] **Step 1: Add voice registration branch**

In `_handle_register()`, after resolving the address but before the existing root session wiring logic, add a branch for `voice:` prefix:

```python
# Detect voice gateway registration
if address.startswith("voice:"):
    # Derive CC session address from voice address
    # voice:user.voice-1 → cc:user.voice-1
    cc_addr = "cc:" + address[len("voice:"):]
    
    # Spawn voice actor
    self.runtime.spawn(
        address, "voice_session", tag=tag or "Voice",
        transport=Transport(type="websocket", config={"instance_id": instance_id}),
        metadata={"cc_target": cc_addr},
    )
    
    # Spawn paired CC session (suspended, waiting for Claude Code WS)
    self.runtime.spawn(
        cc_addr, "cc_session", tag=f"voice-{tag or 'session'}",
        state="suspended",
        downstream=[address],  # CC responses go to voice actor
    )
    
    # Start Claude Code tmux process
    user = instance_id.split(".")[0].replace("voice:", "") if "." in instance_id else ""
    session_name = instance_id.split(".")[-1] if "." in instance_id else instance_id
    self.spawn_cc_process(user, session_name, cc_addr, tag or "Voice")
    
    # Register WS transport for voice actor
    self._ws_to_address[id(ws)] = address
    self._address_to_ws[address] = ws
    
    await ws.send(json.dumps({"action": "registered", "address": address}))
    log.info("Voice registered: %s → CC %s", address, cc_addr)
    return
```

- [ ] **Step 2: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw
git add channel_server/adapters/cc/adapter.py
git commit -m "feat: CCAdapter voice: prefix registration — spawn voice + CC actor pair"
```

---

### Task 3: ActorBridge — Voice Gateway WS Client

**Files:**
- Create: `voice_gateway/actor_bridge.py`

- [ ] **Step 1: Create actor_bridge.py**

```python
# voice_gateway/actor_bridge.py
"""WebSocket bridge to channel_server actor model."""
import asyncio
import json
import logging

import websockets

log = logging.getLogger(__name__)


class ActorBridge:
    def __init__(self):
        self.ws = None
        self.address = ""
        self._pending: asyncio.Future | None = None
        self._reader_task: asyncio.Task | None = None

    async def connect(self, url: str, instance_id: str) -> None:
        """Connect to channel_server WS and register as voice actor."""
        self.ws = await websockets.connect(url, ping_interval=None)

        # Register
        await self.ws.send(json.dumps({
            "action": "register",
            "instance_id": instance_id,
            "tag_name": "Voice Gateway",
        }))

        # Wait for registered ACK
        raw = await self.ws.recv()
        msg = json.loads(raw)
        if msg.get("action") != "registered":
            raise ConnectionError(f"Registration failed: {msg}")
        self.address = msg.get("address", "")
        log.info(f"ActorBridge registered as {self.address}")

        # Start background reader
        self._reader_task = asyncio.create_task(self._read_loop())

    async def query(self, text: str, timeout: float = 60.0) -> str:
        """Send text to CC session, wait for response."""
        if not self.ws:
            raise RuntimeError("ActorBridge not connected")

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending = future

        await self.ws.send(json.dumps({"action": "query", "text": text}))
        log.info(f"ActorBridge query sent: {text[:80]}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            log.info(f"ActorBridge response: {result[:80]}")
            return result
        except asyncio.TimeoutError:
            log.warning("ActorBridge query timed out after %ss", timeout)
            self._pending = None
            raise

    async def _read_loop(self) -> None:
        """Background reader: resolve pending future on response."""
        try:
            async for raw in self.ws:
                if isinstance(raw, str):
                    msg = json.loads(raw)
                    action = msg.get("action", "")

                    if action == "response" and self._pending and not self._pending.done():
                        self._pending.set_result(msg.get("text", ""))
                        self._pending = None
                    elif action == "message" and self._pending and not self._pending.done():
                        # CCSessionHandler sends action=message for external messages
                        self._pending.set_result(msg.get("text", ""))
                        self._pending = None
                    else:
                        log.debug(f"ActorBridge received: {msg}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"ActorBridge read error: {e}")

    async def close(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.ws:
            await self.ws.close()
            log.info("ActorBridge closed")
```

- [ ] **Step 2: Verify import**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run python -c "from actor_bridge import ActorBridge; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/actor_bridge.py
git commit -m "feat(gateway): ActorBridge — WS client to channel_server actor model"
```

---

### Task 4: Config + Server — Bridge Startup

**Files:**
- Modify: `voice_gateway/config.py`
- Modify: `voice_gateway/server.py`

- [ ] **Step 1: Add config**

Add to `voice_gateway/config.py`:

```python
# Channel server connection (actor model bridge)
CHANNEL_SERVER_WS_URL = os.environ.get(
    "CHANNEL_SERVER_WS_URL",
    "ws://127.0.0.1:8765/ws/cc",
)
VOICE_INSTANCE_ID = os.environ.get("VOICE_INSTANCE_ID", "voice:user.voice-1")
```

Also update `START_SESSION_CONFIG` to extend recv_timeout:

```python
"dialog": {
    ...
    "extra": {
        "input_mod": "keep_alive",
        "recv_timeout": 60,  # Extended for CC response time
    },
},
```

- [ ] **Step 2: Update server.py**

Add bridge creation and pass to sessions. In `ws_handler`, create bridge per session (or share one). In `main()`, no change needed — bridge connects per session.

Update ws_handler to pass bridge config:

```python
if data.get("type") == "start":
    mode = data.get("mode", "e2e")
    log.info(f"Starting session in {mode} mode")
    if mode == "split":
        from session_split import SplitSession
        session = SplitSession(ws, start_config=data)
    else:
        session = Session(ws, start_config=data)
    await session.run()
```

No server.py change needed — each session creates its own bridge internally.

- [ ] **Step 3: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/config.py voice_gateway/server.py
git commit -m "feat(config): add CHANNEL_SERVER_WS_URL, extend recv_timeout to 60s"
```

---

### Task 5: Session — Replace pseudo_llm with ActorBridge

**Files:**
- Modify: `voice_gateway/session.py` (E2E mode)
- Modify: `voice_gateway/session_split.py` (Split mode)

- [ ] **Step 1: Update session.py (E2E mode)**

Replace `from pseudo_llm import pseudo_llm` with:
```python
from actor_bridge import ActorBridge
from config import CHANNEL_SERVER_WS_URL, VOICE_INSTANCE_ID
```

In `__init__`, add bridge:
```python
self._bridge = ActorBridge()
```

In `run()`, connect bridge before doubao:
```python
async def run(self) -> None:
    try:
        await self._bridge.connect(CHANNEL_SERVER_WS_URL, VOICE_INSTANCE_ID)
        await self.doubao.connect()
        ...
```

In `_run_query()`, replace `pseudo_llm(text)` with:
```python
result = await self._bridge.query(text, timeout=60.0)
```

In `_cleanup()`, add:
```python
await self._bridge.close()
```

- [ ] **Step 2: Update session_split.py (Split mode)**

Same pattern: import ActorBridge, create in __init__, connect in run(), replace pseudo_llm call with bridge.query(), close in _cleanup().

- [ ] **Step 3: Verify imports**

```bash
cd /Users/h2oslabs/cc-openclaw-voice/voice_gateway && uv run python -c "from session import Session; from session_split import SplitSession; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/h2oslabs/cc-openclaw-voice
git add voice_gateway/session.py voice_gateway/session_split.py
git commit -m "feat(gateway): replace pseudo_llm with ActorBridge in both modes"
```

---

### Task 6: Integration Test

- [ ] **Step 1: Start channel_server** (must be running for bridge to connect)
- [ ] **Step 2: Start voice gateway** (`make start`)
- [ ] **Step 3: Test E2E mode** — verify bridge connects, comfort text sent, CC response injected
- [ ] **Step 4: Test Split mode** — verify bridge connects, ASR → CC → TTS
- [ ] **Step 5: Check logs** — verify actor spawn, message routing, response delivery
- [ ] **Step 6: Commit any fixes**
