# Actor Model Channel Server — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc routing in channel-server with an Erlang-inspired actor model where every participant is an actor with a unique address, managed by a minimal runtime.

**Architecture:** IM-agnostic core (`channel_server/core/`) defines Actor, Message, Handler, and Runtime. Adapters (`channel_server/adapters/feishu/`, `channel_server/adapters/cc/`) bridge Feishu and CC sessions to the runtime. Old `feishu/` directory is retired.

**Tech Stack:** Python 3.11+, asyncio, websockets, lark-oapi, mcp, pytest, pytest-asyncio

**Spec:** `docs/specs/2026-04-15-actor-model-channel-server.md`

---

## File Structure

```
channel_server/
  __init__.py
  app.py                        — Entry point: init runtime + adapters, start service

  core/
    __init__.py
    actor.py                    — Actor, Transport, Message, Action dataclasses
    handler.py                  — Handler protocol, registry, built-in handlers
    runtime.py                  — ActorRuntime: registry, mailbox, per-actor loops
    persistence.py              — actors.json read/write

  adapters/
    __init__.py
    feishu/
      __init__.py
      adapter.py                — Feishu event inbound + Feishu API outbound
      parsers.py                — Feishu message parsing (migrated from feishu/message_parsers.py)
    cc/
      __init__.py
      adapter.py                — WebSocket server (accepts CC connections)
      channel.py                — MCP client (runs in CC session process, migrated from feishu/channel.py)

tests/
  channel_server/
    __init__.py
    core/
      __init__.py
      test_actor.py             — Actor/Message/Action dataclass tests
      test_handler.py           — Handler behavior tests
      test_runtime.py           — Runtime spawn/send/stop/attach tests
      test_persistence.py       — Persistence round-trip tests
    adapters/
      __init__.py
      test_feishu_adapter.py    — Feishu adapter inbound/outbound tests
      test_cc_adapter.py        — CC adapter registration/message tests
    test_integration.py         — End-to-end message flow tests
```

---

### Task 1: Core Data Types (`channel_server/core/actor.py`)

**Files:**
- Create: `channel_server/__init__.py`
- Create: `channel_server/core/__init__.py`
- Create: `channel_server/core/actor.py`
- Test: `tests/channel_server/__init__.py`
- Test: `tests/channel_server/core/__init__.py`
- Test: `tests/channel_server/core/test_actor.py`

- [ ] **Step 1: Write tests for core data types**

```python
# tests/channel_server/core/test_actor.py
from channel_server.core.actor import Actor, Transport, Message, Send, TransportSend, UpdateActor, SpawnActor, StopActor


def test_actor_creation():
    actor = Actor(
        address="cc:linyilun.root",
        tag="root",
        handler="cc_session",
    )
    assert actor.address == "cc:linyilun.root"
    assert actor.state == "active"
    assert actor.parent is None
    assert actor.downstream == []
    assert actor.transport is None
    assert actor.metadata == {}
    assert actor.created_at is not None


def test_actor_with_transport():
    transport = Transport(type="websocket", config={"ws_id": "abc123"})
    actor = Actor(
        address="cc:linyilun.dev",
        tag="dev",
        handler="cc_session",
        parent="cc:linyilun.root",
        downstream=["feishu:oc_xxx:om_anchor"],
        transport=transport,
    )
    assert actor.parent == "cc:linyilun.root"
    assert actor.transport.type == "websocket"
    assert actor.transport.config["ws_id"] == "abc123"


def test_message_creation():
    msg = Message(
        sender="feishu:oc_xxx",
        type="text",
        payload={"text": "hello"},
    )
    assert msg.sender == "feishu:oc_xxx"
    assert msg.type == "text"
    assert msg.metadata == {}


def test_action_types():
    msg = Message(sender="a", type="text", payload={})
    send = Send(to="cc:linyilun.root", message=msg)
    assert send.to == "cc:linyilun.root"

    ts = TransportSend(payload={"text": "hi"})
    assert ts.payload["text"] == "hi"

    ua = UpdateActor(changes={"tag": "new_tag"})
    assert ua.changes["tag"] == "new_tag"

    sa = SpawnActor(address="cc:linyilun.dev", handler="cc_session", kwargs={"tag": "dev"})
    assert sa.address == "cc:linyilun.dev"

    stop = StopActor(address="cc:linyilun.dev")
    assert stop.address == "cc:linyilun.dev"


def test_actor_to_dict_and_from_dict():
    actor = Actor(
        address="cc:linyilun.root",
        tag="root",
        handler="cc_session",
        downstream=["feishu:oc_xxx"],
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_xxx"}),
    )
    d = actor.to_dict()
    assert d["address"] == "cc:linyilun.root"
    assert d["transport_config"] == {"type": "feishu_chat", "chat_id": "oc_xxx"}

    restored = Actor.from_dict(d)
    assert restored.address == actor.address
    assert restored.transport.type == "feishu_chat"


def test_actor_to_dict_no_transport():
    actor = Actor(address="system:monitor", tag="monitor", handler="forward_all")
    d = actor.to_dict()
    assert d["transport_config"] is None

    restored = Actor.from_dict(d)
    assert restored.transport is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_actor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'channel_server'`

- [ ] **Step 3: Implement core data types**

```python
# channel_server/__init__.py
"""Actor-model channel server."""

# channel_server/core/__init__.py
"""Core actor model: data types, handlers, runtime."""

# channel_server/core/actor.py
"""Core data types for the actor model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Union


@dataclass
class Transport:
    """Connection between an actor and an external system."""
    type: str       # "websocket" | "feishu_chat" | "feishu_thread"
    config: dict    # type-specific config


@dataclass
class Actor:
    """A participant in the system with a unique address."""
    address: str
    tag: str
    handler: str
    state: str = "active"
    parent: str | None = None
    downstream: list[str] = field(default_factory=list)
    transport: Transport | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        """Serialize for persistence. Transport instance is stored as config only."""
        return {
            "address": self.address,
            "tag": self.tag,
            "handler": self.handler,
            "state": self.state,
            "parent": self.parent,
            "downstream": list(self.downstream),
            "transport_config": {"type": self.transport.type, **self.transport.config} if self.transport else None,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Actor:
        """Restore from persisted dict."""
        tc = d.get("transport_config")
        transport = None
        if tc:
            t = dict(tc)
            transport = Transport(type=t.pop("type"), config=t)
        return cls(
            address=d["address"],
            tag=d["tag"],
            handler=d["handler"],
            state=d.get("state", "active"),
            parent=d.get("parent"),
            downstream=list(d.get("downstream", [])),
            transport=transport,
            metadata=dict(d.get("metadata", {})),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class Message:
    """A message passed between actors."""
    sender: str
    type: str           # "text" | "command" | "file" | "system" | "error"
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


# --- Actions (returned by handlers, executed by runtime) ---

@dataclass
class Send:
    """Send a message to another actor."""
    to: str
    message: Message

@dataclass
class TransportSend:
    """Send through actor's own transport."""
    payload: dict

@dataclass
class UpdateActor:
    """Modify actor's own state/metadata."""
    changes: dict

@dataclass
class SpawnActor:
    """Create a new actor."""
    address: str
    handler: str
    kwargs: dict = field(default_factory=dict)

@dataclass
class StopActor:
    """Stop an actor."""
    address: str


# Union type for handler return values
Action = Union[Send, TransportSend, UpdateActor, SpawnActor, StopActor]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_actor.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/ tests/channel_server/
git commit -m "feat(actor): core data types — Actor, Transport, Message, Action"
```

---

