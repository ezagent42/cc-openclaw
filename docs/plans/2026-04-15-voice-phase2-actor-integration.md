# Voice Phase 2 — Actor Model Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the validated voice pipeline (Phase 1) into the channel-server actor model, so voice sessions participate as `voice:<user>` actors alongside CC and Feishu actors. Claude replies are delivered via TTS, and ASR transcripts are synced to Feishu.

**Prerequisites:**
- Phase 1 complete: LiveKit echo agent, token server, and Feishu webapp all working
- Actor-model channel server (`feature/actor-model`) merged or stable
- Existing files from Phase 1: `voice/agent.py`, `voice/config.py`, `voice/token_server.py`

**Architecture:** The voice agent registers as a `voice:<user>` actor via WebSocket to the channel-server runtime. A `VoiceSessionHandler` routes ASR transcripts to downstream CC actors and forwards CC replies back to the voice agent for TTS. The voice agent code moves from `voice/` to `channel_server/adapters/voice/`.

**Branch:** `feature/voice-middleware` (rebased on `feature/actor-model` after merge)

---

## File Structure

```
channel_server/
  core/
    handler.py                     — ADD VoiceSessionHandler
  adapters/
    voice/
      __init__.py
      agent.py                     — Migrate from voice/agent.py, add ChannelBridge
      config.py                    — Migrate from voice/config.py, add channel_ws_url
      token_server.py              — Migrate from voice/token_server.py (unchanged)
    cc/
      adapter.py                   — MODIFY: voice: prefix detection + transcript routing

tests/
  channel_server/
    core/
      test_handler.py              — ADD VoiceSessionHandler tests
    adapters/
      test_cc_adapter.py           — ADD voice registration test
    test_voice_integration.py      — Runtime-level voice ↔ CC routing tests
```

---

### Task 1: VoiceSessionHandler (`channel_server/core/handler.py`)

**Files:**
- Modify: `channel_server/core/handler.py`
- Test: `tests/channel_server/core/test_handler.py`

Handler for `voice:<user>` actors:
- External messages (from CC) → TransportSend to voice agent (TTS playback)
- Own messages with `command: "transcript"` → Send to downstream CC actors
- Own messages with `command: "transcript_sync"` → Send to specific Feishu actor
- Own messages with `command: "forward"` → Send to target actor

Refer to the full handler code, tests, and registry update in `docs/plans/2026-04-15-voice-middleware-design.md` Task 1.

---

### Task 2: Migrate Voice Modules to channel_server/adapters/voice/

**Files:**
- Move: `voice/config.py` → `channel_server/adapters/voice/config.py`
- Move: `voice/token_server.py` → `channel_server/adapters/voice/token_server.py`
- Move: `voice/agent.py` → `channel_server/adapters/voice/agent.py`
- Create: `channel_server/adapters/voice/__init__.py`

Changes to config.py:
- Add `channel_ws_url: str` as required field
- Update import paths

Changes to agent.py:
- Replace `EchoAgent` with `ChannelBridge` + `VoiceBridgeAgent`
- Agent no longer generates its own replies — instead forwards transcripts to channel server
- Claude replies arrive via `bridge.on_reply` → `session.say(text)`
- See `docs/plans/2026-04-15-voice-middleware-design.md` Task 3 for full code

---

### Task 3: CC Adapter Modifications (`channel_server/adapters/cc/adapter.py`)

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`
- Test: `tests/channel_server/adapters/test_cc_adapter.py`

Two changes:
1. `_handle_register`: detect `voice:` prefix → use `voice_session` handler
2. `handle_message`: add `"transcript"` to routable message types

See `docs/plans/2026-04-15-voice-middleware-design.md` Task 4 for full code and tests.

---

### Task 4: Integration Tests

**Files:**
- Create: `tests/channel_server/test_voice_integration.py`

Runtime-level tests that verify message flow through actor mailboxes:
1. Voice transcript → runtime.send → CC actor receives via TransportSend
2. CC reply → voice actor → TransportSend (for TTS)

See `docs/plans/2026-04-15-voice-middleware-design.md` Task 8 for full test code.

---

### Task 5: End-to-End Validation

- [ ] **Step 1:** Start channel server with actor runtime
- [ ] **Step 2:** Start voice agent with `CHANNEL_WS_URL` pointing to channel server
- [ ] **Step 3:** Open Feishu webapp, start voice call
- [ ] **Step 4:** Speak → verify transcript reaches CC session (Claude)
- [ ] **Step 5:** Verify Claude's reply plays back via TTS
- [ ] **Step 6:** Verify transcript synced to Feishu as `[语音]` text message
- [ ] **Step 7:** Test barge-in during Claude reply

---

## Summary

| Task | What | Depends on |
|------|------|------------|
| 1 | VoiceSessionHandler | Actor model channel server ready |
| 2 | Migrate voice/ → channel_server/adapters/voice/ | Phase 1 code validated |
| 3 | CC adapter voice support | Task 1 |
| 4 | Integration tests | Tasks 1-3 |
| 5 | End-to-end validation | All above |

## Notes

- The detailed code for Tasks 1, 3, and 4 is in `docs/plans/2026-04-15-voice-middleware-design.md` (the original design doc). This plan references it to avoid duplication.
- Phase 1 code lives in `voice/` (standalone). Phase 2 migrates it to `channel_server/adapters/voice/` with the ChannelBridge additions.
- After Phase 2, the standalone `voice/` directory can be removed or kept as a development tool.
