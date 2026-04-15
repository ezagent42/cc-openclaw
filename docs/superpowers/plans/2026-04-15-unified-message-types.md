# Unified Message Type System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the triple-overloaded `type` field from the actor message system, replacing it with `delivery` (transport semantics), `method` (WebSocket protocol), and `payload["action"]` / `payload["msg_type"]` (content layer).

**Architecture:** Message loses its `type` field, gains `delivery: Delivery`. WebSocket JSON renames `"type"` to `"method"`. Handlers dispatch on `payload["action"]` instead of `payload["command"]` or `msg.type`. Feishu content type stays in `payload["msg_type"]`.

**Tech Stack:** Python 3.12+, dataclasses, str Enum, pytest, pytest-asyncio

**Test command:** `uv run --extra test python3 -m pytest tests/channel_server/ -v`

---

### Task 1: Update Message and add Delivery enum in actor.py

**Files:**
- Modify: `channel_server/core/actor.py`
- Modify: `tests/channel_server/core/test_actor.py`

- [ ] **Step 1: Update test_actor.py — change Message tests to use new signature**

Replace the `TestMessageCreation` class and update `TestActionTypes` to not use `type`:

```python
# In tests/channel_server/core/test_actor.py

# Add to imports:
# from channel_server.core.actor import Delivery

class TestMessageCreation:
    def test_message_creation(self):
        """Message with required fields uses correct defaults."""
        msg = Message(sender="user://alice")
        assert msg.sender == "user://alice"
        assert msg.delivery == Delivery.ONESHOT
        assert msg.payload == {}
        assert msg.metadata == {}

    def test_message_with_payload(self):
        """Message stores payload and metadata correctly."""
        msg = Message(
            sender="bot://bot1",
            delivery=Delivery.STREAM,
            payload={"text": "hello"},
            metadata={"timestamp": "2026-01-01T00:00:00Z"},
        )
        assert msg.delivery == Delivery.STREAM
        assert msg.payload == {"text": "hello"}
        assert msg.metadata == {"timestamp": "2026-01-01T00:00:00Z"}

    def test_delivery_enum_serializes_as_string(self):
        """Delivery enum values serialize as strings for JSON compatibility."""
        assert str(Delivery.ONESHOT) == "Delivery.ONESHOT"
        assert Delivery.ONESHOT.value == "oneshot"
        assert Delivery.STREAM.value == "stream"
        assert Delivery.ONESHOT == "oneshot"  # str Enum comparison
```

Also update `TestActionTypes.test_action_union_isinstance`:

```python
    def test_action_union_isinstance(self):
        """All action types are valid Action instances (type alias check via isinstance)."""
        msg = Message(sender="a://1")
        actions = [
            Send(to="b://2", message=msg),
            TransportSend(payload={}),
            UpdateActor(changes={}),
            SpawnActor(address="c://3", handler="h"),
            StopActor(address="d://4"),
        ]
        assert hasattr(actions[0], "to")
        assert hasattr(actions[1], "payload")
        assert hasattr(actions[2], "changes")
        assert hasattr(actions[3], "address")
        assert hasattr(actions[4], "address")
```

And update `test_send_action`:

```python
    def test_send_action(self):
        msg = Message(sender="a://1")
        action = Send(to="b://2", message=msg)
        assert action.to == "b://2"
        assert action.message is msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_actor.py -v`
Expected: FAIL — `Message` still requires `type` positional arg, `Delivery` not importable

- [ ] **Step 3: Update actor.py — remove type, add Delivery enum and delivery field**

```python
# In channel_server/core/actor.py, add before Transport class:

from enum import Enum

class Delivery(str, Enum):
    """Transport semantics — how a message is delivered.

    ONESHOT — complete content sent in a single message
    STREAM  — streamed in chunks over a persistent connection (e.g., voice)
    """
    ONESHOT = "oneshot"
    STREAM = "stream"
```

Change `Message` to:

```python
@dataclass
class Message:
    """A message routed between actors."""
    sender: str
    delivery: Delivery = Delivery.ONESHOT
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 4: Run test_actor.py to verify it passes**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_actor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/actor.py tests/channel_server/core/test_actor.py
git commit -m "refactor(actor): remove Message.type, add Delivery enum and delivery field"
```

---

### Task 2: Update all handlers in handler.py

