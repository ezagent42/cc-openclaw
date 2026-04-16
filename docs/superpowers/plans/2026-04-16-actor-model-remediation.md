# Actor Model Full Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate all actor model violations — shared mutable state, side effects outside handlers, blocking API calls — so adapters are stateless transport, handlers own all business logic, and Feishu API is fully async.

**Architecture:** Handlers move from a single handler.py into handlers/ directory. FeishuInboundHandler absorbs ACK reaction management, echo prevention, and dedup from FeishuAdapter. Runtime gains async _execute, TransportSend return values, wire() method, and message-level dedup. All Feishu API calls switch to async SDK (acreate/areply/apatch/arequest).

**Tech Stack:** Python 3.14, lark_oapi (async SDK), asyncio, pytest, websockets

**Spec:** `docs/superpowers/specs/2026-04-16-actor-model-remediation-design.md`

**Git workflow:** Work on main, stash before final commit, create branch + PR + merge for traceability. Channel server downtime during development is acceptable.

---

## File Structure Changes

```
channel_server/core/
  handler.py           → MODIFY: keep Handler protocol + registry, remove handler classes
  handlers/            → CREATE directory
  handlers/__init__.py → CREATE: re-export all handlers
  handlers/feishu.py   → CREATE: FeishuInboundHandler (from handler.py + new ACK/echo logic)
  handlers/cc.py       → CREATE: CCSessionHandler (from handler.py + send_summary routing)
  handlers/tool_card.py → CREATE: ToolCardHandler (from handler.py)
  handlers/admin.py    → CREATE: AdminHandler (from handler.py)
  handlers/forward.py  → CREATE: ForwardAllHandler (from handler.py)
  runtime.py           → MODIFY: async _execute, TransportSend returns, wire(), dedup

channel_server/adapters/
  feishu/adapter.py    → MODIFY: remove all shared state + business logic, async API
  cc/adapter.py        → MODIFY: use wire(), move send_summary routing to handler

tests/channel_server/
  core/test_handler.py       → MODIFY: update imports to handlers/
  core/handlers/             → CREATE directory
  core/handlers/__init__.py  → CREATE
  core/handlers/test_feishu.py → CREATE: ACK, echo, serial correctness tests
  core/handlers/test_cc.py   → CREATE: send_summary routing tests
  core/test_runtime.py       → MODIFY: async _execute, dedup, wire tests
  adapters/test_feishu_adapter.py → MODIFY: statelessness tests
  test_compliance.py         → CREATE: architecture compliance tests
```

---

### Task 1: Extract handlers into handlers/ directory

**Files:**
- Create: `channel_server/core/handlers/__init__.py`
- Create: `channel_server/core/handlers/feishu.py`
- Create: `channel_server/core/handlers/cc.py`
- Create: `channel_server/core/handlers/tool_card.py`
- Create: `channel_server/core/handlers/admin.py`
- Create: `channel_server/core/handlers/forward.py`
- Modify: `channel_server/core/handler.py`
- Modify: `tests/channel_server/core/test_handler.py`

No logic changes in this task — pure file extraction. All existing tests must pass unchanged.

- [ ] **Step 1: Create handlers/ directory and files**

```bash
mkdir -p channel_server/core/handlers
touch channel_server/core/handlers/__init__.py
```

- [ ] **Step 2: Extract FeishuInboundHandler to handlers/feishu.py**

Create `channel_server/core/handlers/feishu.py`:
```python
"""Feishu inbound message handler."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, TransportSend

class FeishuInboundHandler:
    """Route messages for a Feishu chat/thread actor."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender.startswith("feishu_user:"):
            return [Send(to=addr, message=msg) for addr in actor.downstream]
        actions: list[Action] = []
        if actor.transport is not None:
            actions.append(TransportSend(payload=msg.payload))
        return actions

    def on_stop(self, actor: Actor) -> list[Action]:
        actions: list[Action] = []
        if actor.transport is None or actor.transport.type != "feishu_thread":
            return actions
        anchor_msg_id = actor.transport.config.get("root_id", "")
        if anchor_msg_id:
            actions.append(TransportSend(payload={
                "action": "unpin",
                "message_id": anchor_msg_id,
            }))
            actions.append(TransportSend(payload={
                "action": "update_anchor",
                "msg_id": anchor_msg_id,
                "title": f"\U0001f534 [{actor.tag}] ended",
                "body_text": f"Session [{actor.tag}] has been terminated",
                "template": "red",
            }))
        return actions
```

- [ ] **Step 3: Extract CCSessionHandler to handlers/cc.py**

Create `channel_server/core/handlers/cc.py`:
```python
"""CC session message handler."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, StopActor, TransportSend

class CCSessionHandler:
    """Bridge between actor messages and a Claude Code session."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender != actor.address:
            return [TransportSend(payload={**msg.metadata, **msg.payload, "action": "message"})]

        action = msg.payload.get("action")

        if action is None:
            text = msg.payload.get("text", "")
            if actor.tag != "root":
                text = f"[{actor.tag}] {text}"
            reply_msg = Message(
                sender=actor.address,
                payload={**msg.payload, "text": text},
            )
            return [Send(to=addr, message=reply_msg) for addr in actor.downstream]

        if action == "forward":
            target = msg.payload.get("target", "")
            return [Send(to=target, message=msg)]

        if action == "send_summary":
            parent_feishu = msg.payload.get("parent_feishu", "")
            return [Send(to=parent_feishu, message=msg)]

        if action == "tool_notify":
            user_session = actor.address.removeprefix("cc:")
            return [Send(to=f"tool_card:{user_session}", message=msg)]

        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_stop(self, actor: Actor) -> list[Action]:
        actions: list[Action] = []
        user_session = actor.address.removeprefix("cc:")
        actions.append(StopActor(address=f"tool_card:{user_session}"))
        for addr in actor.downstream:
            actions.append(StopActor(address=addr))
        return actions
```

