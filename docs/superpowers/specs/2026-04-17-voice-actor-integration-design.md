# Voice Gateway Actor Integration — Design Spec

## Goal

Replace `pseudo_llm` in the voice gateway with a real CC session (Claude Code) via the actor model. Voice gateway remains an independent process, connects to channel_server via WebSocket, and dynamically spawns a paired CC session on registration.

## Two Modes

Both E2E and Split modes use the same `actor_bridge` to communicate with the CC session. The difference is how audio I/O is handled.

### E2E Mode (doubao end-to-end)

```
Browser ↔ Voice Gateway ↔ doubao E2E WS (ASR+TTS)
                ↓ (on ASREnded)
         actor_bridge → channel_server WS → CC Session Actor → Claude Code
                ↑ (response)
         actor_bridge ← channel_server WS ← CC Session Actor
                ↓
         ChatRAGText inject → doubao E2E speaks response
```

- Comfort text sent immediately on ASREnded to suppress E2E's built-in LLM
- CC may take 5-30 seconds. E2E model's `recv_timeout` in config must be extended (e.g., 60s)
- `is_sending_custom_tts` suppresses default LLM text+audio during the wait
- When CC responds, inject via ChatRAGText as before

### Split Mode (separate ASR + TTS)

```
Browser → Voice Gateway → ASR WS → text
                ↓
         actor_bridge → channel_server WS → CC Session Actor → Claude Code
                ↑ (response)
         actor_bridge ← channel_server WS ← CC Session Actor
                ↓
         TTS WS → audio → Browser
```

- No E2E model to suppress, simpler flow
- ASR text sent to CC, response text sent to TTS
- May want a "thinking" indicator on the frontend during CC wait

## Architecture

### Actor Topology (spawned on voice gateway register)

```
voice:user.voice-1  (handler=voice_session, transport=websocket)
    ↕ wired
cc:user.voice-1     (handler=cc_session, transport=websocket, tmux)
```

- Voice actor downstream → [] (no feishu, voice-only)
- CC actor downstream → [voice:user.voice-1]
- CC actor has tmux running Claude Code, connected via its own WS

### Registration Protocol

Voice gateway connects to channel_server WS (same endpoint as CC clients) and sends:

```json
{"action": "register", "instance_id": "voice:user.voice-1", "tag_name": "Voice Session"}
```

CCAdapter detects the `voice:` prefix and:
1. Spawns voice actor: `voice:user.voice-1` (handler=`voice_session`, transport=websocket)
2. Spawns CC actor: `cc:user.voice-1` (handler=`cc_session`, state=suspended)
3. Starts tmux Claude Code process for `cc:user.voice-1`
4. Wires CC downstream → voice actor
5. Responds: `{"action": "registered", "address": "voice:user.voice-1"}`

### Message Flow: Voice → CC → Voice

1. Voice gateway sends over WS:
   ```json
   {"action": "query", "text": "商品四现在有现货吗？"}
   ```

2. VoiceSessionHandler.handle() receives, routes to CC:
   ```python
   cc_addr = actor.metadata["cc_target"]
   return [Send(to=cc_addr, message=Message(sender=actor.address, payload=msg.payload))]
   ```

3. CCSessionHandler.handle() sees external sender, does:
   ```python
   return [TransportSend(payload={...msg.payload, "action": "message"})]
   ```
   This sends the query to Claude Code via its WS.

4. Claude Code processes, replies via WS. CCAdapter routes reply to CC actor.

5. CCSessionHandler.handle() sees internal message (from CC itself), routes to downstream:
   ```python
   return [Send(to=addr, message=reply_msg) for addr in actor.downstream]
   ```
   CC downstream includes `voice:user.voice-1`.

6. VoiceSessionHandler.handle() receives reply, sends to voice gateway:
   ```python
   return [TransportSend(payload={"action": "response", "text": reply_text})]
   ```

7. Voice gateway receives response, injects into E2E (ChatRAGText) or speaks via TTS.

## New Files

### voice_gateway/actor_bridge.py (~80 lines)

WebSocket client that connects to channel_server and provides request-response interface.

```python
class ActorBridge:
    async def connect(url: str, instance_id: str) -> None
        """Connect WS, send register, wait for registered ACK."""

    async def query(text: str, timeout: float = 60.0) -> str
        """Send query, wait for response via asyncio.Future."""

    async def close() -> None
```

- Maintains a single pending Future for request-response
- Sends `{"action": "query", "text": "..."}`, waits for `{"action": "response", "text": "..."}`
- Background reader task processes incoming WS messages, resolves Future on response

### channel_server/core/handlers/voice.py (~40 lines)

VoiceSessionHandler: routes messages between voice gateway and CC session.

```python
class VoiceSessionHandler:
    def handle(actor, msg, runtime):
        if msg.sender == actor.address:
            # From voice gateway: route to CC
            cc_addr = actor.metadata["cc_target"]
            return [Send(to=cc_addr, message=msg)]
        else:
            # From CC (response): send to voice gateway via transport
            return [TransportSend(payload={"action": "response", **msg.payload})]
```

### Changes to Existing Files

**channel_server/adapters/cc/adapter.py**:
- In `_handle_register()`: detect `voice:` prefix, spawn voice actor + CC actor pair
- Wire CC downstream → voice actor
- Register voice actor transport as "websocket" (reuses push_to_cc)

**channel_server/core/handler.py**:
- Register `voice_session` handler in HANDLER_REGISTRY

**voice_gateway/session.py** (E2E mode):
- Replace `pseudo_llm(text)` call with `self._bridge.query(text, timeout=60)`
- Extend `recv_timeout` in START_SESSION_CONFIG to 60s

**voice_gateway/session_split.py** (Split mode):
- Replace `pseudo_llm(text)` call with `self._bridge.query(text, timeout=60)`

**voice_gateway/server.py**:
- On startup: create ActorBridge, connect to channel_server
- Pass bridge to Session/SplitSession constructors

**voice_gateway/config.py**:
- Add `CHANNEL_SERVER_WS_URL` (e.g., `ws://localhost:PORT/ws/cc`)
- Increase `recv_timeout` to 60

## E2E Mode: Extended Wait Handling

Since CC may take much longer than the 2s pseudo_llm:

1. `recv_timeout` in doubao StartSession config extended to 60s
2. Comfort text still sent immediately on ASREnded
3. `is_sending_custom_tts = True` suppresses E2E default LLM for the entire CC wait
4. When CC responds, `ChatRAGText` injects the response
5. If CC times out (60s), fall through to E2E default behavior

## Frontend Changes

- No changes needed for the integration itself
- The mode selector (E2E / Split) works as before
- Optionally: show "Thinking..." indicator when waiting for CC response (future enhancement)

## Out of Scope

- Voice-to-Feishu forwarding (voice sessions don't post to Feishu)
- Multiple concurrent voice sessions per user
- Voice session persistence/resume
- Claude Code system prompt injection (future: pass system_role to Claude Code via CLAUDE.md or session config)