**Files:**
- Modify: `channel_server/core/handler.py`
- Modify: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Update test_handler.py — rewrite all tests for new Message signature and action-based dispatch**

Replace `make_msg` helper:

```python
def make_msg(
    sender: str = "actor://sender",
    payload: dict | None = None,
) -> Message:
    return Message(sender=sender, payload=payload or {})
```

Update `test_cc_session_external_message`:

```python
def test_cc_session_external_message():
    actor = make_actor(address="actor://cc", tag="session")
    msg = make_msg(sender="actor://feishu", payload={"text": "hello"})
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], TransportSend)
    assert actions[0].payload["method"] == "message"
    assert actions[0].payload["text"] == "hello"
```

Update `test_cc_session_reply_with_tag` — now uses `action` not `command`:

```python
def test_cc_session_reply_with_tag():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"text": "Hello world"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "actor://feishu1"
    assert action.message.payload["text"] == "[session-1] Hello world"
```

Update `test_cc_session_reply_skips_tag_for_root`:

```python
def test_cc_session_reply_skips_tag_for_root():
    actor = make_actor(
        address="actor://cc",
        tag="root",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"text": "Hello world"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.message.payload["text"] == "Hello world"
```

Update `test_cc_session_forward`:

```python
def test_cc_session_forward():
    actor = make_actor(address="actor://cc", tag="session-1")
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "forward", "target": "actor://other", "text": "forwarded"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "actor://other"
    assert action.message is msg
```

Update `test_cc_session_send_summary`:

```python
def test_cc_session_send_summary():
    actor = make_actor(address="actor://cc", tag="session-1")
    msg = make_msg(
        sender="actor://cc",
        payload={
            "action": "send_summary",
            "parent_feishu": "actor://parent-feishu",
            "text": "Summary text",
        },
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "actor://parent-feishu"
    assert action.message is msg
```

Update `test_cc_session_update_title`:

```python
def test_cc_session_update_title():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1", "actor://feishu2"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "update_title", "title": "New Title"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 2
    for action in actions:
        assert isinstance(action, Send)
    targets = {a.to for a in actions}
    assert targets == {"actor://feishu1", "actor://feishu2"}
```

Update `test_cc_session_unknown_command_returns_empty`:

```python
def test_cc_session_unknown_action_sends_downstream():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "some_unknown_action"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"
```

Update `test_cc_session_send_file` — now sends downstream, not TransportSend:

```python
def test_cc_session_send_file():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "send_file", "chat_id": "c1", "file_path": "/tmp/test.pdf"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"
    assert actions[0].message.payload["action"] == "send_file"
```

Update `test_cc_session_react` and `test_cc_session_react_default_emoji` — now sends downstream:

```python
def test_cc_session_react():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "react", "message_id": "m1", "emoji_type": "HEART"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"
    assert actions[0].message.payload["action"] == "react"
    assert actions[0].message.payload["message_id"] == "m1"


def test_cc_session_react_default_emoji():
    """React action preserves payload as-is (no default injection by handler)."""
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "react", "message_id": "m1"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert actions[0].message.payload["action"] == "react"
```

Add `test_cc_session_tool_notify` — routes to tool_card actor:

```python
def test_cc_session_tool_notify():
    actor = make_actor(
        address="cc:user.dev",
        tag="dev",
        downstream=["feishu:oc_xxx"],
    )
    msg = make_msg(
        sender="cc:user.dev",
        payload={"action": "tool_notify", "text": "Running tests..."},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "tool_card:user.dev"
```

Update `test_tool_card_accumulates_history`:

```python
def test_tool_card_accumulates_history():
    actor = make_actor(metadata={"history": ["a", "b", "c", "d", "e"]})
    msg = make_msg(payload={"text": "f"})
    actions = ToolCardHandler().handle(actor, msg)

    update = next(a for a in actions if isinstance(a, UpdateActor))
    transport = next(a for a in actions if isinstance(a, TransportSend))

    assert update.changes["metadata"]["history"] == ["b", "c", "d", "e", "f"]
    assert transport.payload["action"] == "tool_notify"
    assert "f" in transport.payload["text"]
    assert "a" not in transport.payload["text"]
```

Update `test_admin_system_notification_forward` — uses `payload["msg_type"]`:

```python
def test_admin_system_notification_forward():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root", "feishu:chat1"],
    )
    msg = make_msg(sender="system:runtime", payload={"msg_type": "system", "text": "server online"})
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 2
    targets = {a.to for a in actions}
    assert targets == {"cc:user.root", "feishu:chat1"}
    for action in actions:
        assert isinstance(action, Send)
        assert action.message is msg
```

Update `test_admin_help_command` — AdminHandler help sends Message without type:

```python
def test_admin_help_command():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    msg = make_msg(sender="feishu_user:u1", payload={"text": "/help"})
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "cc:user.root"
    assert "/help" in actions[0].message.payload["text"]
    assert "/spawn" in actions[0].message.payload["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_handler.py -v`
Expected: FAIL — `make_msg` still uses `type`, handlers still use `msg.type`

- [ ] **Step 3: Update handler.py — all handlers use payload-based dispatch**

Full replacement of `channel_server/core/handler.py`:

```python
"""Handler protocol and built-in handler implementations for the actor model."""
from __future__ import annotations

from typing import Protocol

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    TransportSend,
    UpdateActor,
)


class Handler(Protocol):
    """Protocol that all actor message handlers must satisfy."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]: ...


# ---------------------------------------------------------------------------
# FeishuInboundHandler
# ---------------------------------------------------------------------------

class FeishuInboundHandler:
    """Route messages for a Feishu chat/thread actor.

    - Messages from external users (feishu_user:*) -> forward to downstream CC actors.
    - Messages from CC actors (cc:* or others) -> push to Feishu transport (outbound).
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender.startswith("feishu_user:"):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        return [TransportSend(payload=msg.payload)]


# ---------------------------------------------------------------------------
# CCSessionHandler
# ---------------------------------------------------------------------------

class CCSessionHandler:
    """Bridge between actor messages and a Claude Code session.

    - External message (sender != actor.address): push to CC via TransportSend.
    - CC-originated message (sender == actor.address): dispatch by payload["action"].
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender != actor.address:
            # External message -> push to CC over WebSocket.
            # Inject "method": "message" for the WebSocket protocol layer.
            return [TransportSend(payload={
                "method": "message",
                **msg.payload,
            })]

        # CC-originated operation — dispatch on action
        action = msg.payload.get("action")

        if action is None:
            # Default: reply with text, send to downstream
            text = msg.payload.get("text", "")
            if actor.tag != "root":
                text = f"[{actor.tag}] {text}"
            reply_msg = Message(sender=actor.address, payload={**msg.payload, "text": text})
            return [Send(to=addr, message=reply_msg) for addr in actor.downstream]

        if action == "forward":
            target = msg.payload.get("target", "")
            return [Send(to=target, message=msg)]

        if action == "send_summary":
            parent_feishu = msg.payload.get("parent_feishu", "")
            return [Send(to=parent_feishu, message=msg)]

        if action == "tool_notify":
            # Route to tool_card actor, not downstream feishu actor
            user_session = actor.address.replace("cc:", "")
            tool_card_addr = f"tool_card:{user_session}"
            return [Send(to=tool_card_addr, message=msg)]

        # react, send_file, update_title, etc. -> send to downstream feishu actor
        return [Send(to=addr, message=msg) for addr in actor.downstream]


# ---------------------------------------------------------------------------
# ForwardAllHandler
# ---------------------------------------------------------------------------

class ForwardAllHandler:
    """Broadcast every message to all downstream actors."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]


# ---------------------------------------------------------------------------
# ToolCardHandler
# ---------------------------------------------------------------------------

class ToolCardHandler:
    """Accumulate a rolling history window (max 5) and emit a tool-card update."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "")
        history: list[str] = list(actor.metadata.get("history", []))
        history.append(text)
        if len(history) > 5:
            history = history[-5:]
        display = "\n".join(history)
        return [
            UpdateActor(changes={"metadata": {"history": history}}),
            TransportSend(payload={"action": "tool_notify", "text": display}),
        ]


# ---------------------------------------------------------------------------
# AdminHandler
# ---------------------------------------------------------------------------

class AdminHandler:
    """Handles admin commands and notifications.

    - System notifications (msg_type == "system") are forwarded downstream.
    - Session commands (/spawn, /kill, /sessions) pass through to downstream.
    - Non-slash messages pass through to downstream.
    - /help shows available commands.
    - Unknown slash commands get an error message.
    """

    SESSION_COMMANDS = ("/spawn", "/kill", "/sessions")

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "").strip()

        # System notifications -> forward downstream
        if msg.payload.get("msg_type") == "system":
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Session commands -> pass through to downstream CC actor
        if text.startswith(self.SESSION_COMMANDS):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Non-slash messages -> forward to downstream
        if not text.startswith("/"):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Admin commands
        if text == "/help":
            return [
                Send(
                    to=addr,
                    message=Message(
                        sender=actor.address,
                        payload={"text": self._help_text()},
                    ),
                )
                for addr in actor.downstream
            ]

        # Unknown slash command
        cmd = text.split()[0]
        return [
            Send(
                to=addr,
                message=Message(
                    sender=actor.address,
                    payload={"text": f"\u672a\u77e5\u547d\u4ee4: {cmd}\n\u53d1\u9001 /help \u67e5\u770b\u53ef\u7528\u547d\u4ee4"},
                ),
            )
            for addr in actor.downstream
        ]

    @staticmethod
    def _help_text() -> str:
        return (
            "\u53ef\u7528\u547d\u4ee4:\n"
            "/help \u2014 \u663e\u793a\u5e2e\u52a9\n"
            "/spawn <name> \u2014 \u521b\u5efa\u5b50 session\n"
            "/kill <name> \u2014 \u7ed3\u675f\u5b50 session\n"
            "/sessions \u2014 \u5217\u51fa\u6d3b\u8dc3 sessions"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
    "admin": AdminHandler(),
}


def get_handler(name: str) -> Handler:
    """Look up a handler by name. Raises ValueError if not found."""
    handler = HANDLER_REGISTRY.get(name)
    if handler is None:
        raise ValueError(f"Unknown handler: {name}")
    return handler
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_handler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/handler.py tests/channel_server/core/test_handler.py
git commit -m "refactor(handler): dispatch on payload action instead of msg.type"
```