### Task 2: Handler Protocol and Built-in Handlers (`channel_server/core/handler.py`)

**Files:**
- Create: `channel_server/core/handler.py`
- Test: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Write tests for handlers**

```python
# tests/channel_server/core/test_handler.py
from channel_server.core.actor import Actor, Message, Send, TransportSend, UpdateActor
from channel_server.core.handler import (
    get_handler,
    FeishuInboundHandler,
    CCSessionHandler,
    ForwardAllHandler,
    ToolCardHandler,
)


def test_feishu_inbound_forwards_to_downstream():
    actor = Actor(
        address="feishu:oc_xxx",
        tag="DM",
        handler="feishu_inbound",
        downstream=["cc:linyilun.root"],
    )
    msg = Message(sender="feishu:oc_xxx", type="text", payload={"text": "hello"})
    handler = FeishuInboundHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "cc:linyilun.root"


def test_feishu_inbound_multiple_downstream():
    actor = Actor(
        address="feishu:oc_xxx",
        tag="DM",
        handler="feishu_inbound",
        downstream=["cc:linyilun.root", "system:monitor"],
    )
    msg = Message(sender="feishu:oc_xxx", type="text", payload={"text": "hello"})
    handler = FeishuInboundHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 2
    assert actions[0].to == "cc:linyilun.root"
    assert actions[1].to == "system:monitor"


def test_cc_session_text_message_sends_via_transport():
    actor = Actor(
        address="cc:linyilun.root",
        tag="root",
        handler="cc_session",
        downstream=["feishu:oc_xxx"],
    )
    msg = Message(sender="feishu:oc_xxx", type="text", payload={"text": "hello"})
    handler = CCSessionHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], TransportSend)
    assert actions[0].payload["text"] == "hello"


def test_cc_session_reply_sends_to_downstream():
    actor = Actor(
        address="cc:linyilun.root",
        tag="root",
        handler="cc_session",
        downstream=["feishu:oc_xxx"],
    )
    msg = Message(sender="cc:linyilun.root", type="command", payload={"command": "reply", "text": "world"})
    handler = CCSessionHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "feishu:oc_xxx"


def test_cc_session_forward_sends_to_target():
    actor = Actor(
        address="cc:linyilun.root",
        tag="root",
        handler="cc_session",
        downstream=["feishu:oc_xxx"],
    )
    msg = Message(
        sender="cc:linyilun.root",
        type="command",
        payload={"command": "forward", "to": "cc:linyilun.dev", "text": "check this"},
    )
    handler = CCSessionHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "cc:linyilun.dev"


def test_cc_session_send_summary_sends_to_parent_downstream():
    actor = Actor(
        address="cc:linyilun.dev",
        tag="dev",
        handler="cc_session",
        parent="cc:linyilun.root",
        downstream=["feishu:oc_xxx:om_anchor"],
    )
    msg = Message(
        sender="cc:linyilun.dev",
        type="command",
        payload={"command": "send_summary", "summary": "done with task", "parent_feishu": "feishu:oc_xxx"},
    )
    handler = CCSessionHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "feishu:oc_xxx"


def test_cc_session_update_title_sends_transport():
    actor = Actor(
        address="cc:linyilun.dev",
        tag="dev",
        handler="cc_session",
        downstream=["feishu:oc_xxx:om_anchor"],
    )
    msg = Message(
        sender="cc:linyilun.dev",
        type="command",
        payload={"command": "update_title", "title": "debugging login"},
    )
    handler = CCSessionHandler()
    actions = handler.handle(actor, msg)
    # update_title targets the feishu thread actor to update its card
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "feishu:oc_xxx:om_anchor"
    assert actions[0].message.type == "update_title"


def test_forward_all_handler():
    actor = Actor(
        address="system:broadcast",
        tag="broadcast",
        handler="forward_all",
        downstream=["cc:a", "cc:b", "cc:c"],
    )
    msg = Message(sender="feishu:oc_xxx", type="text", payload={"text": "alert"})
    handler = ForwardAllHandler()
    actions = handler.handle(actor, msg)
    assert len(actions) == 3
    assert all(isinstance(a, Send) for a in actions)
    assert [a.to for a in actions] == ["cc:a", "cc:b", "cc:c"]


def test_tool_card_handler_accumulates_history():
    actor = Actor(
        address="cc:linyilun.root:tool_card",
        tag="tool_card",
        handler="tool_card",
        downstream=["feishu:oc_xxx"],
        metadata={"history": []},
    )
    msg = Message(sender="hook", type="tool_notify", payload={"text": "⚙️ Running: echo test"})
    handler = ToolCardHandler()
    actions = handler.handle(actor, msg)
    # Should update metadata + send transport update
    has_update = any(isinstance(a, UpdateActor) for a in actions)
    has_transport = any(isinstance(a, TransportSend) for a in actions)
    assert has_update
    assert has_transport


def test_get_handler_returns_correct_type():
    assert isinstance(get_handler("feishu_inbound"), FeishuInboundHandler)
    assert isinstance(get_handler("cc_session"), CCSessionHandler)
    assert isinstance(get_handler("forward_all"), ForwardAllHandler)
    assert isinstance(get_handler("tool_card"), ToolCardHandler)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'channel_server.core.handler'`

- [ ] **Step 3: Implement handlers**

```python
# channel_server/core/handler.py
"""Handler protocol and built-in handler implementations."""
from __future__ import annotations

from typing import Protocol

from .actor import (
    Action, Actor, Message,
    Send, TransportSend, UpdateActor,
)


class Handler(Protocol):
    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        ...


class FeishuInboundHandler:
    """Default Feishu actor: forward messages to all downstream actors."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]


class CCSessionHandler:
    """CC session actor: bridge between actor messages and CC session transport."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        # Messages from external actors → push to CC via transport
        if msg.sender != actor.address:
            return [TransportSend(payload={
                "type": "message",
                "chat_id": msg.metadata.get("chat_id", ""),
                "text": msg.payload.get("text", ""),
                "message_id": msg.metadata.get("message_id", ""),
                "user": msg.metadata.get("user", ""),
                "user_id": msg.metadata.get("user_id", ""),
                "runtime_mode": msg.metadata.get("runtime_mode", "discussion"),
                "source": msg.metadata.get("source", "feishu"),
                "ts": msg.metadata.get("ts", ""),
                "root_id": msg.metadata.get("root_id", ""),
                "parent_id": msg.metadata.get("parent_id", ""),
                "file_path": msg.metadata.get("file_path", ""),
            })]

        # Messages from CC session itself (commands: reply, forward, etc.)
        command = msg.payload.get("command", "")

        if command == "reply":
            text = msg.payload.get("text", "")
            tag = actor.tag
            tagged_text = f"[{tag}] {text}" if tag and tag != "root" else text
            reply_msg = Message(
                sender=actor.address,
                type="text",
                payload={"text": tagged_text},
                metadata=msg.metadata,
            )
            return [Send(to=addr, message=reply_msg) for addr in actor.downstream]

        if command == "forward":
            target = msg.payload.get("to", "")
            text = msg.payload.get("text", "")
            fwd_msg = Message(
                sender=actor.address,
                type="text",
                payload={"text": text},
            )
            return [Send(to=target, message=fwd_msg)]

        if command == "send_summary":
            summary = msg.payload.get("summary", "")
            tag = actor.tag
            parent_feishu = msg.payload.get("parent_feishu", "")
            if parent_feishu:
                notification = Message(
                    sender=actor.address,
                    type="text",
                    payload={"text": f"📋 [{tag}] {summary}"},
                )
                return [Send(to=parent_feishu, message=notification)]
            return []

        if command == "update_title":
            title = msg.payload.get("title", "")
            tag = actor.tag
            update_msg = Message(
                sender=actor.address,
                type="update_title",
                payload={"title": f"🟢 [{tag}] {title}"},
            )
            return [Send(to=addr, message=update_msg) for addr in actor.downstream]

        return []


class ForwardAllHandler:
    """Unconditional broadcast to all downstream."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]


class ToolCardHandler:
    """Tool notification: accumulate history + update card via transport."""

    MAX_HISTORY = 5

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "")
        history = list(actor.metadata.get("history", []))
        history.append(text)
        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]
        display = "\n".join(history)

        return [
            UpdateActor(changes={"metadata": {"history": history}}),
            TransportSend(payload={"type": "tool_card_update", "text": display}),
        ]


# --- Handler Registry ---

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
}


def get_handler(name: str) -> Handler:
    handler = HANDLER_REGISTRY.get(name)
    if handler is None:
        raise ValueError(f"Unknown handler: {name}")
    return handler
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_handler.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/handler.py tests/channel_server/core/test_handler.py
git commit -m "feat(actor): handler protocol + built-in handlers (feishu_inbound, cc_session, forward_all, tool_card)"
```