- [ ] **Step 4: Extract ToolCardHandler to handlers/tool_card.py**

Create `channel_server/core/handlers/tool_card.py`:
```python
"""Tool card handler — displays tool execution status in Feishu."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, TransportSend, UpdateActor

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
            TransportSend(payload={
                "action": "tool_notify",
                "text": display,
                "card_msg_id": actor.metadata.get("card_msg_id", ""),
            }),
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        card_msg_id = actor.metadata.get("card_msg_id", "")
        if card_msg_id and actor.transport is not None:
            return [TransportSend(payload={
                "action": "tool_notify",
                "text": "\u2b1b Session ended",
                "card_msg_id": card_msg_id,
            })]
        return []
```

- [ ] **Step 5: Extract AdminHandler to handlers/admin.py**

Create `channel_server/core/handlers/admin.py`:
```python
"""Admin command handler."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send

class AdminHandler:
    """Handles admin commands and notifications."""

    SESSION_COMMANDS = ("/spawn", "/kill", "/sessions")

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "").strip()

        if msg.payload.get("msg_type") == "system":
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        if text.startswith(self.SESSION_COMMANDS):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        if not text.startswith("/"):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

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

        cmd = text.split()[0]
        return [
            Send(
                to=addr,
                message=Message(
                    sender=actor.address,
                    payload={"text": f"未知命令: {cmd}\n发送 /help 查看可用命令"},
                ),
            )
            for addr in actor.downstream
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        return []

    @staticmethod
    def _help_text() -> str:
        return (
            "可用命令:\n"
            "/help — 显示帮助\n"
            "/spawn <name> — 创建子 session\n"
            "/kill <name> — 结束子 session\n"
            "/sessions — 列出活跃 sessions"
        )
```

- [ ] **Step 6: Extract ForwardAllHandler to handlers/forward.py**

Create `channel_server/core/handlers/forward.py`:
```python
"""Forward-all handler — broadcasts every message to downstream."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send

class ForwardAllHandler:
    """Broadcast every message to all downstream actors."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_stop(self, actor: Actor) -> list[Action]:
        return []
```

- [ ] **Step 7: Create handlers/__init__.py with re-exports**

Create `channel_server/core/handlers/__init__.py`:
```python
"""Handler implementations."""
from channel_server.core.handlers.admin import AdminHandler
from channel_server.core.handlers.cc import CCSessionHandler
from channel_server.core.handlers.feishu import FeishuInboundHandler
from channel_server.core.handlers.forward import ForwardAllHandler
from channel_server.core.handlers.tool_card import ToolCardHandler

__all__ = [
    "AdminHandler",
    "CCSessionHandler",
    "FeishuInboundHandler",
    "ForwardAllHandler",
    "ToolCardHandler",
]
```

- [ ] **Step 8: Update handler.py — keep only protocol + registry**

Replace `channel_server/core/handler.py` with:
```python
"""Handler protocol and registry."""
from __future__ import annotations

from typing import Protocol

from channel_server.core.actor import Action, Actor, Message
from channel_server.core.handlers import (
    AdminHandler,
    CCSessionHandler,
    FeishuInboundHandler,
    ForwardAllHandler,
    ToolCardHandler,
)


class Handler(Protocol):
    """Protocol that all actor message handlers must satisfy."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]: ...

    def on_stop(self, actor: Actor) -> list[Action]:
        return []


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

- [ ] **Step 9: Run all tests**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All 126 tests pass. No logic changes, only file moves.

- [ ] **Step 10: Commit**

```bash
git add channel_server/core/handlers/ channel_server/core/handler.py
git commit -m "refactor: extract handlers into handlers/ directory"
```

---

### Task 2: Runtime — async _execute, TransportSend return values, wire(), dedup

**Files:**
- Modify: `channel_server/core/runtime.py`
- Create: `tests/channel_server/core/test_runtime_async.py`

- [ ] **Step 1: Write test for async _execute + TransportSend return value**

Create `tests/channel_server/core/test_runtime_async.py`:
```python
"""Tests for async runtime changes — TransportSend returns, wire, dedup."""
from __future__ import annotations

import asyncio
import pytest
from channel_server.core.actor import Actor, Message, Transport, TransportSend
from channel_server.core.runtime import ActorRuntime
from channel_server.core.handler import HANDLER_REGISTRY


@pytest.mark.asyncio
async def test_transport_send_return_merges_metadata():
    """TransportSend callback returning a dict merges into actor.metadata."""
    rt = ActorRuntime()

    async def fake_transport(actor, payload):
        return {"ack_reaction_id": "r_123"}

    rt.register_transport_handler("test_transport", fake_transport)
    actor = rt.spawn(
        "actor://test", "tool_card",
        transport=Transport(type="test_transport", config={}),
    )
    rt.send("actor://test", Message(sender="actor://ext", payload={"text": "hello"}))

    run_task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.2)
    await rt.shutdown()
    await run_task

    assert actor.metadata.get("ack_reaction_id") == "r_123"