---

### Task 3: Update runtime.py — log format and error messages

**Files:**
- Modify: `channel_server/core/runtime.py`
- Modify: `tests/channel_server/core/test_runtime.py`

- [ ] **Step 1: Update test_runtime.py — fix all Message() calls**

Every `Message(sender=..., type=...)` must become `Message(sender=...)` with relevant info in payload instead. Apply these changes:

In `test_send_delivers_to_mailbox`:
```python
def test_send_delivers_to_mailbox():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    msg = Message(sender="actor://b", payload={"text": "hi"})
    rt.send("actor://a", msg)
    assert not rt.mailboxes["actor://a"].empty()
    queued = rt.mailboxes["actor://a"].get_nowait()
    assert queued is msg
```

In `test_send_to_nonexistent_no_crash`:
```python
def test_send_to_nonexistent_no_crash():
    rt = make_runtime()
    msg = Message(sender="actor://b")
    rt.send("actor://nowhere", msg)
```

In `test_send_to_ended_actor_no_crash`:
```python
def test_send_to_ended_actor_no_crash():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    rt.stop("actor://a")
    msg = Message(sender="actor://b")
    rt.send("actor://a", msg)
```

In `test_actor_loop_forwards_messages`:
```python
    msg = Message(sender="actor://ext", payload={"text": "hello"})
```

And update the assertion:
```python
    assert received[0]["action"] == "tool_notify"
```

In `test_actor_loop_handles_handler_error` and `test_actor_loop_ends_after_max_errors` and `test_handler_error_notifies_parent` and `test_max_errors_stops_actor`, change all:
```python
Message(sender="actor://ext", type="chat")
```
to:
```python
Message(sender="actor://ext")
```

And for error type assertions, change:
```python
assert received[0].type == "error"
```
to check payload instead:
```python
assert received[0].payload.get("msg_type") == "error"
```

In `test_transport_send_dispatches`:
```python
    msg = Message(sender="actor://ext", payload={"text": "ping"})
```

And update the assertion:
```python
    assert sent_payloads[0]["action"] == "tool_notify"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_runtime.py -v`
Expected: FAIL — runtime still logs `msg.type`, error messages still use `type="error"`

- [ ] **Step 3: Update runtime.py — log format and error message construction**

In `_actor_loop`, change the log line (line 153):
```python
                    action_label = msg.payload.get("action") or msg.payload.get("msg_type", "message")
                    log.info("Actor %s processing msg from %s action=%s", actor.address, msg.sender, action_label)
```