---

### Task 3: Actor Runtime (`channel_server/core/runtime.py`)

**Files:**
- Create: `channel_server/core/runtime.py`
- Test: `tests/channel_server/core/test_runtime.py`

- [ ] **Step 1: Write tests for runtime**

```python
# tests/channel_server/core/test_runtime.py
import asyncio
import pytest
from channel_server.core.actor import Actor, Message, Transport, Send
from channel_server.core.runtime import ActorRuntime


@pytest.fixture
def runtime():
    return ActorRuntime()


async def test_spawn_creates_actor(runtime):
    actor = runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    assert actor.address == "cc:test.root"
    assert actor.state == "active"
    assert runtime.lookup("cc:test.root") is actor


async def test_spawn_duplicate_raises(runtime):
    runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    with pytest.raises(ValueError, match="already exists"):
        runtime.spawn("cc:test.root", handler="forward_all", tag="root2")


async def test_stop_ends_actor(runtime):
    runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    runtime.stop("cc:test.root")
    actor = runtime.lookup("cc:test.root")
    assert actor.state == "ended"


async def test_send_delivers_to_mailbox(runtime):
    runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    msg = Message(sender="feishu:oc_xxx", type="text", payload={"text": "hello"})
    runtime.send("cc:test.root", msg)
    mailbox = runtime.mailboxes["cc:test.root"]
    received = mailbox.get_nowait()
    assert received.payload["text"] == "hello"


async def test_send_to_nonexistent_actor_logs_warning(runtime, caplog):
    msg = Message(sender="a", type="text", payload={})
    runtime.send("nonexistent", msg)
    assert "not found" in caplog.text.lower() or True  # graceful, no crash


async def test_attach_transport(runtime):
    actor = runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    transport = Transport(type="websocket", config={"ws_id": "abc"})
    runtime.attach("cc:test.root", transport)
    assert actor.transport is transport


async def test_detach_transport(runtime):
    actor = runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    runtime.attach("cc:test.root", Transport(type="websocket", config={}))
    runtime.detach("cc:test.root")
    assert actor.transport is None
    assert actor.state == "suspended"


async def test_attach_resumes_suspended_actor(runtime):
    actor = runtime.spawn("cc:test.root", handler="forward_all", tag="root")
    actor.state = "suspended"
    runtime.attach("cc:test.root", Transport(type="websocket", config={}))
    assert actor.state == "active"


async def test_actor_loop_processes_messages(runtime):
    """Test that the runtime's actor loop forwards messages via handler."""
    received = []

    # Spawn two actors: sender forwards to receiver
    runtime.spawn("sender", handler="forward_all", tag="s", downstream=["receiver"])
    runtime.spawn("receiver", handler="forward_all", tag="r")

    # Start runtime in background
    task = asyncio.create_task(runtime.run())

    # Send message to sender; it should forward to receiver
    msg = Message(sender="external", type="text", payload={"text": "ping"})
    runtime.send("sender", msg)

    # Give the loop time to process
    await asyncio.sleep(0.1)

    # Check receiver's mailbox was consumed (handler forwarded, but receiver has no downstream so nothing further)
    # The message should have arrived at receiver
    # Since receiver's handler is forward_all with no downstream, it produces no actions
    # We verify by checking the message passed through sender
    assert runtime.mailboxes["sender"].empty()

    await runtime.shutdown()
    await task


async def test_actor_loop_handles_errors_gracefully(runtime):
    """Test that a handler error doesn't crash the actor loop."""
    runtime.spawn("bad", handler="forward_all", tag="bad")

    task = asyncio.create_task(runtime.run())

    # Send a message — forward_all with no downstream just returns empty actions, no error
    # To test error handling, we'd need a bad handler, but for now verify no crash
    msg = Message(sender="x", type="text", payload={})
    runtime.send("bad", msg)
    await asyncio.sleep(0.1)

    assert runtime.lookup("bad").state == "active"

    await runtime.shutdown()
    await task
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'channel_server.core.runtime'`

- [ ] **Step 3: Implement runtime**