def test_wire_appends_downstream():
    """wire() appends to downstream, deduplicates."""
    rt = ActorRuntime()
    rt.spawn("actor://a", "forward_all")
    rt.spawn("actor://b", "forward_all")

    rt.wire("actor://a", "actor://b")
    assert "actor://b" in rt.lookup("actor://a").downstream

    # Duplicate wire is no-op
    rt.wire("actor://a", "actor://b")
    assert rt.lookup("actor://a").downstream.count("actor://b") == 1

    # Wire to non-existent source is no-op
    rt.wire("actor://nonexistent", "actor://b")


def test_send_dedup_by_message_id():
    """send() with same message_id only delivers once."""
    rt = ActorRuntime()
    rt.spawn("actor://a", "forward_all")

    msg = Message(sender="ext")
    rt.send("actor://a", msg, message_id="msg_001")
    rt.send("actor://a", msg, message_id="msg_001")  # duplicate

    mailbox = rt.mailboxes["actor://a"]
    assert mailbox.qsize() == 1


def test_send_dedup_bounded():
    """Dedup set is bounded — old entries are evicted."""
    rt = ActorRuntime()
    rt.spawn("actor://a", "forward_all")

    for i in range(15000):
        rt.send("actor://a", Message(sender="ext"), message_id=f"msg_{i}")

    assert len(rt._dedup) <= 10000
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/channel_server/core/test_runtime_async.py -v
```

Expected: FAIL — `wire()` not found, `message_id` param not accepted, `_dedup` not found.

- [ ] **Step 3: Implement runtime changes**

Modify `channel_server/core/runtime.py`:

Add `_dedup` set to `__init__`:
```python
def __init__(self) -> None:
    self.actors: dict[str, Actor] = {}
    self.mailboxes: dict[str, asyncio.Queue] = {}
    self._tasks: dict[str, asyncio.Task] = {}
    self._stop_event = asyncio.Event()
    self._transport_handlers: dict[str, Callable] = {}
    self._dedup: set[str] = set()
    self._dedup_max = 10_000
```

Add `wire()` method after `detach()`:
```python
def wire(self, from_addr: str, to_addr: str) -> None:
    """Append to_addr to from_addr's downstream if not already present."""
    actor = self.actors.get(from_addr)
    if actor and to_addr not in actor.downstream:
        actor.downstream.append(to_addr)