Change the error notification (line 170-176):
```python
                    if actor.parent:
                        self.send(
                            actor.parent,
                            Message(
                                sender=actor.address,
                                payload={"msg_type": "error", "error": str(e)},
                            ),
                        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test python3 -m pytest tests/channel_server/core/test_runtime.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/runtime.py tests/channel_server/core/test_runtime.py
git commit -m "refactor(runtime): update log format and error messages for typeless Message"
```

---

### Task 4: Update CC adapter — method-based WebSocket protocol

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`
- Modify: `tests/channel_server/adapters/test_cc_adapter.py`

- [ ] **Step 1: Update test_cc_adapter.py — change all "type" to "method" in WS messages**

In `test_handle_register_attaches_transport`:
```python
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
        "tag_name": "root",
    })
    # ...
    ack = json.loads(ws.send.call_args[0][0])
    assert ack["method"] == "registered"
    assert ack["address"] == "cc:alice.root"
```

In `test_handle_reply_sends_to_actor`:
```python
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })

    await adapter.handle_message(ws, {
        "method": "reply",
        "chat_id": "oc_abc",
        "text": "hello world",
    })

    mailbox = rt.mailboxes.get("cc:alice.root")
    assert mailbox is not None
    assert not mailbox.empty()
    msg = mailbox.get_nowait()
    assert isinstance(msg, Message)
    assert msg.payload.get("text") == "hello world"
    # reply has no action (default = send text)
    assert msg.payload.get("action") is None
```

In `test_handle_register_auto_spawns`:
```python
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "bob.root",
        "tag_name": "root",
    })
```

In `test_handle_register_rejects_missing_instance_id`:
```python
    await adapter._handle_register(ws, {"method": "register"})
    ws.send.assert_called_once()
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "error"
```

In `test_handle_list_returns_sessions`:
```python
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })
    # ...
    await adapter._handle_list(ws, {"method": "list_sessions"})
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "sessions_list"
```

In `test_handle_message_ignores_pong`:
```python
    await adapter.handle_message(ws, {"method": "pong"})
```

In `test_push_to_cc_sends_via_ws`:
```python
    payload = {"method": "message", "text": "incoming from feishu", "chat_id": "oc_abc"}
    adapter.push_to_cc(actor, payload)
    # ...
    sent = json.loads(ws.send.call_args[0][0])
    assert sent["method"] == "message"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test python3 -m pytest tests/channel_server/adapters/test_cc_adapter.py -v`
Expected: FAIL — adapter still uses `msg.get("type")`

- [ ] **Step 3: Update adapter.py — method-based dispatch and action translation**

In `handle_message`, change `msg.get("type")` to `msg.get("method")`:

```python
    async def handle_message(self, ws, msg: dict) -> None:
        """Route incoming WS messages by method."""
        method = msg.get("method", "")

        if method == "register":
            await self._handle_register(ws, msg)
        elif method in ("reply", "forward", "send_summary", "update_title",
                          "send_file", "react", "tool_notify"):
            self._route_to_actor(ws, msg)
        elif method == "spawn_session":
            await self._handle_spawn(ws, msg)
        elif method == "kill_session":
            await self._handle_kill(ws, msg)
        elif method == "list_sessions":
            await self._handle_list(ws, msg)
        elif method == "pong":
            pass
        else:
            log.debug("Unknown WS method: %s", method)
```

In `_handle_register`, change all `"type"` keys to `"method"`:

```python
        await ws.send(json.dumps({"method": "error", "message": "Missing instance_id"}))
        # ...
        await ws.send(json.dumps({"method": "registered", "address": address}))
```

In `_route_to_actor`, translate `method` to `action`:

```python
    def _route_to_actor(self, ws, msg: dict) -> None:
        """Convert a WS message to an actor Message and send to the CC actor."""
        address = self._ws_to_address.get(id(ws))
        if not address:
            log.warning("_route_to_actor: unregistered WebSocket")
            return

        method = msg.get("method", "")
        payload = {k: v for k, v in msg.items() if k != "method"}

        # --- action (transport-specific operation) ---
        # reply -> no action (default = send text)
        # all other methods -> set as action
        if method != "reply":
            payload["action"] = method

        log.info("CC message from %s: method=%s text=%s", address, method, str(payload.get("text", ""))[:60])

        # --- addressing (routing metadata) ---
        # send_summary needs parent_feishu injected for handler routing
        if method == "send_summary":
            actor = self.runtime.lookup(address)
            if actor and actor.parent:
                parent = self.runtime.lookup(actor.parent)
                if parent:
                    parent_feishu = next(
                        (d for d in parent.downstream if d.startswith("feishu:")), ""
                    )
                    if parent_feishu:
                        payload["parent_feishu"] = parent_feishu

        actor_msg = Message(sender=address, payload=payload)
        self.runtime.send(address, actor_msg)