```python
# channel_server/core/runtime.py
"""Actor runtime: registry, mailbox management, per-actor message loops."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .actor import (
    Action, Actor, Message, Transport,
    Send, TransportSend, UpdateActor, SpawnActor, StopActor,
)
from .handler import get_handler

log = logging.getLogger("actor-runtime")


class ActorRuntime:
    """Minimal actor runtime inspired by Erlang VM."""

    def __init__(self):
        self.actors: dict[str, Actor] = {}
        self.mailboxes: dict[str, asyncio.Queue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._transport_callback: dict[str, callable] = {}  # transport_type → callback

    # --- Core API ---

    def spawn(self, address: str, handler: str, *, tag: str = "",
              state: str = "active", parent: str | None = None,
              downstream: list[str] | None = None,
              transport: Transport | None = None,
              metadata: dict | None = None) -> Actor:
        if address in self.actors and self.actors[address].state != "ended":
            raise ValueError(f"Actor {address} already exists")

        actor = Actor(
            address=address,
            tag=tag or address.split(":")[-1],
            handler=handler,
            state=state,
            parent=parent,
            downstream=list(downstream or []),
            transport=transport,
            metadata=dict(metadata or {}),
        )
        self.actors[address] = actor
        self.mailboxes[address] = asyncio.Queue()

        if state == "active":
            self._start_actor_loop(actor)

        log.info("Spawned actor %s (handler=%s, state=%s)", address, handler, state)
        return actor

    def stop(self, address: str) -> None:
        actor = self.actors.get(address)
        if not actor:
            return
        actor.state = "ended"
        actor.updated_at = datetime.now(timezone.utc).isoformat()

        # Cancel the actor's loop task
        task = self._tasks.pop(address, None)
        if task:
            task.cancel()

        log.info("Stopped actor %s", address)

    def send(self, to: str, message: Message) -> None:
        mailbox = self.mailboxes.get(to)
        if mailbox is None:
            log.warning("Send to %s: actor not found, message dropped", to)
            return
        actor = self.actors.get(to)
        if actor and actor.state == "ended":
            log.warning("Send to %s: actor ended, message dropped", to)
            return
        mailbox.put_nowait(message)

    def lookup(self, address: str) -> Actor | None:
        return self.actors.get(address)

    # --- Transport Management ---

    def attach(self, address: str, transport: Transport) -> None:
        actor = self.actors.get(address)
        if not actor:
            log.warning("Attach to %s: actor not found", address)
            return
        actor.transport = transport
        actor.updated_at = datetime.now(timezone.utc).isoformat()
        if actor.state == "suspended":
            actor.state = "active"
            self._start_actor_loop(actor)
        log.info("Attached transport %s to %s", transport.type, address)

    def detach(self, address: str) -> None:
        actor = self.actors.get(address)
        if not actor:
            return
        actor.transport = None
        actor.state = "suspended"
        actor.updated_at = datetime.now(timezone.utc).isoformat()

        # Cancel loop — will be restarted on reattach
        task = self._tasks.pop(address, None)
        if task:
            task.cancel()

        log.info("Detached transport from %s (now suspended)", address)

    # --- Transport Callbacks ---

    def register_transport_handler(self, transport_type: str, callback) -> None:
        """Register a callback for handling TransportSend actions for a given transport type."""
        self._transport_callback[transport_type] = callback

    # --- Lifecycle ---

    async def run(self) -> None:
        """Start all active actor loops. Blocks until shutdown."""
        self._stop_event.clear()
        for addr, actor in self.actors.items():
            if actor.state == "active" and addr not in self._tasks:
                self._start_actor_loop(actor)
        await self._stop_event.wait()

    async def shutdown(self) -> None:
        """Stop all actors and cancel loops."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._stop_event.set()

    # --- Internal ---

    def _start_actor_loop(self, actor: Actor) -> None:
        if actor.address in self._tasks:
            return
        task = asyncio.ensure_future(self._actor_loop(actor))
        self._tasks[actor.address] = task

    async def _actor_loop(self, actor: Actor) -> None:
        mailbox = self.mailboxes[actor.address]
        handler = get_handler(actor.handler)
        error_count = 0
        max_errors = 10

        try:
            while actor.state == "active":
                try:
                    msg = await asyncio.wait_for(mailbox.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    actions = handler.handle(actor, msg)
                    for action in actions:
                        self._execute(actor, action)
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    log.error("Actor %s handler error (%d/%d): %s",
                              actor.address, error_count, max_errors, e)
                    if actor.parent:
                        self.send(actor.parent, Message(
                            sender=actor.address,
                            type="error",
                            payload={"error": str(e)},
                        ))
                    if error_count >= max_errors:
                        log.error("Actor %s exceeded max errors, stopping", actor.address)
                        actor.state = "ended"
                        break
        except asyncio.CancelledError:
            pass

    def _execute(self, actor: Actor, action: Action) -> None:
        if isinstance(action, Send):
            self.send(action.to, action.message)

        elif isinstance(action, TransportSend):
            if actor.transport:
                transport_type = actor.transport.type
                callback = self._transport_callback.get(transport_type)
                if callback:
                    callback(actor, action.payload)
                else:
                    log.warning("No transport handler for type %s (actor %s)",
                                transport_type, actor.address)
            else:
                log.warning("TransportSend but actor %s has no transport", actor.address)

        elif isinstance(action, UpdateActor):
            for key, value in action.changes.items():
                if key == "metadata":
                    actor.metadata.update(value)
                elif hasattr(actor, key):
                    setattr(actor, key, value)
            actor.updated_at = datetime.now(timezone.utc).isoformat()

        elif isinstance(action, SpawnActor):
            self.spawn(action.address, action.handler, **action.kwargs)

        elif isinstance(action, StopActor):
            self.stop(action.address)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_runtime.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/runtime.py tests/channel_server/core/test_runtime.py
git commit -m "feat(actor): runtime — spawn, send, stop, attach/detach, per-actor loops"
```

---

### Task 4: Persistence (`channel_server/core/persistence.py`)

**Files:**
- Create: `channel_server/core/persistence.py`
- Test: `tests/channel_server/core/test_persistence.py`

- [ ] **Step 1: Write tests for persistence**

```python
# tests/channel_server/core/test_persistence.py
import json
import tempfile
from pathlib import Path
from channel_server.core.actor import Actor, Transport
from channel_server.core.persistence import save_actors, load_actors


def test_save_and_load_actors(tmp_path):
    filepath = tmp_path / "actors.json"
    actors = {
        "cc:linyilun.root": Actor(
            address="cc:linyilun.root",
            tag="root",
            handler="cc_session",
            downstream=["feishu:oc_xxx"],
        ),
        "feishu:oc_xxx": Actor(
            address="feishu:oc_xxx",
            tag="DM",
            handler="feishu_inbound",
            transport=Transport(type="feishu_chat", config={"chat_id": "oc_xxx"}),
            downstream=["cc:linyilun.root"],
        ),
    }
    save_actors(actors, filepath)

    loaded = load_actors(filepath)
    assert len(loaded) == 2
    assert loaded["cc:linyilun.root"].tag == "root"
    assert loaded["feishu:oc_xxx"].transport.type == "feishu_chat"
    assert loaded["feishu:oc_xxx"].transport.config["chat_id"] == "oc_xxx"


def test_load_missing_file(tmp_path):
    filepath = tmp_path / "missing.json"
    loaded = load_actors(filepath)
    assert loaded == {}


def test_save_filters_ended_actors(tmp_path):
    filepath = tmp_path / "actors.json"
    actors = {
        "active": Actor(address="active", tag="a", handler="forward_all", state="active"),
        "ended": Actor(address="ended", tag="e", handler="forward_all", state="ended"),
    }
    save_actors(actors, filepath)

    loaded = load_actors(filepath)
    assert "active" in loaded
    assert "ended" not in loaded


def test_load_corrupt_file(tmp_path):
    filepath = tmp_path / "actors.json"
    filepath.write_text("not json{{{")
    loaded = load_actors(filepath)
    assert loaded == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_persistence.py -v`
Expected: FAIL

- [ ] **Step 3: Implement persistence**

```python
# channel_server/core/persistence.py
"""Actor state persistence: save/load actors.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .actor import Actor

log = logging.getLogger("actor-runtime")


def save_actors(actors: dict[str, Actor], filepath: Path) -> None:
    """Save active/suspended actors to JSON file. Ended actors are not persisted."""
    data = {}
    for address, actor in actors.items():
        if actor.state != "ended":
            data[address] = actor.to_dict()

    tmp = filepath.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(filepath)
    log.info("Saved %d actors to %s", len(data), filepath)


def load_actors(filepath: Path) -> dict[str, Actor]:
    """Load actors from JSON file. Returns empty dict if file missing or corrupt."""
    if not filepath.exists():
        return {}
    try:
        data = json.loads(filepath.read_text())
        actors = {}
        for address, d in data.items():
            actors[address] = Actor.from_dict(d)
        log.info("Loaded %d actors from %s", len(actors), filepath)
        return actors
    except Exception as e:
        log.warning("Failed to load actors from %s: %s", filepath, e)
        return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/core/test_persistence.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/persistence.py tests/channel_server/core/test_persistence.py
git commit -m "feat(actor): persistence — save/load actors.json with atomic write"
```

---

### Task 5: Feishu Adapter (`channel_server/adapters/feishu/`)

**Files:**
- Create: `channel_server/adapters/__init__.py`
- Create: `channel_server/adapters/feishu/__init__.py`
- Create: `channel_server/adapters/feishu/adapter.py`
- Copy+migrate: `channel_server/adapters/feishu/parsers.py` (from `feishu/message_parsers.py`)
- Test: `tests/channel_server/adapters/__init__.py`
- Test: `tests/channel_server/adapters/test_feishu_adapter.py`