```

Update `send()` to accept `message_id`:
```python
def send(self, to: str, message: Message, *, message_id: str = "") -> None:
    """Deliver a message to an actor's mailbox. Dedup by message_id if provided."""
    if message_id:
        if message_id in self._dedup:
            log.info("send: dedup skip %s → %s", message_id[:20], to)
            return
        self._dedup.add(message_id)
        if len(self._dedup) > self._dedup_max:
            to_remove = list(self._dedup)[:self._dedup_max // 2]
            self._dedup -= set(to_remove)

    actor = self.actors.get(to)
    if actor is None or actor.state == "ended":
        log.warning("send: dropping message to %s (not found or ended)", to)
        return
    mailbox = self.mailboxes.get(to)
    if mailbox is not None:
        mailbox.put_nowait(message)
    task = self._tasks.get(to)
    if task is None or task.done():
        log.warning("send: actor %s has no running loop task (state=%s, task=%s)",
                    to, actor.state, "done" if task and task.done() else "missing")
```

Make `_execute` async:
```python
async def _execute(self, actor: Actor, action: Action) -> None:
    if isinstance(action, Send):
        log.info("Execute Send: %s → %s", actor.address, action.to)
        self.send(action.to, action.message)
    elif isinstance(action, TransportSend):
        await self._execute_transport_send(actor, action)
    elif isinstance(action, UpdateActor):
        self._execute_update(actor, action)
    elif isinstance(action, SpawnActor):
        self.spawn(action.address, action.handler, **action.kwargs)
    elif isinstance(action, StopActor):
        self.stop(action.address)
```

Make `_execute_transport_send` async with return value capture. All transport
handlers are async after Task 3, so always await (no isawaitable shim):
```python
async def _execute_transport_send(self, actor: Actor, action: TransportSend) -> None:
    if actor.transport is None:
        log.warning("TransportSend on actor %s with no transport", actor.address)
        return
    callback = self._transport_handlers.get(actor.transport.type)
    if callback is None:
        log.warning("No transport handler for type %s on actor %s",
                    actor.transport.type, actor.address)
        return
    result = await callback(actor, action.payload)
    if isinstance(result, dict):
        # Merge return values into actor metadata (e.g., ack_reaction_id)
        actor.metadata.update(result)
        # Auto-maintain sent_msg_ids ring buffer for echo prevention
        sent_id = result.get("_sent_msg_id", "")
        if sent_id:
            sent_ids = list(actor.metadata.get("sent_msg_ids", []))
            sent_ids.append(sent_id)
            if len(sent_ids) > 100:
                sent_ids = sent_ids[-100:]
            actor.metadata["sent_msg_ids"] = sent_ids
```

Note: During Task 2 (before Task 3 async migration), temporarily keep
`inspect.isawaitable` for backward compat with sync handlers. Task 3 Step 6
removes it when all handlers are async.

Update `_actor_loop` to await `_execute`:
```python
# In _actor_loop, change:
for action in actions:
    self._execute(actor, action)
# To:
for action in actions:
    await self._execute(actor, action)
```

Make `stop()` async so on_stop TransportSend actions complete before state change:
```python
async def stop(self, address: str) -> None:
    """Stop an actor — run on_stop lifecycle, set state to ended, cancel loop."""
    actor = self.actors.get(address)
    if actor is None:
        log.warning("stop: actor %s not found", address)
        return
    if actor.state == "ended":
        return

    try:
        handler = get_handler(actor.handler)
        actions = handler.on_stop(actor)
        for action in actions:
            await self._execute(actor, action)
    except Exception as e:
        log.error("on_stop error for %s: %s", address, e)

    actor.state = "ended"
    self._cancel_task(address)
```

Note: `stop()` becoming async means callers in `_actor_loop` (for StopActor) and
`_handle_kill`/`_handle_spawn` must await it. `_execute` already handles StopActor:
```python
elif isinstance(action, StopActor):
    await self.stop(action.address)
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All tests pass (existing + new).

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/runtime.py tests/channel_server/core/test_runtime_async.py
git commit -m "feat: async _execute, TransportSend returns, wire(), dedup in runtime"
```

---

### Task 3: Async Feishu API migration

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`

Convert all Feishu API methods from sync+threading.Thread to async SDK. Transport handlers become async def. No business logic changes in this task.

- [ ] **Step 1: Convert _send_message to async**

Replace the method (currently lines 497-528) with async version using `areply`/`acreate`:
```python
async def _send_message(self, chat_id: str, text: str, thread_anchor: str | None = None) -> str:
    """Send a text message to a Feishu chat or thread. Returns sent message_id."""
    if not self.feishu_client:
        return ""
    try:
        content = json.dumps({"text": text})
        if thread_anchor:
            req = (ReplyMessageRequestBody.builder()
                   .msg_type("text").content(content).build())
            full_req = (ReplyMessageRequest.builder()
                        .message_id(thread_anchor)
                        .request_body(req).build())
            resp = await self.feishu_client.im.v1.message.areply(full_req)
        else:
            req = (CreateMessageRequestBody.builder()
                   .receive_id(chat_id).msg_type("text").content(content).build())
            full_req = (CreateMessageRequest.builder()
                        .receive_id_type("chat_id")
                        .request_body(req).build())
            resp = await self.feishu_client.im.v1.message.acreate(full_req)

        if resp.success() and resp.data and resp.data.message_id:
            log.info("Reply to Feishu chat_id=%s thread=%s text=%s",
                     chat_id, thread_anchor or "-", text[:60])
            return resp.data.message_id
        else:
            log.warning("Reply failed: code=%s msg=%s", resp.code, resp.msg)
            return ""
    except Exception as e:
        log.error("_send_message error: %s", e)
        return ""
```

- [ ] **Step 2: Convert _send_reaction to async**

```python
async def _send_reaction(self, message_id: str, emoji_type: str = "MUSCLE") -> str:
    """Add emoji reaction. Returns reaction_id."""
    if not self.feishu_client:
        return ""
    try:
        req = (lark.BaseRequest.builder()
               .http_method(lark.HttpMethod.POST)
               .uri(f"/open-apis/im/v1/messages/{message_id}/reactions")
               .token_types({lark.AccessTokenType.TENANT})
               .body({"reaction_type": {"emoji_type": emoji_type}})
               .build())
        resp = await self.feishu_client.arequest(req)
        if not resp.success():
            log.warning("Reaction failed for %s: code=%s msg=%s", message_id, resp.code, resp.msg)
            return ""
        log.info("Reaction sent: %s %s", emoji_type, message_id)
        try:
            data = json.loads(resp.raw.content)
            return data.get("data", {}).get("reaction_id", "")
        except Exception:
            return ""
    except Exception as e:
        log.warning("Reaction error for %s: %s", message_id, e)
        return ""
```

- [ ] **Step 3: Convert _remove_reaction to async**

```python
async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
    """Remove a reaction by ID."""
    if not reaction_id or not self.feishu_client:
        return
    try:
        req = (lark.BaseRequest.builder()
               .http_method(lark.HttpMethod.DELETE)
               .uri(f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}")
               .token_types({lark.AccessTokenType.TENANT})
               .build())
        resp = await self.feishu_client.arequest(req)
        if resp.success():
            log.info("Removed reaction %s on %s", reaction_id, message_id)
    except Exception as e:
        log.warning("Remove reaction error: %s", e)
```

- [ ] **Step 4: Convert remaining API methods to async**

Convert each of these following the same pattern (sync → async, .method() → .amethod()):
- `_send_file()` → async, use `.acreate()`
- `_update_card()` → async, use `.apatch()`
- `_update_anchor_card()` → async, use `.apatch()`
- `create_thread_anchor()` → async, use `.acreate()`, `.areply()`
- `create_tool_card()` → async, use `.acreate()`
- `pin_message()` → async, use `.acreate()`
- `unpin_message()` → async, use `.adelete()`
- `_download_resource()` → async, use `.aget()`
- `send_startup_notification()` → async, use `.acreate()`

- [ ] **Step 5: Convert transport handlers to async**

Update `_handle_chat_transport` and `_handle_thread_transport` to `async def`, replace `threading.Thread(target=...).start()` with direct `await self._method()` calls:

```python
async def _handle_chat_transport(self, actor: Actor, payload: dict) -> dict | None:
    """Execute outbound Feishu API call. Returns metadata to merge if any."""
    action = payload.get("action")
    chat_id = actor.transport.config["chat_id"] if actor.transport else ""

    if action is None:
        text = payload.get("text", "")
        sent_id = await self._send_message(chat_id, text, None)
        if sent_id:
            return {"_sent_msg_id": sent_id}
    elif action == "ack_react":
        message_id = payload.get("message_id", "")
        reaction_id = await self._send_reaction(message_id)
        if reaction_id:
            return {"ack_reaction_id": reaction_id}
    elif action == "remove_ack":
        message_id = payload.get("message_id", "")
        reaction_id = payload.get("reaction_id", "")
        await self._remove_reaction(message_id, reaction_id)
    elif action == "react":
        message_id = payload.get("message_id", "")
        emoji_type = payload.get("emoji_type", "THUMBSUP")
        await self._send_reaction(message_id, emoji_type)
    elif action == "send_file":
        file_path = payload.get("file_path", "")
        await self._send_file(payload.get("chat_id", chat_id), file_path)
    elif action == "tool_notify":
        msg_id = payload.get("card_msg_id", "")
        text = payload.get("text", "")
        await self._update_card(msg_id, text)
    elif action == "unpin":
        message_id = payload.get("message_id", "")
        await self.unpin_message(message_id)
    elif action == "update_anchor":
        msg_id = payload.get("msg_id", "")
        title = payload.get("title", "")
        body_text = payload.get("body_text", "")
        template = payload.get("template", "red")
        await self._update_anchor_card(msg_id, title, body_text=body_text, template=template)
    return None
```

Apply same pattern to `_handle_thread_transport`.

- [ ] **Step 6: Remove all threading.Thread calls from adapter**

Search and remove every `threading.Thread(target=..., daemon=True).start()` in adapter.py. The only thread that stays is the Feishu WS listener thread in `start_feishu_ws` — mark it with `# compliance-exempt: WS listener needs own event loop`.

Also remove the `inspect.isawaitable` shim from `_execute_transport_send` since all transport handlers are now async.

- [ ] **Step 7: Update _handle_spawn to await async methods**

In `channel_server/adapters/cc/adapter.py`, update `_handle_spawn` (line 368-370):
```python
anchor_msg_id = await self.feishu_adapter.create_thread_anchor(chat_id, tag)
if anchor_msg_id:
    await self.feishu_adapter.pin_message(anchor_msg_id)
```

And tool card creation:
```python
tool_card_msg_id = await self.feishu_adapter.create_tool_card(chat_id, f"[{tag}] starting...")
```

- [ ] **Step 8: Run all tests**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
git add channel_server/adapters/ channel_server/core/
git commit -m "refactor: async Feishu API, remove all threading.Thread from adapters"
```

---

### Task 4: Migrate ACK reaction + echo prevention to FeishuInboundHandler

**Files:**
- Modify: `channel_server/core/handlers/feishu.py`
- Modify: `channel_server/adapters/feishu/adapter.py`
- Create: `tests/channel_server/core/handlers/__init__.py`
- Create: `tests/channel_server/core/handlers/test_feishu.py`

- [ ] **Step 1: Write tests for ACK emit on inbound**

Create `tests/channel_server/core/handlers/__init__.py` (empty) and `tests/channel_server/core/handlers/test_feishu.py`:
```python
"""Tests for FeishuInboundHandler — ACK, echo prevention, serial correctness."""
from __future__ import annotations

from channel_server.core.actor import Actor, Message, Send, Transport, TransportSend, UpdateActor
from channel_server.core.handlers.feishu import FeishuInboundHandler


def make_actor(**kwargs) -> Actor:
    defaults = dict(address="feishu:oc_test", tag="test", handler="feishu_inbound",
                    downstream=["system:admin"], metadata={})
    defaults.update(kwargs)
    return Actor(**defaults)


def test_inbound_emits_ack_and_forward():
    handler = FeishuInboundHandler()
    actor = make_actor(transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}))
    msg = Message(sender="feishu_user:u1", payload={"message_id": "om_1", "text": "hi"})

    actions = handler.handle(actor, msg)

    updates = [a for a in actions if isinstance(a, UpdateActor)]
    acks = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") == "ack_react"]
    sends = [a for a in actions if isinstance(a, Send)]

    assert len(updates) == 1
    assert updates[0].changes["metadata"]["ack_msg_id"] == "om_1"
    assert len(acks) == 1
    assert acks[0].payload["message_id"] == "om_1"
    assert len(sends) >= 1