```

In `_handle_spawn`, `_handle_kill`, `_handle_list` — change all response `"type"` to `"method"`:

Search and replace in these methods:
- `{"type": "error"` → `{"method": "error"`
- `{"type": "spawn_result"` → `{"method": "spawn_result"`
- `{"type": "kill_result"` → `{"method": "kill_result"`
- `{"type": "sessions_list"` → `{"method": "sessions_list"`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test python3 -m pytest tests/channel_server/adapters/test_cc_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/cc/adapter.py tests/channel_server/adapters/test_cc_adapter.py
git commit -m "refactor(cc-adapter): use method-based WS protocol, translate to payload action"
```

---

### Task 5: Update Feishu adapter — payload fields and transport dispatch

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`
- Modify: `tests/channel_server/adapters/test_feishu_adapter.py`

- [ ] **Step 1: Update test_feishu_adapter.py — Message without type, payload with msg_type**

In `test_on_feishu_event_sends_message`:
```python
def test_on_feishu_event_sends_message():
    adapter, rt = make_adapter()
    evt = feishu_event()
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    mailbox = rt.mailboxes.get(addr)
    assert mailbox is not None
    assert not mailbox.empty()
    msg = mailbox.get_nowait()
    assert isinstance(msg, Message)
    # Content fields in payload
    assert msg.payload["text"] == "hello"
    assert msg.payload["msg_type"] == "text"
    assert msg.payload["file_path"] == ""
    assert msg.payload["chat_id"] == "oc_abc123"
    assert msg.payload["message_id"] == "msg_001"
    # Metadata preserved
    assert msg.metadata["user"] == "Alice"
    assert msg.metadata["user_id"] == "ou_alice"
```

Add a test for file_path in payload:
```python
def test_on_feishu_event_includes_file_path():
    adapter, rt = make_adapter()
    evt = feishu_event(msg_type="image")
    evt["file_path"] = "/tmp/downloads/photo.png"
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    msg = rt.mailboxes[addr].get_nowait()
    assert msg.payload["msg_type"] == "image"
    assert msg.payload["file_path"] == "/tmp/downloads/photo.png"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test python3 -m pytest tests/channel_server/adapters/test_feishu_adapter.py -v`
Expected: FAIL — `msg.type` assertions fail, payload missing `msg_type`/`file_path`

- [ ] **Step 3: Update feishu adapter.py — payload construction and transport dispatch**

In `on_feishu_event`, change the Message construction (around line 226-238):

```python
        msg = Message(
            sender=f"feishu_user:{user_id}" if user_id else "feishu_user:unknown",
            payload={
                # --- content (message body) ---
                "text": text,
                "file_path": event.get("file_path", ""),
                # --- addressing (routing metadata) ---
                "chat_id": chat_id,
                "message_id": message_id,
                # --- discriminator (content format) ---
                "msg_type": msg_type,
            },
            metadata={
                "user": user,
                "user_id": user_id,
                "message_id": message_id,
                "chat_id": chat_id,
                "root_id": root_id or "",
                "msg_type": msg_type,
            },
        )
```

In `_handle_chat_transport`, change dispatch from `payload.get("type", "text")` to `payload.get("action")`:

```python
    def _handle_chat_transport(self, actor: Actor, payload: dict) -> None:
        action = payload.get("action")
        chat_id = actor.transport.config["chat_id"] if actor.transport else ""

        # Remove ACK reaction for the last inbound message in this chat
        last_msg = self._last_msg_id.pop(chat_id, "")
        if last_msg:
            threading.Thread(target=self._remove_reaction, args=(last_msg,), daemon=True).start()

        if action is None:
            # Default: send text message
            text = payload.get("text", "")
            threading.Thread(target=self._send_message, args=(chat_id, text, None), daemon=True).start()
        elif action == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(target=self._send_reaction, args=(message_id, emoji_type), daemon=True).start()
        elif action == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(target=self._send_file, args=(payload.get("chat_id", chat_id), file_path), daemon=True).start()
        elif action == "tool_notify":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(target=self._update_card, args=(msg_id, text), daemon=True).start()
        else:
            log.warning("_handle_chat_transport: unhandled action=%s actor=%s", action, actor.address)
```

Apply the same pattern to `_handle_thread_transport`:

```python
    def _handle_thread_transport(self, actor: Actor, payload: dict) -> None:
        config = actor.transport.config if actor.transport else {}
        chat_id = config.get("chat_id", "")
        root_id = config.get("root_id", "")
        action = payload.get("action")

        last_msg = self._last_msg_id.pop(chat_id, "")
        if last_msg:
            threading.Thread(target=self._remove_reaction, args=(last_msg,), daemon=True).start()

        if action is None:
            text = payload.get("text", "")
            threading.Thread(target=self._send_message, args=(chat_id, text, root_id), daemon=True).start()
        elif action == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(target=self._send_reaction, args=(message_id, emoji_type), daemon=True).start()
        elif action == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(target=self._send_file, args=(payload.get("chat_id", chat_id), file_path), daemon=True).start()
        elif action == "update_title":
            msg_id = payload.get("msg_id", "")
            title = payload.get("title", "")
            threading.Thread(target=self._update_anchor_card, args=(msg_id, title), daemon=True).start()
        elif action == "tool_notify":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(target=self._update_card, args=(msg_id, text), daemon=True).start()
        else:
            log.warning("_handle_thread_transport: unhandled action=%s actor=%s", action, actor.address)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test python3 -m pytest tests/channel_server/adapters/test_feishu_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/feishu/adapter.py tests/channel_server/adapters/test_feishu_adapter.py
git commit -m "refactor(feishu-adapter): add msg_type/file_path to payload, dispatch on action"
```

---

### Task 6: Update channel.py — method-based WS protocol on client side

**Files:**
- Modify: `channel_server/adapters/cc/channel.py`

- [ ] **Step 1: Update _message_loop — replace all "type" with "method"**

```python
    async def _message_loop(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method")
            if method == "message":
                await self._message_queue.put(msg)
            elif method == "forwarded_message":
                from_id = msg.get("from", "unknown")
                text = msg.get("text", "")
                await self._message_queue.put({
                    "method": "message",
                    "text": f"[from {from_id}] {text}",
                    "user": from_id,
                    "user_id": from_id,
                    "chat_id": "internal",
                    "source": "forward",
                    "ts": datetime.now(tz=timezone.utc).isoformat(),
                })
            elif method in ("spawn_result", "kill_result", "sessions_list"):
                await self._message_queue.put({
                    "method": "message",
                    "text": msg.get("text", json.dumps(msg)),
                    "user": "channel-server",
                    "user_id": "system",
                    "chat_id": "internal",
                    "source": "system",
                    "ts": datetime.now(tz=timezone.utc).isoformat(),
                })
            elif method == "ping":
                await ws.send(json.dumps({"method": "pong"}))
            elif method == "error":
                log.error(f"Server error: {msg}")
            else:
                log.warning(f"_message_loop: unhandled method={method!r} keys={list(msg.keys())}")
```

- [ ] **Step 2: Update _register — check "method" not "type"**

```python
    async def _register(self, ws):
        payload = {
            "method": "register",
            "role": "developer" if ("*" in self.chat_ids or not self.chat_ids) else "production",
            "chat_ids": self.chat_ids,
            "instance_id": self.instance_id,
            "runtime_mode": self.runtime_mode,
        }
        if self.tag_name:
            payload["tag_name"] = self.tag_name
        await ws.send(json.dumps(payload))
        resp = json.loads(await ws.recv())
        if resp.get("method") == "error":
            log.error(f"Registration failed: {resp}")
            raise RuntimeError(resp.get("message", "Registration failed"))
        log.info(f"Registered with channel-server: chat_ids={self.chat_ids}")
```

- [ ] **Step 3: Update all send methods — "type" to "method"**

```python
    async def send_reply(self, chat_id, text):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "reply", "chat_id": chat_id, "text": text,
            }))

    async def send_react(self, message_id, emoji_type):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "react", "message_id": message_id, "emoji_type": emoji_type,
            }))

    async def send_file(self, chat_id, file_path):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "send_file", "chat_id": chat_id, "file_path": file_path,
            }))

    async def send_forward(self, target_instance, text):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "forward", "target_instance": target_instance, "text": text,
            }))

    async def send_summary(self, text):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "send_summary", "text": text,
            }))

    async def update_title(self, title):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "update_title", "title": title,
            }))

    async def send_spawn(self, session_name, tag=""):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "spawn_session", "session_name": session_name, "tag": tag or session_name,
            }))

    async def send_kill(self, session_name):
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "kill_session", "session_name": session_name,
            }))

    async def send_list_sessions(self):
        if self.ws:
            await self.ws.send(json.dumps({"method": "list_sessions"}))