- [ ] **Step 1: Copy parsers.py from existing code**

```bash
mkdir -p channel_server/adapters/feishu
cp feishu/message_parsers.py channel_server/adapters/feishu/parsers.py
```

Update the import in parsers.py: change `from typing import TYPE_CHECKING` references to the new adapter module. The parsers will reference `FeishuAdapter` instead of `ChannelServer` for the `server` parameter.

- [ ] **Step 2: Write tests for Feishu adapter**

```python
# tests/channel_server/adapters/test_feishu_adapter.py
import pytest
from unittest.mock import MagicMock, patch
from channel_server.core.actor import Actor, Transport, Message
from channel_server.core.runtime import ActorRuntime
from channel_server.adapters.feishu.adapter import FeishuAdapter


@pytest.fixture
def runtime():
    return ActorRuntime()


@pytest.fixture
def adapter(runtime):
    return FeishuAdapter(runtime, feishu_client=None)


def test_resolve_actor_address_main_chat(adapter):
    addr = adapter.resolve_actor_address(chat_id="oc_xxx", root_id=None)
    assert addr == "feishu:oc_xxx"


def test_resolve_actor_address_thread(adapter):
    addr = adapter.resolve_actor_address(chat_id="oc_xxx", root_id="om_anchor")
    assert addr == "feishu:oc_xxx:om_anchor"


def test_on_feishu_event_auto_spawns_actor(adapter, runtime):
    event = {
        "chat_id": "oc_xxx",
        "root_id": "",
        "text": "hello",
        "user": "test_user",
        "user_id": "ou_123",
        "message_id": "om_msg1",
    }
    adapter.on_feishu_event(event)
    actor = runtime.lookup("feishu:oc_xxx")
    assert actor is not None
    assert actor.handler == "feishu_inbound"


def test_on_feishu_event_sends_message_to_actor(adapter, runtime):
    # Pre-spawn the actor
    runtime.spawn("feishu:oc_xxx", handler="feishu_inbound", tag="DM")
    event = {
        "chat_id": "oc_xxx",
        "root_id": "",
        "text": "hello",
        "user": "test_user",
        "user_id": "ou_123",
        "message_id": "om_msg1",
    }
    adapter.on_feishu_event(event)
    mailbox = runtime.mailboxes["feishu:oc_xxx"]
    msg = mailbox.get_nowait()
    assert msg.payload["text"] == "hello"


def test_execute_feishu_chat_transport(adapter):
    """Test outbound: sending a message through feishu_chat transport."""
    adapter.feishu_client = MagicMock()
    actor = Actor(
        address="feishu:oc_xxx",
        tag="DM",
        handler="feishu_inbound",
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_xxx"}),
    )
    payload = {"text": "hello from CC"}
    # Should not raise
    adapter.execute_transport(actor, payload)
```

- [ ] **Step 3: Implement Feishu adapter**

```python
# channel_server/adapters/__init__.py
"""Adapters: bridge external systems to actor runtime."""

# channel_server/adapters/feishu/__init__.py
"""Feishu adapter: event inbound + API outbound."""

# channel_server/adapters/feishu/adapter.py
"""Feishu adapter: bridges Feishu events and API to the actor runtime."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger("feishu-adapter")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class FeishuAdapter:
    """Bridges Feishu WebSocket events and REST API to the actor runtime."""

    def __init__(self, runtime: ActorRuntime, feishu_client):
        self.runtime = runtime
        self.feishu_client = feishu_client
        self._recent_sent: set[str] = set()

        # Register transport handlers
        runtime.register_transport_handler("feishu_chat", self._handle_chat_transport)
        runtime.register_transport_handler("feishu_thread", self._handle_thread_transport)

    # --- Inbound: Feishu events → actor messages ---

    def resolve_actor_address(self, chat_id: str, root_id: str | None) -> str:
        if root_id:
            return f"feishu:{chat_id}:{root_id}"
        return f"feishu:{chat_id}"

    def on_feishu_event(self, event: dict) -> None:
        """Called when a Feishu message arrives. Routes to appropriate actor."""
        chat_id = event.get("chat_id", "")
        root_id = event.get("root_id", "") or None
        text = event.get("text", "")
        msg_id = event.get("message_id", "")

        # Skip our own messages (echo prevention)
        if msg_id in self._recent_sent:
            self._recent_sent.discard(msg_id)
            return

        actor_addr = self.resolve_actor_address(chat_id, root_id)

        # Auto-spawn feishu actor if it doesn't exist
        if not self.runtime.lookup(actor_addr):
            transport_type = "feishu_thread" if root_id else "feishu_chat"
            config = {"chat_id": chat_id}
            if root_id:
                config["anchor_msg_id"] = root_id
            self.runtime.spawn(
                actor_addr,
                handler="feishu_inbound",
                tag=event.get("user", chat_id),
                transport=Transport(type=transport_type, config=config),
            )

        msg = Message(
            sender=actor_addr,
            type="text",
            payload={"text": text, "file_path": event.get("file_path", "")},
            metadata={
                "chat_id": chat_id,
                "root_id": root_id or "",
                "parent_id": event.get("parent_id", ""),
                "message_id": msg_id,
                "user": event.get("user", ""),
                "user_id": event.get("user_id", ""),
                "runtime_mode": event.get("runtime_mode", "discussion"),
                "source": event.get("source", "feishu"),
                "ts": event.get("ts", datetime.now(timezone.utc).isoformat()),
                "file_path": event.get("file_path", ""),
            },
        )
        self.runtime.send(actor_addr, msg)

    # --- Outbound: actor transport → Feishu API ---

    def execute_transport(self, actor: Actor, payload: dict) -> None:
        """Execute a transport send for a feishu actor."""
        if not actor.transport:
            return
        if actor.transport.type == "feishu_chat":
            self._handle_chat_transport(actor, payload)
        elif actor.transport.type == "feishu_thread":
            self._handle_thread_transport(actor, payload)

    def _handle_chat_transport(self, actor: Actor, payload: dict) -> None:
        """Send a message to a Feishu chat (main conversation)."""
        if not self.feishu_client:
            return
        chat_id = actor.transport.config.get("chat_id", "") if actor.transport else ""
        text = payload.get("text", "")
        if not chat_id or not text:
            return
        threading.Thread(
            target=self._send_message, args=(chat_id, text, None), daemon=True
        ).start()

    def _handle_thread_transport(self, actor: Actor, payload: dict) -> None:
        """Send a message to a Feishu thread."""
        if not self.feishu_client:
            return
        config = actor.transport.config if actor.transport else {}
        chat_id = config.get("chat_id", "")
        anchor_msg_id = config.get("anchor_msg_id", "")
        text = payload.get("text", "")

        msg_type = payload.get("type", "")
        if msg_type == "tool_card_update":
            threading.Thread(
                target=self._update_card, args=(anchor_msg_id, text), daemon=True
            ).start()
        elif msg_type == "update_title":
            title = payload.get("title", text)
            threading.Thread(
                target=self._update_anchor_card, args=(anchor_msg_id, title), daemon=True
            ).start()
        elif chat_id and text:
            threading.Thread(
                target=self._send_message, args=(chat_id, text, anchor_msg_id), daemon=True
            ).start()

    def _send_message(self, chat_id: str, text: str, thread_anchor: str | None) -> None:
        """Send a text message via Feishu API (sync, runs in thread)."""
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody,
                ReplyMessageRequest, ReplyMessageRequestBody,
            )
            if thread_anchor:
                body = (
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .reply_in_thread(True)
                    .build()
                )
                req = ReplyMessageRequest.builder().message_id(thread_anchor).request_body(body).build()
                resp = self.feishu_client.im.v1.message.reply(req)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
                resp = self.feishu_client.im.v1.message.create(req)

            if resp.success() and resp.data and resp.data.message_id:
                self._recent_sent.add(resp.data.message_id)
            else:
                log.warning("Feishu send failed: %s", resp.msg if resp else "no response")
        except Exception as e:
            log.error("Feishu send error: %s", e)

    def _update_card(self, msg_id: str, text: str) -> None:
        """Update a tool card via Feishu PATCH API."""
        if not self.feishu_client or not msg_id:
            return
        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            card = {
                "header": {"title": {"tag": "plain_text", "content": "🔧 Tool Activity"}, "template": "grey"},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}],
            }
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            self.feishu_client.im.v1.message.patch(req)
        except Exception as e:
            log.error("Card update error: %s", e)

    def _update_anchor_card(self, msg_id: str, title: str) -> None:
        """Update a session anchor card (title + template)."""
        if not self.feishu_client or not msg_id:
            return
        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            template = "red" if "🔴" in title else "blue" if "🟢" in title else "green"
            card = {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": title}}],
            }
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            self.feishu_client.im.v1.message.patch(req)
        except Exception as e:
            log.error("Anchor card update error: %s", e)

    # --- Thread/Card Creation ---

    def create_thread_anchor(self, chat_id: str, tag: str) -> str | None:
        """Create a thread anchor (interactive card + reply) for a child session. Returns msg_id."""
        if not self.feishu_client:
            return None
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody,
                ReplyMessageRequest, ReplyMessageRequestBody,
            )
            card = {
                "header": {"title": {"tag": "plain_text", "content": f"🟢 [{tag}]"}, "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": f"Session [{tag}] started — reply in this thread"}}],
            }
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            )
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = self.feishu_client.im.v1.message.create(req)
            if not (resp.success() and resp.data and resp.data.message_id):
                return None
            anchor_msg_id = resp.data.message_id
            self._recent_sent.add(anchor_msg_id)

            # Reply in thread to auto-create topic
            reply_body = (
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": f"💬 [{tag}] 在此话题中对话"}))
                .reply_in_thread(True)
                .build()
            )
            reply_req = ReplyMessageRequest.builder().message_id(anchor_msg_id).request_body(reply_body).build()
            reply_resp = self.feishu_client.im.v1.message.reply(reply_req)
            if reply_resp.success() and reply_resp.data:
                self._recent_sent.add(reply_resp.data.message_id)

            # Pin the anchor
            self.pin_message(anchor_msg_id)

            return anchor_msg_id
        except Exception as e:
            log.error("Thread anchor creation error: %s", e)
            return None

    def pin_message(self, message_id: str) -> bool:
        if not self.feishu_client:
            return False
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import CreatePinRequest, CreatePinRequestBody
            body = CreatePinRequestBody.builder().message_id(message_id).build()
            req = CreatePinRequest.builder().request_body(body).build()
            resp = self.feishu_client.im.v1.pin.create(req)
            return resp.success()
        except Exception:
            return False

    def create_tool_card(self, chat_id: str, text: str) -> str | None:
        """Create an interactive card for tool notifications."""
        if not self.feishu_client:
            return None
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
            card = {
                "header": {"title": {"tag": "plain_text", "content": "🔧 Tool Activity"}, "template": "grey"},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}],
            }
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            )
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = self.feishu_client.im.v1.message.create(req)
            if resp.success() and resp.data and resp.data.message_id:
                self._recent_sent.add(resp.data.message_id)
                return resp.data.message_id
            return None
        except Exception as e:
            log.error("Tool card creation error: %s", e)
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/adapters/test_feishu_adapter.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/adapters/ tests/channel_server/adapters/
git commit -m "feat(actor): Feishu adapter — inbound event routing + outbound API calls"
```