def test_outbound_removes_ack_and_sends():
    handler = FeishuInboundHandler()
    actor = make_actor(
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}),
        metadata={"ack_msg_id": "om_1", "ack_reaction_id": "r_1"},
    )
    msg = Message(sender="cc:user.root", payload={"text": "reply"})

    actions = handler.handle(actor, msg)

    removes = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") == "remove_ack"]
    sends = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") != "remove_ack"]

    assert len(removes) == 1
    assert removes[0].payload["message_id"] == "om_1"
    assert removes[0].payload["reaction_id"] == "r_1"
    assert len(sends) >= 1


def test_echo_prevention():
    handler = FeishuInboundHandler()
    actor = make_actor(
        metadata={"sent_msg_ids": ["om_echo"]},
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}),
    )
    msg = Message(sender="feishu_user:u1", payload={"message_id": "om_echo", "text": "echo"})

    actions = handler.handle(actor, msg)
    assert actions == []


def test_serial_ack_no_interference():
    """Messages A then B, reply to A removes B's ack (latest)."""
    handler = FeishuInboundHandler()
    actor = make_actor(transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}))

    # Inbound A
    msg_a = Message(sender="feishu_user:u1", payload={"message_id": "om_A", "text": "A"})
    actions_a = handler.handle(actor, msg_a)
    for a in actions_a:
        if isinstance(a, UpdateActor):
            actor.metadata.update(a.changes.get("metadata", {}))

    assert actor.metadata["ack_msg_id"] == "om_A"

    # Inbound B
    msg_b = Message(sender="feishu_user:u1", payload={"message_id": "om_B", "text": "B"})
    actions_b = handler.handle(actor, msg_b)
    for a in actions_b:
        if isinstance(a, UpdateActor):
            actor.metadata.update(a.changes.get("metadata", {}))

    assert actor.metadata["ack_msg_id"] == "om_B"

    # Reply (outbound) → removes latest ack (B)
    reply = Message(sender="cc:user.root", payload={"text": "reply"})
    actions_reply = handler.handle(actor, reply)
    removes = [a for a in actions_reply if isinstance(a, TransportSend) and a.payload.get("action") == "remove_ack"]
    assert len(removes) == 1
    assert removes[0].payload["message_id"] == "om_B"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/channel_server/core/handlers/test_feishu.py -v