```

- [ ] **Step 4: Update inject_message — read "method" for type field in notification**

Find the `inject_message` function and update it to read `method` from the message dict instead of `type`. Keep the MCP notification key as-is (that's the MCP protocol, not our internal protocol).

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/cc/channel.py
git commit -m "refactor(channel): rename WS type to method on client side"
```

---

### Task 7: Update integration tests

**Files:**
- Modify: `tests/channel_server/test_integration.py`

- [ ] **Step 1: Update all Message() calls and assertions**

In `test_end_to_end_feishu_to_cc`:
```python
    runtime.send(
        "feishu:oc_xxx",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "hello", "chat_id": "oc_xxx", "message_id": "om_1", "file_path": ""},
            metadata={"chat_id": "oc_xxx", "user": "testuser"},
        ),
    )
    await asyncio.sleep(0.2)

    assert len(transport_log) == 1
    assert transport_log[0]["text"] == "hello"
    assert transport_log[0]["method"] == "message"
```

In `test_end_to_end_cc_reply`:
```python
    runtime.send(
        "cc:linyilun.root",
        Message(
            sender="cc:linyilun.root",
            payload={"text": "world"},
        ),
    )
```

In `test_round_trip`, update the mock_ws to use new format:
```python
    def mock_ws(actor, payload):
        ws_log.append(payload)
        runtime.send(
            actor.address,
            Message(
                sender=actor.address,
                payload={"text": f"echo: {payload.get('text', '')}"},
            ),
        )
```

