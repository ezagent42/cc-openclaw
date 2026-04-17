# Tool Card Simplification — Direct Message per Tool Notification

**Date:** 2026-04-17
**Status:** Draft

## Problem

Tool cards are created once at session spawn time as interactive cards, then updated in-place via a dedicated `tool_card:*` actor. This creates a static card that only updates when hooks fire, and shows stale content between rounds.

The desired behavior: each tool notification (PreToolUse/PostToolUse) is a simple text message in the conversation, showing real-time tool activity. No persistent card, no in-place editing.

## Goals

1. Each `tool_notify` from a CC hook becomes a text message in the correct feishu conversation (main chat for root, thread for child sessions).
2. Remove the `tool_card:*` actor and `ToolCardHandler` — no longer needed.
3. Remove `on_spawn` tool card creation from both `FeishuInboundHandler` and `SessionMgrHandler.init_session`.
4. All tool_notify messages flow through the actor model: hook → CC adapter → cc actor → downstream feishu actor → feishu transport.

## Design

### Message Flow

```
Hook process (notify-channel.sh)
  → anonymous WebSocket → CC adapter
  → _route_anonymous_tool_notify finds cc:user.session actor
  → runtime.send(cc_actor, Message(action="tool_notify", text="⚙️ Running: git status"))
  → CCSessionHandler.handle routes to downstream feishu actors (same as reply/send_file)
  → FeishuInboundHandler._handle_outbound sends text to feishu
  → Message appears in correct chat/thread
```

### Changes

**1. CCSessionHandler (`core/handlers/cc.py`)**

Change `tool_notify` routing from sending to `tool_card:*` actor to sending to downstream feishu actors (same catch-all path as reply/send_file/react):

```python
# Before:
if action == "tool_notify":
    user_session = actor.address.removeprefix("cc:")
    return [Send(to=f"tool_card:{user_session}", message=msg)]

# After:
if action == "tool_notify":
    return [Send(to=addr, message=msg) for addr in actor.downstream]
```

**2. CC adapter `_route_anonymous_tool_notify` (`adapters/cc/adapter.py`)**

Currently finds a `tool_card:*` actor by matching `chat_id`. Change to find the `cc:*` actor instead (by matching chat_id in downstream feishu actor's transport config), then send the message to that cc actor. The cc actor's handler will route it to downstream feishu actors.

```python
def _route_anonymous_tool_notify(self, msg: dict) -> None:
    chat_id = msg.get("chat_id", "")
    text = msg.get("text", "")
    if not chat_id or not text:
        return

    # Find cc actor that has this chat_id in its downstream feishu actor
    for addr, actor in self.runtime.actors.items():
        if not addr.startswith("cc:") or actor.state == "ended":
            continue
        for ds_addr in actor.downstream:
            ds = self.runtime.lookup(ds_addr)
            if (ds and ds.transport
                    and ds.transport.type in ("feishu_chat", "feishu_thread")
                    and ds.transport.config.get("chat_id") == chat_id):
                self.runtime.send(addr, Message(
                    sender="hook:tool_notify",
                    payload={"action": "tool_notify", "text": text},
                ))
                return
```

**3. Remove tool card creation from on_spawn**

- `FeishuInboundHandler.on_spawn` (`core/handlers/feishu.py`): Remove the `create_tool_card` TransportSend action. Keep `create_thread_anchor`.
- `SessionMgrHandler._handle_init` (`core/handlers/session_mgr.py`): Remove the SpawnActor for `tool_card:user.root` in root init. Return `[]` for root mode (root doesn't need a thread anchor either).

**4. Remove ToolCardHandler and tool_card actor**

- Delete: `channel_server/core/handlers/tool_card.py`
- Remove from `channel_server/core/handler.py` HANDLER_REGISTRY
- Remove from `channel_server/core/handlers/__init__.py`
- `CCSessionHandler.on_stop`: Remove `StopActor(address=f"tool_card:{user_session}")` — actor no longer exists.
- `feishu_adapter._handle_chat_transport` and `_handle_thread_transport`: Remove `tool_notify` action handling (card update via `_update_card`). The feishu transport handlers no longer need to know about tool_notify — they just send text messages.

**5. CCSessionHandler must strip `action` before forwarding to feishu**

When tool_notify is forwarded to downstream feishu actors, the payload contains `{"action": "tool_notify", "text": "..."}`. The feishu transport handler dispatches by `action` field — with `tool_notify` removed from the transport handler, this would hit the "unhandled action" warning. Fix: CCSessionHandler strips the `action` key and sends only `{"text": "..."}` to downstream, so the feishu transport handler treats it as a plain text message (action=None path).

```python
# In CCSessionHandler.handle:
if action == "tool_notify":
    text_msg = Message(sender=msg.sender, payload={"text": msg.payload.get("text", "")})
    return [Send(to=addr, message=text_msg) for addr in actor.downstream]
```

**6. Remove dead code from feishu adapter**

After removing tool_card actor, these methods become dead code:
- `create_tool_card()` — no longer called from anywhere
- `_build_tool_card()` — only used by create_tool_card
- `_update_card()` — only called by tool_notify transport handler (being removed)

Remove all three.

**7. Remove tool_card spawn from CC adapter legacy spawn path**

`adapters/cc/adapter.py` lines ~418-433 still have legacy code that spawns `tool_card:*` actors during the old MCP-driven spawn flow (inside `_handle_spawn` which was supposed to be deleted). Verify this code is gone (it should have been removed in the session-mgr refactoring). If any references remain, remove them.

**8. Hook stays unchanged**

`notify-channel.sh` continues to send `{"action": "tool_notify", "chat_id": "...", "text": "..."}` via anonymous WebSocket. No changes needed.

## What Gets Removed

- `channel_server/core/handlers/tool_card.py` (entire file)
- `tool_card` from HANDLER_REGISTRY and `__init__.py`
- `tool_card:*` actor spawning (on_spawn, init_session)
- `StopActor(tool_card:*)` from CCSessionHandler.on_stop
- `tool_notify` case in feishu transport handlers (`_handle_chat_transport`, `_handle_thread_transport`)
- `create_tool_card` TransportSend from FeishuInboundHandler.on_spawn
- Dead code: `create_tool_card()`, `_build_tool_card()`, `_update_card()` from feishu adapter
- Any remaining tool_card references in CC adapter

## Testing

- Update `test_handler.py`: Remove ToolCardHandler tests, update CCSessionHandler tool_notify test to verify routing to downstream as text
- Update `test_session_mgr.py`: Update `test_init_session_root` — _handle_init now returns `[]` for root mode
- Update `test_cc_session_on_stop_stops_children`: Remove tool_card from expected StopActor addresses
- Integration: Verify tool_notify messages appear in feishu thread as text