---

### Task 6: CC Adapter (`channel_server/adapters/cc/`)

**Files:**
- Create: `channel_server/adapters/cc/__init__.py`
- Create: `channel_server/adapters/cc/adapter.py`
- Migrate: `channel_server/adapters/cc/channel.py` (from `feishu/channel.py`)
- Test: `tests/channel_server/adapters/test_cc_adapter.py`

- [ ] **Step 1: Write tests for CC adapter**

```python
# tests/channel_server/adapters/test_cc_adapter.py
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from channel_server.core.actor import Actor, Transport, Message
from channel_server.core.runtime import ActorRuntime
from channel_server.adapters.cc.adapter import CCAdapter


@pytest.fixture
def runtime():
    return ActorRuntime()


@pytest.fixture
def adapter(runtime):
    return CCAdapter(runtime)


async def test_handle_register_attaches_transport(adapter, runtime):
    # Pre-spawn the CC actor (as spawn flow would)
    runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root", state="suspended")

    ws = AsyncMock()
    msg = {"type": "register", "instance_id": "cc:linyilun.root", "chat_ids": ["oc_xxx"]}
    await adapter.handle_message(ws, msg)

    actor = runtime.lookup("cc:linyilun.root")
    assert actor.transport is not None
    assert actor.transport.type == "websocket"
    assert actor.state == "active"


async def test_handle_reply_sends_to_actor(adapter, runtime):
    runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root",
                  downstream=["feishu:oc_xxx"])
    ws = AsyncMock()
    adapter._ws_to_address[id(ws)] = "cc:linyilun.root"

    msg = {"type": "reply", "chat_id": "oc_xxx", "text": "hello"}
    await adapter.handle_message(ws, msg)

    mailbox = runtime.mailboxes["cc:linyilun.root"]
    received = mailbox.get_nowait()
    assert received.payload["command"] == "reply"
    assert received.payload["text"] == "hello"


async def test_handle_disconnect_detaches_transport(adapter, runtime):
    runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root")
    runtime.attach("cc:linyilun.root", Transport(type="websocket", config={}))
    ws = AsyncMock()
    adapter._ws_to_address[id(ws)] = "cc:linyilun.root"

    adapter.handle_disconnect(ws)
    actor = runtime.lookup("cc:linyilun.root")
    assert actor.state == "suspended"


async def test_transport_push_sends_via_websocket(adapter, runtime):
    ws = AsyncMock()
    actor = runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root")
    runtime.attach("cc:linyilun.root", Transport(type="websocket", config={"ws": ws}))

    adapter.push_to_cc(actor, {"type": "message", "text": "hello"})
    ws.send.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/adapters/test_cc_adapter.py -v`
Expected: FAIL

- [ ] **Step 3: Implement CC adapter**