And the initial message:
```python
    runtime.send(
        "feishu:oc_round",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "ping", "chat_id": "oc_round", "message_id": "om_1", "file_path": ""},
            metadata={"chat_id": "oc_round"},
        ),
    )
```

In `test_suspended_actor_no_processing`:
```python
    runtime.send(
        "feishu:oc_sus",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "queued", "chat_id": "oc_sus", "message_id": "om_1", "file_path": ""},
        ),
    )
```

- [ ] **Step 2: Run all channel_server tests**

Run: `uv run --extra test python3 -m pytest tests/channel_server/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/channel_server/test_integration.py
git commit -m "refactor(tests): update integration tests for unified message types"
```

---

### Task 8: Restart channel server and smoke test

**Files:** None (operational verification)

- [ ] **Step 1: Restart the channel server**

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.channel-server
sleep 3
tail -10 ~/.openclaw/logs/channel-server.err.log
```

Expected: Server starts, Feishu adapter initializes, no import errors.

- [ ] **Step 2: Restart linyilun.root session**

```bash
# Kill old session that still uses the old protocol
tmux kill-window -t cc-openclaw:linyilun.root 2>/dev/null
./cc-openclaw.sh --user linyilun
```

Expected: Session starts, MCP channel connects, `Attached transport to existing CC actor: cc:linyilun.root` in logs.

- [ ] **Step 3: Send a test message from Feishu and verify round-trip**

Send "测试" from Feishu. Check logs:

```bash
tail -20 ~/.openclaw/logs/channel-server.err.log
```

Expected log lines:
```
Actor feishu:oc_xxx processing msg from feishu_user:ou_xxx action=text
Actor system:admin processing msg from feishu_user:ou_xxx action=text
Actor cc:linyilun.root processing msg from feishu_user:ou_xxx action=text
```

And the reply path should show action-based routing with no warnings.

- [ ] **Step 4: Commit if any fixups were needed**

```bash
git add -A && git commit -m "fix: address smoke test findings" || echo "No fixups needed"
```