```

Expected: FAIL — handler doesn't emit ACK actions yet.

- [ ] **Step 3: Implement FeishuInboundHandler with ACK + echo logic**

Update `channel_server/core/handlers/feishu.py`:
```python
"""Feishu inbound message handler."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, TransportSend, UpdateActor


class FeishuInboundHandler:
    """Route messages for a Feishu chat/thread actor.

    Inbound (from feishu_user:*):
      - Check echo prevention (skip if msg_id in sent_msg_ids)
      - ACK react via TransportSend
      - Update ack_msg_id in metadata
      - Forward to downstream

    Outbound (from cc:* or others):
      - Remove ACK react for current ack_msg_id
      - Push to Feishu transport
      - Track sent message_id for echo prevention
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender.startswith("feishu_user:"):
            return self._handle_inbound(actor, msg)
        return self._handle_outbound(actor, msg)

    def _handle_inbound(self, actor: Actor, msg: Message) -> list[Action]:
        message_id = msg.payload.get("message_id", "")

        # Echo prevention — skip messages we sent ourselves
        sent_ids = actor.metadata.get("sent_msg_ids", [])
        if message_id and message_id in sent_ids:
            return []

        actions: list[Action] = []

        # ACK react
        if message_id:
            actions.append(UpdateActor(changes={"metadata": {"ack_msg_id": message_id}}))
            actions.append(TransportSend(payload={"action": "ack_react", "message_id": message_id}))

        # Forward to downstream (admin, CC actors)
        for addr in actor.downstream:
            actions.append(Send(to=addr, message=msg))

        return actions

    def _handle_outbound(self, actor: Actor, msg: Message) -> list[Action]:
        actions: list[Action] = []

        # Remove ACK react for the latest inbound message
        ack_msg_id = actor.metadata.get("ack_msg_id", "")
        ack_reaction_id = actor.metadata.get("ack_reaction_id", "")
        if ack_msg_id:
            actions.append(TransportSend(payload={
                "action": "remove_ack",
                "message_id": ack_msg_id,
                "reaction_id": ack_reaction_id,
            }))
            actions.append(UpdateActor(changes={"metadata": {"ack_msg_id": "", "ack_reaction_id": ""}}))

        # Send reply via transport
        if actor.transport is not None:
            actions.append(TransportSend(payload=msg.payload))

        return actions

    def on_stop(self, actor: Actor) -> list[Action]:
        """Cleanup: unpin anchor message and update card to 'ended'."""
        actions: list[Action] = []
        if actor.transport is None or actor.transport.type != "feishu_thread":
            return actions
        anchor_msg_id = actor.transport.config.get("root_id", "")
        if anchor_msg_id:
            actions.append(TransportSend(payload={
                "action": "unpin",
                "message_id": anchor_msg_id,
            }))
            actions.append(TransportSend(payload={
                "action": "update_anchor",
                "msg_id": anchor_msg_id,
                "title": f"\U0001f534 [{actor.tag}] ended",
                "body_text": f"Session [{actor.tag}] has been terminated",
                "template": "red",
            }))
        return actions
```

- [ ] **Step 4: Remove ACK logic from FeishuAdapter**

In `channel_server/adapters/feishu/adapter.py`:
- Remove `_last_msg_id` from `__init__`
- Remove `_ack_reactions` from `__init__`
- Remove the ACK reaction block from `on_feishu_event` (lines ~295-303)
- Remove `_last_msg_id.pop()` and `_remove_reaction` calls from `_handle_chat_transport` and `_handle_thread_transport`

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All tests pass. Some existing handler tests may need updating for new ACK actions.

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handlers/feishu.py channel_server/adapters/feishu/adapter.py tests/
git commit -m "feat: ACK reaction + echo prevention in FeishuInboundHandler"
```

---

