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

**5. FeishuInboundHandler outbound handling**

The tool_notify message arrives as a regular outbound message via `_handle_outbound`. The existing logic already sends `msg.payload` via `TransportSend`. The feishu transport handler sees `text` in payload and sends it as a text message. This should work without changes — verify in testing.

**6. Hook stays unchanged**

`notify-channel.sh` continues to send `{"action": "tool_notify", "chat_id": "...", "text": "..."}` via anonymous WebSocket. No changes needed.

## What Gets Removed

- `channel_server/core/handlers/tool_card.py` (entire file)
- `tool_card` from HANDLER_REGISTRY
- `tool_card:*` actor spawning (on_spawn, init_session)
- `StopActor(tool_card:*)` from CCSessionHandler.on_stop
- `tool_notify` case in feishu transport handlers
- `create_tool_card` TransportSend from FeishuInboundHandler.on_spawn

## Testing

- Update `test_handler.py`: Remove ToolCardHandler tests, update CCSessionHandler tool_notify test to verify routing to downstream
- Update `test_session_mgr.py`: Update init_session test (no more tool_card spawn)
- Integration: Verify tool_notify messages appear in feishu thread as text