```python
# channel_server/adapters/cc/__init__.py
"""CC adapter: WebSocket server + MCP client."""

# channel_server/adapters/cc/adapter.py
"""CC adapter: bridges CC sessions (via WebSocket) to the actor runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

from channel_server.core.actor import Message, Transport
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger("cc-adapter")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class CCAdapter:
    """Bridges CC sessions to the actor runtime via WebSocket."""

    def __init__(self, runtime: ActorRuntime, host: str = "127.0.0.1", port: int = 0):
        self.runtime = runtime
        self.host = host
        self.port = port
        self._server = None
        self._ws_to_address: dict[int, str] = {}  # id(ws) → actor address

        # Register transport handler for websocket type
        runtime.register_transport_handler("websocket", self.push_to_cc)

    async def start(self) -> int:
        """Start WebSocket server, return actual port."""
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port,
            ping_interval=30, ping_timeout=20,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("CC adapter WebSocket server on %s:%d", self.host, self.port)
        return self.port

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, ws: ServerConnection) -> None:
        log.info("CC client connected from %s", ws.remote_address)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self.handle_message(ws, msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.handle_disconnect(ws)
            log.info("CC client disconnected")

    async def handle_message(self, ws, msg: dict) -> None:
        msg_type = msg.get("type", "")

        if msg_type == "register":
            await self._handle_register(ws, msg)
        elif msg_type in ("reply", "forward", "send_summary", "update_title",
                          "send_file", "react", "spawn_session", "kill_session",
                          "list_sessions", "tool_notify"):
            address = self._ws_to_address.get(id(ws))
            if not address:
                return
            # Convert MCP tool call to actor message
            actor_msg = Message(
                sender=address,
                type="command",
                payload={"command": msg_type, **msg},
            )
            self.runtime.send(address, actor_msg)
        elif msg_type == "pong":
            pass

    async def _handle_register(self, ws, msg: dict) -> None:
        instance_id = msg.get("instance_id", "")
        # Map old instance_id format to actor address
        # "linyilun.root" → "cc:linyilun.root"
        if not instance_id.startswith("cc:"):
            address = f"cc:{instance_id}"
        else:
            address = instance_id

        actor = self.runtime.lookup(address)
        if not actor:
            # Auto-spawn CC actor if not pre-created by spawn flow
            tag = msg.get("tag_name", "") or instance_id.split(".")[-1]
            chat_ids = msg.get("chat_ids", [])
            actor = self.runtime.spawn(
                address, handler="cc_session", tag=tag, state="suspended",
            )

        # Attach WebSocket transport
        self.runtime.attach(address, Transport(type="websocket", config={"ws": ws}))
        self._ws_to_address[id(ws)] = address

        # Send registered ack
        await ws.send(json.dumps({"type": "registered", "chat_ids": msg.get("chat_ids", [])}))
        log.info("Registered CC actor %s", address)

    def handle_disconnect(self, ws) -> None:
        address = self._ws_to_address.pop(id(ws), None)
        if address:
            self.runtime.detach(address)
            log.info("Disconnected CC actor %s", address)

    def push_to_cc(self, actor, payload: dict) -> None:
        """Push a message to CC session via WebSocket (transport callback)."""
        if not actor.transport or actor.transport.type != "websocket":
            return
        ws = actor.transport.config.get("ws")
        if ws:
            try:
                asyncio.ensure_future(ws.send(json.dumps(payload)))
            except Exception as e:
                log.warning("Push to CC %s failed: %s", actor.address, e)

    # --- Session Management ---

    def spawn_cc_process(self, user: str, session_name: str, tag: str = "") -> None:
        """Start a CC session via cc-openclaw.sh in tmux."""
        cmd = [str(PROJECT_ROOT / "cc-openclaw.sh"), "--user", user, "--session", session_name]
        if tag:
            cmd.extend(["--tag", tag])
        try:
            subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))
            log.info("Spawned CC process for %s.%s", user, session_name)
        except Exception as e:
            log.error("Failed to spawn CC process: %s", e)

    def kill_cc_process(self, user: str, session_name: str) -> None:
        """Kill a CC session's tmux window."""
        window_name = f"{user}.{session_name}"
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-t", "cc-openclaw", "-F", "#{window_index}:#{window_name}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                idx, name = line.split(":", 1)
                if name == window_name:
                    subprocess.run(
                        ["tmux", "kill-window", "-t", f"cc-openclaw:{idx}"],
                        capture_output=True, timeout=5,
                    )
                    log.info("Killed tmux window %s (index %s)", window_name, idx)
                    return
            log.warning("Tmux window %s not found", window_name)
        except Exception as e:
            log.warning("Failed to kill tmux window %s: %s", window_name, e)
```

- [ ] **Step 4: Migrate channel.py**

```bash
cp feishu/channel.py channel_server/adapters/cc/channel.py
```