### Task 5: Remove remaining shared state from adapter

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`
- Modify: `channel_server/core/handlers/feishu.py`

- [ ] **Step 1: Remove _seen from adapter**

Remove `self._seen` from `__init__`. Remove dedup logic from `on_feishu_event`. The adapter now passes `message_id` to `runtime.send()` for dedup:

```python
def on_feishu_event(self, event: dict) -> None:
    message_id = event.get("message_id", "")
    chat_id = event.get("chat_id", "")

    log.info("on_feishu_event: msg_id=%s chat_id=%s text=%s",
             message_id[:20], chat_id[:20], event.get("text", "")[:40])

    # Route — no filtering, no side effects
    root_id = event.get("root_id") or None
    address = self.resolve_actor_address(chat_id, None)
    if root_id:
        thread_addr = self.resolve_actor_address(chat_id, root_id)
        thread_actor = self.runtime.lookup(thread_addr)
        if thread_actor and thread_actor.state != "ended":
            for ds_addr in thread_actor.downstream:
                ds = self.runtime.lookup(ds_addr)
                if ds and ds.state == "active" and ds.transport is not None:
                    address = thread_addr
                    break

    # Auto-spawn main chat actor if needed
    actor = self.runtime.lookup(address)
    if actor is None or actor.state == "ended":
        self.runtime.spawn(
            address, "feishu_inbound", tag=chat_id,
            transport=Transport(type="feishu_chat", config={"chat_id": chat_id}),
        )

    # Build and deliver — dedup handled by runtime
    msg = Message(
        sender=f"feishu_user:{event.get('user_id', '') or 'unknown'}",
        payload={
            "text": event.get("text", ""),
            "file_path": event.get("file_path", ""),
            "chat_id": chat_id,
            "message_id": message_id,
            "msg_type": event.get("msg_type", "text"),
        },
        metadata={
            "user": event.get("user", ""),
            "user_id": event.get("user_id", ""),
            "message_id": message_id,
            "chat_id": chat_id,
            "root_id": root_id or "",
            "msg_type": event.get("msg_type", "text"),
        },
    )
    self.runtime.send(address, msg, message_id=message_id)
```

- [ ] **Step 2: Remove _recent_sent from adapter**

Remove `self._recent_sent` from `__init__`. Remove echo checks from `on_feishu_event` and `on_message`. Echo prevention is now in the handler (checking `actor.metadata["sent_msg_ids"]`).

The transport handler returns `{"_sent_msg_id": sent_id}` which the runtime merges into metadata. The handler uses this to maintain the sent_msg_ids list.

- [ ] **Step 3: Move _chat_id_map to handler**

Remove `_chat_id_map`, `_load_chat_id_map`, `_save_chat_id_map`, `_record_chat_id` from adapter. Add to FeishuInboundHandler `_handle_inbound`:

```python
# In _handle_inbound, after ACK:
chat_type = msg.metadata.get("chat_type", "")
user_id = msg.metadata.get("user_id", "")
if chat_type == "p2p" and user_id and chat_id:
    chat_map = dict(actor.metadata.get("chat_id_map", {}))
    if user_id not in chat_map:
        chat_map[user_id] = chat_id
        actions.append(UpdateActor(changes={"metadata": {"chat_id_map": chat_map}}))
```

- [ ] **Step 4: Run all tests, fix failures**

```bash
uv run pytest tests/channel_server/ -v
```

- [ ] **Step 5: Commit**

```bash
git add channel_server/ tests/
git commit -m "refactor: remove all shared state from FeishuAdapter"
```

---

### Task 6: Simplify CCAdapter — wire() + send_summary routing

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`
- Modify: `channel_server/core/handlers/cc.py`

- [ ] **Step 1: Replace topology wiring with runtime.wire()**

In `_handle_register` (cc/adapter.py lines 178-195), replace direct downstream mutation:
```python
# Replace:
if feishu_addr not in cc_actor.downstream:
    cc_actor.downstream.append(feishu_addr)
# With:
self.runtime.wire(address, feishu_addr)
self.runtime.wire("system:admin", address)
```

- [ ] **Step 2: Move send_summary parent_feishu lookup to CCSessionHandler**

In `channel_server/core/handlers/cc.py`, the handler resolves parent_feishu by
walking `actor.parent` → looking up parent's downstream for a feishu address.
The handler needs runtime access for this lookup. Add `runtime` to handler init:

```python
class CCSessionHandler:
    def __init__(self, runtime: ActorRuntime | None = None):
        self._runtime = runtime

    def handle(self, actor, msg):
        # ... existing logic ...
        if action == "send_summary":
            # Resolve parent feishu address
            parent_feishu = ""
            if actor.parent and self._runtime:
                parent = self._runtime.lookup(actor.parent)
                if parent:
                    parent_feishu = next(
                        (d for d in parent.downstream if d.startswith("feishu:")), ""
                    )
            return [Send(to=parent_feishu, message=msg)] if parent_feishu else []
```

Update HANDLER_REGISTRY in handler.py to pass runtime:
```python
# Deferred initialization — runtime set after ActorRuntime is created
"cc_session": CCSessionHandler(),  # runtime injected by app.py
```

- [ ] **Step 3: Remove parent_feishu injection from _route_to_actor**

Remove lines 248-258 from `_route_to_actor` in cc/adapter.py.

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/channel_server/ -v
```

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/cc/ channel_server/core/handlers/
git commit -m "refactor: CCAdapter uses wire(), simplify routing"
```

---

### Task 7: File download refactor

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`
- Modify: `channel_server/adapters/feishu/parsers.py`

File downloads currently happen in the Feishu WS thread (blocking, inside parsers).
After async migration, defer downloads to the transport handler:

- [ ] **Step 1: Change parsers to pass file_key instead of downloading**

In `parsers.py`, `_parse_downloadable` currently calls `server.download_file()`.
Change to return the file_key/image_key in the text representation without downloading:

```python
@register_parser("image", "file", "audio", "media")
def _parse_downloadable(content: dict, message, server) -> tuple[str, str]:
    msg_type = message.message_type or "file"
    if msg_type == "image":
        file_key = content.get("image_key", "")
    else:
        file_key = content.get("file_key", "")
    file_name = content.get("file_name", file_key or "unknown")
    return f"[{msg_type}: {file_name}]", ""  # no download, file_key in payload
```

Add file_key to the event payload in `on_message` so the handler/transport can
download later if needed.

- [ ] **Step 2: Add async download in transport handler**

Add `"download"` action to `_handle_chat_transport` that downloads on demand:
```python
elif action == "download":
    message_id = payload.get("message_id", "")
    file_key = payload.get("file_key", "")
    resource_type = payload.get("resource_type", "file")
    file_name = payload.get("file_name", "unknown")
    chat_id_for_path = payload.get("chat_id", "unknown")
    path = await self._download_resource(message_id, file_key, resource_type, file_name, chat_id_for_path, "download")
    if path:
        return {"downloaded_file_path": path}
```

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/channel_server/ -v
git add channel_server/adapters/feishu/
git commit -m "refactor: defer file downloads from parsers to transport handler"
```

---

### Task 8: Architecture compliance tests

**Files:**
- Create: `tests/channel_server/test_compliance.py`

- [ ] **Step 1: Write compliance tests**

```python
"""Architecture compliance tests — prevent actor model regression."""
from __future__ import annotations

import inspect
from pathlib import Path
from glob import glob


def test_adapter_files_have_no_metadata_writes():
    """Adapter source must not write to actor.metadata."""
    for path in glob("channel_server/adapters/**/*.py", recursive=True):
        source = Path(path).read_text()
        for i, line in enumerate(source.split("\n"), 1):
            if "actor.metadata" in line and "=" in line and "# compliance-exempt" not in line:
                if ".get(" not in line and "isinstance" not in line:
                    raise AssertionError(f"{path}:{i} writes to actor.metadata: {line.strip()}")


def test_handler_files_have_no_api_imports():
    """Handler source must not import lark_oapi or call feishu_client."""
    for path in glob("channel_server/core/handlers/*.py"):
        source = Path(path).read_text()
        assert "lark_oapi" not in source, f"{path} imports lark_oapi"
        assert "feishu_client" not in source, f"{path} references feishu_client"


def test_transport_handlers_are_async():
    """All transport handler methods must be async."""
    from channel_server.adapters.feishu.adapter import FeishuAdapter
    from channel_server.core.runtime import ActorRuntime
    from unittest.mock import MagicMock

    rt = ActorRuntime()
    adapter = FeishuAdapter(rt, MagicMock())
    assert inspect.iscoroutinefunction(adapter._handle_chat_transport)
    assert inspect.iscoroutinefunction(adapter._handle_thread_transport)


def test_no_threading_in_adapters():
    """No threading.Thread in adapter files (except WS listener)."""
    for path in glob("channel_server/adapters/**/*.py", recursive=True):
        source = Path(path).read_text()
        for i, line in enumerate(source.split("\n"), 1):
            if "threading.Thread" in line and "# compliance-exempt" not in line:
                raise AssertionError(f"{path}:{i} uses threading.Thread: {line.strip()}")


def test_adapter_has_no_business_state():
    """FeishuAdapter must not hold _last_msg_id, _ack_reactions, _seen, _recent_sent."""
    from channel_server.adapters.feishu.adapter import FeishuAdapter
    from channel_server.core.runtime import ActorRuntime
    from unittest.mock import MagicMock

    adapter = FeishuAdapter(ActorRuntime(), MagicMock())
    for attr in ["_last_msg_id", "_ack_reactions", "_seen", "_recent_sent", "_chat_id_map"]:
        assert not hasattr(adapter, attr), f"FeishuAdapter still has {attr}"
```

- [ ] **Step 2: Run compliance tests**

```bash
uv run pytest tests/channel_server/test_compliance.py -v
```

Expected: All pass.

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/channel_server/test_compliance.py
git commit -m "test: architecture compliance tests for actor model"
```

---

### Task 9: Cleanup + final PR

**Files:**
- All modified files

- [ ] **Step 1: Remove dead code**

Search for unused imports, dead methods, orphaned helper functions across all modified files.

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/channel_server/ -v
```

Expected: All tests pass.

- [ ] **Step 3: Create branch and PR**

```bash
git stash
git checkout -b refactor/actor-model-remediation
git stash pop
git add channel_server/ tests/ docs/
git commit -m "refactor: full actor model remediation

- Extract handlers into handlers/ directory
- Runtime: async _execute, TransportSend return values, wire(), dedup
- Async Feishu API: all methods use async SDK, no threading.Thread
- ACK reaction + echo prevention in FeishuInboundHandler
- Remove all shared state from FeishuAdapter
- CCAdapter uses runtime.wire() for topology
- Architecture compliance tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"

git push -u origin refactor/actor-model-remediation
gh pr create --title "refactor: full actor model remediation" --body "..."
gh pr merge --merge
git checkout main && git pull
```