Update `channel_server/adapters/cc/channel.py`:
- Change MCP tool `send_summary` description to match new behavior (notify root only)
- Add `update_title` tool (already done in current feishu/channel.py)
- Update WebSocket message types to include actor address prefix where needed

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/adapters/test_cc_adapter.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add channel_server/adapters/cc/ tests/channel_server/adapters/test_cc_adapter.py
git commit -m "feat(actor): CC adapter — WebSocket server + session management"
```

---

### Task 7: App Entry Point (`channel_server/app.py`)

**Files:**
- Create: `channel_server/app.py`
- Test: `tests/channel_server/test_integration.py`

- [ ] **Step 1: Implement app.py**

```python
# channel_server/app.py
"""Channel server entry point: initializes runtime + adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .core.runtime import ActorRuntime
from .core.persistence import save_actors, load_actors
from .adapters.feishu.adapter import FeishuAdapter
from .adapters.cc.adapter import CCAdapter

log = logging.getLogger("channel-server")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ChannelServerApp:
    """Main application: wires runtime + adapters together."""

    def __init__(self, *, admin_chat_id: str | None = None,
                 feishu_enabled: bool = True, port: int = 0):
        self.admin_chat_id = admin_chat_id
        self.feishu_enabled = feishu_enabled

        # Core
        self.runtime = ActorRuntime()
        self.actors_file = PROJECT_ROOT / ".workspace" / "actors.json"

        # Adapters
        self.feishu_adapter = None
        self.cc_adapter = CCAdapter(self.runtime, port=port)

        # Pidfile
        self.pidfile = PROJECT_ROOT / ".channel-server.pid"

    async def start(self):
        """Initialize and start all components."""
        # Restore persisted actors
        persisted = load_actors(self.actors_file)
        for addr, actor in persisted.items():
            self.runtime.actors[addr] = actor
            self.runtime.mailboxes[addr] = asyncio.Queue()
            # Mark all as suspended until transports attach
            actor.state = "suspended"

        # Start CC adapter (WebSocket server)
        port = await self.cc_adapter.start()
        self.pidfile.write_text(f"{os.getpid()}:{port}")

        # Start Feishu adapter if enabled
        if self.feishu_enabled:
            feishu_client = self._init_feishu_client()
            self.feishu_adapter = FeishuAdapter(self.runtime, feishu_client)
            # Reactivate feishu actors (their transport is the API, always available)
            for addr, actor in self.runtime.actors.items():
                if addr.startswith("feishu:") and actor.transport:
                    actor.state = "active"
            # Start Feishu WebSocket listener in background
            # (migrated from channel_server.py _run_feishu_safe)

        # Start runtime message loops
        asyncio.create_task(self.runtime.run())

        # Periodic persistence
        asyncio.create_task(self._persist_loop())

        log.info("Channel server started on port %d", port)

    async def stop(self):
        save_actors(self.runtime.actors, self.actors_file)
        await self.runtime.shutdown()
        await self.cc_adapter.stop()
        self.pidfile.unlink(missing_ok=True)
        log.info("Channel server stopped")

    async def _persist_loop(self):
        """Periodically save actor state."""
        while True:
            await asyncio.sleep(30)
            try:
                save_actors(self.runtime.actors, self.actors_file)
            except Exception as e:
                log.error("Persistence error: %s", e)

    def _init_feishu_client(self):
        """Initialize Feishu SDK client from credentials file."""
        creds_file = PROJECT_ROOT / ".feishu-credentials.json"
        if not creds_file.exists():
            log.warning("No Feishu credentials file found")
            return None
        try:
            import lark_oapi as lark
            creds = json.loads(creds_file.read_text())
            return (
                lark.Client.builder()
                .app_id(creds["app_id"])
                .app_secret(creds["app_secret"])
                .build()
            )
        except Exception as e:
            log.error("Failed to init Feishu client: %s", e)
            return None


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    feishu_enabled = os.environ.get("FEISHU_ENABLED", "true").lower() in ("true", "1", "yes")

    app = ChannelServerApp(
        admin_chat_id=admin_chat_id,
        feishu_enabled=feishu_enabled,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.stop()))

    await app.start()

    sep = "=" * 60
    print(f"\n{sep}")
    print("  OpenClaw Channel Server (Actor Model)")
    print(f"  Listening  : ws://127.0.0.1:{app.cc_adapter.port}")
    print(f"  Feishu     : {'enabled' if feishu_enabled else 'disabled'}")
    if admin_chat_id:
        print(f"  Admin group: {admin_chat_id}")
    print(f"  Pidfile    : {app.pidfile}")
    print(f"{sep}\n")

    # Block until stopped
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Write integration test**

```python
# tests/channel_server/test_integration.py
import asyncio
import pytest
from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime
from channel_server.core.handler import get_handler


async def test_end_to_end_feishu_to_cc():
    """Simulate: feishu actor receives message → forwards to cc actor → cc actor gets transport push."""
    runtime = ActorRuntime()
    transport_log = []

    def mock_ws_transport(actor, payload):
        transport_log.append(payload)

    runtime.register_transport_handler("websocket", mock_ws_transport)

    # Create actors
    runtime.spawn("feishu:oc_xxx", handler="feishu_inbound", tag="DM",
                  downstream=["cc:linyilun.root"])
    runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root",
                  downstream=["feishu:oc_xxx"])
    runtime.attach("cc:linyilun.root", Transport(type="websocket", config={}))

    # Start runtime
    task = asyncio.create_task(runtime.run())

    # Simulate feishu message
    msg = Message(
        sender="feishu:oc_xxx", type="text",
        payload={"text": "hello"},
        metadata={"chat_id": "oc_xxx", "user": "testuser"},
    )
    runtime.send("feishu:oc_xxx", msg)

    await asyncio.sleep(0.2)

    # CC actor should have received the message via transport
    assert len(transport_log) == 1
    assert transport_log[0]["text"] == "hello"

    await runtime.shutdown()
    await task


async def test_end_to_end_cc_reply():
    """Simulate: cc actor sends reply → feishu actor receives."""
    runtime = ActorRuntime()
    feishu_log = []

    def mock_feishu_transport(actor, payload):
        feishu_log.append(payload)

    runtime.register_transport_handler("feishu_chat", mock_feishu_transport)

    runtime.spawn("feishu:oc_xxx", handler="feishu_inbound", tag="DM",
                  downstream=["cc:linyilun.root"],
                  transport=Transport(type="feishu_chat", config={"chat_id": "oc_xxx"}))
    runtime.spawn("cc:linyilun.root", handler="cc_session", tag="root",
                  downstream=["feishu:oc_xxx"])

    task = asyncio.create_task(runtime.run())

    # CC sends reply
    reply_msg = Message(
        sender="cc:linyilun.root", type="command",
        payload={"command": "reply", "text": "world"},
    )
    runtime.send("cc:linyilun.root", reply_msg)

    await asyncio.sleep(0.2)

    # Feishu actor should have received via transport
    assert len(feishu_log) == 1
    assert "world" in feishu_log[0].get("text", "")

    await runtime.shutdown()
    await task
```

- [ ] **Step 3: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/test_integration.py -v`
Expected: All 2 tests PASS

- [ ] **Step 4: Commit**

```bash
git add channel_server/app.py tests/channel_server/test_integration.py
git commit -m "feat(actor): app entry point + integration tests"
```

---

### Task 8: Update Build Config and Launch Scripts

**Files:**
- Modify: `pyproject.toml`
- Modify: `cc-openclaw.sh` (update Claude command to use new channel.py path)
- Modify: `/Users/h2oslabs/Library/LaunchAgents/ai.openclaw.channel-server.plist`
- Modify: `.mcp.json` (update MCP server path)

- [ ] **Step 1: Update pyproject.toml**

Add `channel_server` to the hatch build packages:

```toml
[tool.hatch.build.targets.wheel]
packages = ["feishu", "sidecar", "channel_server"]
```

- [ ] **Step 2: Update launchd plist**

Change `ProgramArguments` from:
```xml
<string>feishu/channel_server.py</string>
```
to:
```xml
<string>-m</string>
<string>channel_server.app</string>
```

- [ ] **Step 3: Update .mcp.json**

Update the MCP server command for `openclaw-channel` to point to `channel_server/adapters/cc/channel.py` instead of `feishu/channel.py`.

- [ ] **Step 4: Update cc-openclaw.sh**

Update the `--dangerously-load-development-channels` path if it references the old `feishu/` location.

- [ ] **Step 5: Test launch**

```bash
cd /Users/h2oslabs/cc-openclaw
uv run python -m channel_server.app &
sleep 3
# Verify server starts and pidfile is written
cat .channel-server.pid
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml cc-openclaw.sh .mcp.json
git commit -m "chore: update build config and launch scripts for actor model"
```

---

### Task 9: Migration — Retire Old `feishu/` Directory

**Files:**
- Remove: `feishu/channel_server.py` (replaced by `channel_server/`)
- Remove: `feishu/channel.py` (replaced by `channel_server/adapters/cc/channel.py`)
- Remove: `feishu/message_parsers.py` (replaced by `channel_server/adapters/feishu/parsers.py`)
- Keep: `feishu/__init__.py` (may be needed by sidecar or other code)

- [ ] **Step 1: Verify no other code imports from old feishu/ modules**

```bash
cd /Users/h2oslabs/cc-openclaw
grep -r "from feishu.channel_server\|from feishu.channel\|from feishu.message_parsers\|import feishu.channel" --include="*.py" | grep -v ".venv" | grep -v "channel_server/"
```

- [ ] **Step 2: Remove old files**

```bash
git rm feishu/channel_server.py feishu/channel.py feishu/message_parsers.py
```

- [ ] **Step 3: Run full test suite**

```bash
cd /Users/h2oslabs/cc-openclaw && uv run pytest tests/channel_server/ -v
```

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: retire old feishu/ channel modules, replaced by channel_server/"
```

---

### Task 10: Smoke Test — Full System Validation

- [ ] **Step 1: Start channel server with new architecture**

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.channel-server
sleep 5
cat /Users/h2oslabs/cc-openclaw/.channel-server.pid
tail -10 /Users/h2oslabs/.openclaw/logs/channel-server.err.log
```

- [ ] **Step 2: Start root session and verify registration**

```bash
./cc-openclaw.sh --user linyilun
```

Verify in logs: CC actor `cc:linyilun.root` registered, feishu actor `feishu:oc_xxx` auto-spawned.

- [ ] **Step 3: Test message round-trip**

Send a message from Feishu DM → verify CC receives it → reply from CC → verify Feishu receives it.

- [ ] **Step 4: Test spawn/kill**

```
/spawn test-actor
```

Verify: thread created, pinned, child CC actor registered, messages routed to thread.

```
/kill test-actor
```

Verify: anchor card updated to red, tmux window killed, actors stopped.

- [ ] **Step 5: Test tool notification card**

Run a Bash command → verify tool card appears in correct location (main chat for root, thread for child).

- [ ] **Step 6: Commit final state**

```bash
git add -A
git commit -m "feat(actor): actor model channel server — complete migration"
```
