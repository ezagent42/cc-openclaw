# Session Manager & Admin Command Refactoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify session initialization through a session-mgr actor, intercept admin commands before they reach the LLM, fix tool card placement, and route MCP replies through actor downstream.

**Architecture:** A new `system:session-mgr` actor orchestrates all session lifecycle commands (spawn/kill/sessions). AdminHandler becomes a pure router that intercepts slash commands. Each actor domain handles its own initialization via `on_spawn` lifecycle hooks. MCP reply/send_file routes through actor downstream instead of bypassing to Feishu directly.

**Tech Stack:** Python 3.11+, asyncio, pytest, websockets

**Spec:** `docs/superpowers/specs/2026-04-16-session-mgr-admin-cmds-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `channel_server/core/handlers/session_mgr.py` | SessionMgrHandler — spawn/kill/sessions/init orchestration |
| Modify | `channel_server/core/handler.py` | Add `on_spawn` to Handler protocol, register session_mgr handler |
| Modify | `channel_server/core/runtime.py` | Add `on_spawn` hook in `spawn()`, pass runtime to `handle()` |
| Modify | `channel_server/core/actor.py` | No changes needed (existing Action types sufficient) |
| Modify | `channel_server/core/handlers/admin.py` | Intercept session commands → Send to session-mgr |
| Modify | `channel_server/core/handlers/cc.py` | Add `on_spawn` for tmux process management |
| Modify | `channel_server/core/handlers/feishu.py` | Add `on_spawn` for thread anchor + tool card creation |
| Modify | `channel_server/adapters/cc/adapter.py` | Simplify _handle_register, forward _handle_spawn/_kill/_list to session-mgr |
| Modify | `channel_server/adapters/feishu/adapter.py` | Add root_id to create_tool_card, inject chat_id in messages, new transport actions |
| Modify | `channel_server/app.py` | Spawn session-mgr actor at startup |
| Create | `tests/channel_server/core/handlers/test_session_mgr.py` | Tests for SessionMgrHandler |
| Modify | `tests/channel_server/core/test_handler.py` | Update AdminHandler tests for new routing |
| Modify | `tests/channel_server/core/test_runtime.py` | Test on_spawn lifecycle hook |

---

### Task 0: Add app_id to Feishu Actor Addresses

**Goal:** Change feishu actor address format from `feishu:{chat_id}` to `feishu:{app_id}:{chat_id}` (and thread actors from `feishu:{chat_id}:{anchor}` to `feishu:{app_id}:{chat_id}:{anchor}`). This prepares for multi-app support without needing to change addresses later.

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py` (address construction in `on_feishu_event`, `start_feishu_ws`)
- Modify: `channel_server/adapters/cc/adapter.py` (address references in `_handle_register` wiring, `_handle_spawn`)
- Modify: `channel_server/core/handlers/cc.py` (any address pattern matching for feishu actors)
- Modify: `channel_server/core/handlers/feishu.py` (`on_stop` address parsing)
- Modify: `channel_server/app.py` (admin feishu actor address at startup)
- Modify: `tests/` (update all feishu actor addresses in tests)

- [ ] **Step 1: Find all feishu actor address construction points**

Search for `feishu:` string literals across the codebase to find every place that constructs or matches feishu actor addresses. Key locations:
- `feishu/adapter.py`: `on_feishu_event` constructs `feishu:{chat_id}` and `feishu:{chat_id}:{root_id}`
- `cc/adapter.py`: `_handle_register` wires to `feishu:{chat_id}`, `_handle_spawn` creates `feishu:{chat_id}:{anchor_msg_id}`
- `app.py`: startup spawns `feishu:{admin_chat_id}`
- `handlers/cc.py`: `on_stop` and `send_summary` match `feishu:` prefix in downstream
- Tests: address strings in test fixtures

- [ ] **Step 2: Store app_id on FeishuAdapter**

In `channel_server/adapters/feishu/adapter.py`, store app_id from `start_feishu_ws`:

```python
def start_feishu_ws(self, app_id: str, app_secret: str) -> None:
    self.app_id = app_id  # NEW: store for address construction
    # ... rest unchanged
```

- [ ] **Step 3: Update feishu adapter address construction**

In `on_feishu_event` and everywhere a feishu actor address is built in the feishu adapter, change:
- `f"feishu:{chat_id}"` → `f"feishu:{self.app_id}:{chat_id}"`
- `f"feishu:{chat_id}:{root_id}"` → `f"feishu:{self.app_id}:{chat_id}:{root_id}"`

- [ ] **Step 4: Update CC adapter address references**

In `channel_server/adapters/cc/adapter.py`:
- `_handle_register`: the feishu address for wiring needs the app_id. Since CC adapter has `self.feishu_adapter`, get it via `self.feishu_adapter.app_id`:
  ```python
  feishu_addr = f"feishu:{self.feishu_adapter.app_id}:{chat_id}"
  ```
- `_handle_spawn`: same pattern for feishu thread actor addresses

- [ ] **Step 5: Update handlers that match feishu addresses**

In `channel_server/core/handlers/cc.py`:
- `on_stop`: iterates `actor.downstream` looking for `feishu:` prefix — this still works (prefix match unchanged)
- `send_summary`: finds parent's downstream starting with `feishu:` — still works

In `channel_server/core/handlers/feishu.py`:
- `on_stop`: checks `actor.transport.type == "feishu_thread"` — no address parsing, still works

- [ ] **Step 6: Update app.py startup**

```python
# Where admin feishu actor is spawned:
feishu_addr = f"feishu:{self.feishu_adapter.app_id}:{self.admin_chat_id}"
```

- [ ] **Step 7: Update SessionMgrHandler address construction**

In the session_mgr handler (Task 3), thread actor addresses will use:
```python
thread_addr = f"feishu:{app_id}:{chat_id}:thread:{session_name}"
```
The `app_id` needs to be in the message payload. Add it alongside `chat_id` in the feishu adapter's message injection (Task 6).

- [ ] **Step 8: Update all tests**

Search for `feishu:` in all test files and update addresses to include a test app_id like `feishu:test_app:oc_test`. Key files:
- `tests/channel_server/core/test_handler.py`
- `tests/channel_server/core/handlers/test_feishu.py`
- `tests/channel_server/test_integration.py`
- `tests/channel_server/adapters/test_cc_adapter.py`
- `tests/channel_server/adapters/test_feishu_adapter.py`

- [ ] **Step 9: Run full test suite**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 10: Commit**

```bash
git add channel_server/ tests/
git commit -m "refactor: add app_id to feishu actor addresses (feishu:{app_id}:{chat_id})"
```

---

### Task 1: Add `on_spawn` Lifecycle Hook to Runtime

**Files:**
- Modify: `channel_server/core/handler.py:16-27`
- Modify: `channel_server/core/runtime.py:40-74`
- Test: `tests/channel_server/core/test_runtime.py`

- [ ] **Step 1: Write failing test for on_spawn hook**

Add to `tests/channel_server/core/test_runtime.py`:

```python
@pytest.mark.asyncio
async def test_spawn_calls_on_spawn_hook():
    """on_spawn hook actions are executed when actor is spawned."""
    from channel_server.core.actor import TransportSend
    from channel_server.core.handler import HANDLER_REGISTRY

    class SpawnHookHandler:
        def handle(self, actor, msg, runtime=None):
            return []
        def on_spawn(self, actor):
            return [TransportSend(payload={"action": "init", "tag": actor.tag})]
        def on_stop(self, actor):
            return []

    rt = ActorRuntime()
    HANDLER_REGISTRY["test_spawn_hook"] = SpawnHookHandler()
    transport_calls = []

    async def mock_transport(actor, payload):
        transport_calls.append(payload)
        return None

    rt.register_transport_handler("test", mock_transport)

    try:
        # spawn() is sync — on_spawn hooks are scheduled as async tasks
        rt.spawn("test:actor", "test_spawn_hook", tag="mytag",
                 transport=Transport(type="test", config={}))
        # Give the deferred on_spawn task time to execute
        await asyncio.sleep(0.2)
        assert len(transport_calls) == 1
        assert transport_calls[0]["action"] == "init"
        assert transport_calls[0]["tag"] == "mytag"
    finally:
        del HANDLER_REGISTRY["test_spawn_hook"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_runtime.py::test_spawn_calls_on_spawn_hook -v`
Expected: FAIL — `on_spawn` not called

- [ ] **Step 3: Add on_spawn to Handler protocol**

In `channel_server/core/handler.py`, add `on_spawn` method to the `Handler` protocol (after line 22):

```python
class Handler(Protocol):
    def handle(self, actor: Actor, msg: Message) -> list[Action]: ...

    def on_spawn(self, actor: Actor) -> list[Action]:
        """Lifecycle callback invoked when an actor is spawned."""
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        """Lifecycle callback invoked when an actor is stopped."""
        return []
```

- [ ] **Step 4: Add on_spawn execution to runtime.spawn()**

In `channel_server/core/runtime.py`, add on_spawn hook execution to `spawn()`. **Critical: spawn() stays sync.** The on_spawn hook actions are scheduled as a deferred async task, avoiding a breaking change to 30+ call sites (including `feishu/adapter.py:295` which calls spawn via `call_soon_threadsafe` from a background thread — making spawn async would silently break this).

Add this at the end of `spawn()`, before the return statement (after `_maybe_start_loop`):

```python
def spawn(self, address: str, handler: str, *, tag: str = "",
          state: str = "active", parent: str | None = None,
          downstream: list[str] | None = None,
          transport: Transport | None = None,
          metadata: dict | None = None) -> Actor:
    # ... existing code unchanged ...

    if state == "active" and self._stop_event and not self._stop_event.is_set():
        self._maybe_start_loop(actor)

    # NEW: schedule on_spawn hook as deferred async task
    self._schedule_on_spawn(actor)

    return actor
```

Add the new helper method:

```python
def _schedule_on_spawn(self, actor: Actor) -> None:
    """Schedule on_spawn lifecycle hook as an async task."""
    from channel_server.core.handler import get_handler
    try:
        h = get_handler(actor.handler)
    except ValueError:
        return  # unknown handler — skip
    if not hasattr(h, "on_spawn"):
        return
    actions = h.on_spawn(actor)
    if not actions:
        return

    async def _run_on_spawn():
        for action in actions:
            try:
                await self._execute(actor, action)
            except Exception:
                logger.exception("on_spawn action failed for %s", actor.address)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_on_spawn())
    except RuntimeError:
        pass  # no running loop (e.g., during tests) — skip
```

**Key design choices:**
- `spawn()` stays sync — zero call site changes needed
- `on_spawn()` handler method is sync (returns list[Action]) — same as `on_stop()`
- Action execution is async (TransportSend calls feishu API) — scheduled as task
- Failure in on_spawn is logged, not fatal — actor still exists
- No event loop = no hook execution (safe for sync tests)

Also add error handling in `_execute` around SpawnActor for race conditions (spec requirement):

```python
# In _execute(), around the SpawnActor case:
elif isinstance(action, SpawnActor):
    try:
        self.spawn(action.address, action.handler, **action.kwargs)
    except ValueError as e:
        logger.warning("SpawnActor failed (duplicate?): %s", e)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_runtime.py -v`
Expected: ALL PASS (existing tests unaffected — spawn is still sync)

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS — no call site changes needed

- [ ] **Step 7: Commit**

```bash
git add channel_server/core/handler.py channel_server/core/runtime.py tests/channel_server/core/test_runtime.py
git commit -m "feat: add on_spawn lifecycle hook to runtime and handler protocol"
```

---

### Task 2: Pass Runtime to Handler.handle()

**Files:**
- Modify: `channel_server/core/runtime.py:189`
- Modify: `channel_server/core/handlers/admin.py`
- Modify: `channel_server/core/handlers/cc.py`
- Modify: `channel_server/core/handlers/feishu.py`
- Modify: `channel_server/core/handlers/forward.py`
- Modify: `channel_server/core/handlers/tool_card.py`
- Modify: `channel_server/core/handler.py`
- Test: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Write failing test**

Add to `tests/channel_server/core/test_handler.py`:

```python
def test_handler_receives_runtime_arg():
    """All handlers accept runtime as third argument."""
    from unittest.mock import MagicMock
    runtime = MagicMock()
    actor = make_actor(address="system:admin", handler="admin", downstream=["cc:user.root"])
    msg = make_msg(sender="feishu_user:u1", payload={"text": "hello"})

    # All handlers should accept runtime without error
    AdminHandler().handle(actor, msg, runtime)
    FeishuInboundHandler().handle(actor, msg, runtime)
    CCSessionHandler().handle(actor, msg, runtime)
    ForwardAllHandler().handle(actor, msg, runtime)
    ToolCardHandler().handle(actor, msg, runtime)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py::test_handler_receives_runtime_arg -v`
Expected: FAIL — handlers don't accept 3rd arg

- [ ] **Step 3: Update Handler protocol and all handler signatures**

In `channel_server/core/handler.py`, update the Protocol:

```python
class Handler(Protocol):
    def handle(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None" = None) -> list[Action]: ...
```

Update each handler's `handle` method signature to accept `runtime=None`:

`channel_server/core/handlers/admin.py`:
```python
def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
```

`channel_server/core/handlers/cc.py`:
```python
def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
```
Note: CCSessionHandler already has `self._runtime` from constructor. Keep both — the constructor-injected one is for backward compat, the parameter is the new standard.

`channel_server/core/handlers/feishu.py`:
```python
def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
```

`channel_server/core/handlers/forward.py`:
```python
def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
```

`channel_server/core/handlers/tool_card.py`:
```python
def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
```

- [ ] **Step 4: Update runtime._actor_loop to pass runtime**

In `channel_server/core/runtime.py`, in `_actor_loop` (line 189), change:
```python
actions = handler.handle(actor, msg)
```
to:
```python
actions = handler.handle(actor, msg, self)
```

- [ ] **Step 5: Run all tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handler.py channel_server/core/handlers/ channel_server/core/runtime.py tests/channel_server/core/test_handler.py
git commit -m "feat: pass runtime as third arg to handler.handle()"
```

---

### Task 3: Create SessionMgrHandler

**Files:**
- Create: `channel_server/core/handlers/session_mgr.py`
- Modify: `channel_server/core/handler.py:34-40` (register in HANDLER_REGISTRY)
- Create: `tests/channel_server/core/handlers/test_session_mgr.py`

- [ ] **Step 1: Write failing tests for /spawn**

Create `tests/channel_server/core/handlers/test_session_mgr.py`:

```python
"""Tests for SessionMgrHandler."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from channel_server.core.actor import Actor, Message, Send, SpawnActor, StopActor
from channel_server.core.handlers.session_mgr import SessionMgrHandler


def make_actor(
    address: str = "system:session-mgr",
    tag: str = "session-mgr",
    handler: str = "session_mgr",
    downstream: list[str] | None = None,
    metadata: dict | None = None,
) -> Actor:
    return Actor(
        address=address, tag=tag, handler=handler,
        downstream=downstream or [], metadata=metadata or {},
    )


def make_msg(text: str, user: str = "testuser", chat_id: str = "oc_test123",
             app_id: str = "test_app") -> Message:
    return Message(
        sender="feishu_user:u1",
        payload={"text": text, "user": user, "chat_id": chat_id, "app_id": app_id},
    )


def make_runtime(actors: dict[str, Actor] | None = None) -> MagicMock:
    rt = MagicMock()
    _actors = actors or {}
    rt.lookup.side_effect = lambda addr: _actors.get(addr)
    rt.actors = _actors
    return rt


# ---------------------------------------------------------------------------
# /spawn — new session
# ---------------------------------------------------------------------------

def test_spawn_new_session():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = make_msg("/spawn dev")
    actions = handler.handle(make_actor(), msg, rt)

    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawn_actions) == 2  # feishu thread + cc session

    cc_spawn = next(a for a in spawn_actions if a.address == "cc:testuser.dev")
    assert cc_spawn.handler == "cc_session"
    assert cc_spawn.kwargs.get("state") == "suspended"
    assert cc_spawn.kwargs.get("parent") == "cc:testuser.root"

    thread_spawn = next(a for a in spawn_actions if "thread:" in a.address)
    assert thread_spawn.handler == "feishu_inbound"


def test_spawn_with_tag():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = make_msg("/spawn voice-widget --tag Voice")
    actions = handler.handle(make_actor(), msg, rt)

    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    cc_spawn = next(a for a in spawn_actions if a.address == "cc:testuser.voice-widget")
    assert cc_spawn.kwargs["metadata"]["tag"] == "Voice"


def test_spawn_already_active():
    existing = Actor(address="cc:testuser.dev", tag="dev", handler="cc_session", state="active")
    rt = make_runtime({"cc:testuser.dev": existing})
    handler = SessionMgrHandler()
    msg = make_msg("/spawn dev")
    actions = handler.handle(make_actor(), msg, rt)

    # Should return error reply, no SpawnActor
    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawn_actions) == 0
    send_actions = [a for a in actions if isinstance(a, Send)]
    assert len(send_actions) == 1
    assert "already active" in send_actions[0].message.payload.get("text", "").lower()


def test_spawn_resume_suspended():
    existing = Actor(address="cc:testuser.dev", tag="dev", handler="cc_session", state="suspended")
    rt = make_runtime({"cc:testuser.dev": existing})
    handler = SessionMgrHandler()
    msg = make_msg("/spawn dev")
    actions = handler.handle(make_actor(), msg, rt)

    # Should send resume message, no SpawnActor
    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawn_actions) == 0
    send_actions = [a for a in actions if isinstance(a, Send)]
    resume = next((a for a in send_actions if a.to == "cc:testuser.dev"), None)
    assert resume is not None
    assert resume.message.payload.get("action") == "resume"


def test_spawn_missing_name():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = make_msg("/spawn")
    actions = handler.handle(make_actor(), msg, rt)

    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawn_actions) == 0
    send_actions = [a for a in actions if isinstance(a, Send)]
    assert len(send_actions) == 1  # error reply


# ---------------------------------------------------------------------------
# /kill
# ---------------------------------------------------------------------------

def test_kill_active_session():
    existing = Actor(
        address="cc:testuser.dev", tag="dev", handler="cc_session",
        state="active", downstream=["feishu:oc_test123:om_anchor"],
    )
    rt = make_runtime({"cc:testuser.dev": existing})
    handler = SessionMgrHandler()
    msg = make_msg("/kill dev")
    actions = handler.handle(make_actor(), msg, rt)

    stop_actions = [a for a in actions if isinstance(a, StopActor)]
    stop_addrs = {a.address for a in stop_actions}
    assert "cc:testuser.dev" in stop_addrs


def test_kill_nonexistent():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = make_msg("/kill dev")
    actions = handler.handle(make_actor(), msg, rt)

    stop_actions = [a for a in actions if isinstance(a, StopActor)]
    assert len(stop_actions) == 0
    send_actions = [a for a in actions if isinstance(a, Send)]
    assert len(send_actions) == 1  # error reply


def test_kill_cannot_kill_root():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = make_msg("/kill root")
    actions = handler.handle(make_actor(), msg, rt)

    stop_actions = [a for a in actions if isinstance(a, StopActor)]
    assert len(stop_actions) == 0


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------

def test_sessions_lists_all():
    actors = {
        "cc:testuser.root": Actor(address="cc:testuser.root", tag="root", handler="cc_session", state="active"),
        "cc:testuser.dev": Actor(address="cc:testuser.dev", tag="dev", handler="cc_session", state="active"),
        "cc:testuser.old": Actor(address="cc:testuser.old", tag="old", handler="cc_session", state="ended"),
        "feishu:oc_chat": Actor(address="feishu:oc_chat", tag="chat", handler="feishu_inbound"),
    }
    rt = make_runtime(actors)
    handler = SessionMgrHandler()
    msg = make_msg("/sessions")
    actions = handler.handle(make_actor(), msg, rt)

    send_actions = [a for a in actions if isinstance(a, Send)]
    assert len(send_actions) == 1
    text = send_actions[0].message.payload["text"]
    assert "root" in text
    assert "dev" in text
    # ended sessions should still show (but marked)
    # feishu actors should NOT show


# ---------------------------------------------------------------------------
# init_session (from register)
# ---------------------------------------------------------------------------

def test_init_session_root():
    rt = make_runtime()
    handler = SessionMgrHandler()
    msg = Message(
        sender="cc:testuser.root",
        payload={"user": "testuser", "session_name": "root",
                 "chat_id": "oc_test123", "mode": "root"},
    )
    msg.metadata = {"type": "init_session"}
    actions = handler.handle(make_actor(), msg, rt)

    spawn_actions = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawn_actions) == 1
    assert spawn_actions[0].address == "tool_card:testuser.root"
    assert spawn_actions[0].handler == "tool_card"


# ---------------------------------------------------------------------------
# get_handler registry
# ---------------------------------------------------------------------------

def test_session_mgr_in_registry():
    from channel_server.core.handler import get_handler
    handler = get_handler("session_mgr")
    assert isinstance(handler, SessionMgrHandler)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/handlers/test_session_mgr.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Create SessionMgrHandler**

Create `channel_server/core/handlers/session_mgr.py`:

```python
"""Session lifecycle orchestrator.

Handles /spawn, /kill, /sessions commands and init_session from register.
All session lifecycle flows (from both Feishu text and CC MCP tools)
converge here.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    SpawnActor,
    StopActor,
    Transport,
)

if TYPE_CHECKING:
    from channel_server.core.runtime import ActorRuntime

_MAX_CHILDREN = 5


def _parse_spawn_args(text: str) -> tuple[str, str]:
    """Parse '/spawn name [--tag tag]' → (name, tag)."""
    parts = text.split()
    name = parts[1] if len(parts) > 1 else ""
    tag = ""
    if "--tag" in parts:
        idx = parts.index("--tag")
        if idx + 1 < len(parts):
            tag = parts[idx + 1]
    return name, tag


def _parse_kill_args(text: str) -> str:
    """Parse '/kill name' → name."""
    parts = text.split()
    return parts[1] if len(parts) > 1 else ""


def _reply(app_id: str, chat_id: str, text: str) -> Send:
    """Build a reply action that sends text to the feishu chat actor."""
    return Send(
        to=f"feishu:{app_id}:{chat_id}",
        message=Message(sender="system:session-mgr", payload={"text": text}),
    )


class SessionMgrHandler:
    """Orchestrates session lifecycle: spawn, kill, list, init."""

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        text = msg.payload.get("text", "").strip()
        msg_type = msg.metadata.get("type", "")

        if msg_type == "init_session":
            return self._handle_init(actor, msg, runtime)

        if text.startswith("/spawn"):
            return self._handle_spawn(actor, msg, runtime)
        elif text.startswith("/kill"):
            return self._handle_kill(actor, msg, runtime)
        elif text.startswith("/sessions"):
            return self._handle_sessions(actor, msg, runtime)

        return []

    def on_spawn(self, actor: Actor) -> list[Action]:
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        return []

    def _handle_spawn(self, actor: Actor, msg: Message, runtime: ActorRuntime | None) -> list[Action]:
        user = msg.payload.get("user", "")
        chat_id = msg.payload.get("chat_id", "")
        app_id = msg.payload.get("app_id", "")
        text = msg.payload.get("text", "")
        session_name, tag = _parse_spawn_args(text)
        tag = tag or session_name

        if not session_name:
            return [_reply(app_id, chat_id, "Usage: /spawn <name> [--tag <tag>]")]

        if not runtime:
            return [_reply(app_id, chat_id, "Internal error: no runtime")]

        cc_addr = f"cc:{user}.{session_name}"
        existing = runtime.lookup(cc_addr)

        # Already active
        if existing and existing.state == "active":
            return [_reply(app_id, chat_id, f"Session '{session_name}' is already active")]

        # Resume suspended
        if existing and existing.state == "suspended":
            return [
                Send(
                    to=cc_addr,
                    message=Message(
                        sender="system:session-mgr",
                        payload={"action": "resume", "tag": tag, "chat_id": chat_id},
                    ),
                ),
                _reply(app_id, chat_id, f"Session '{session_name}' resumed"),
            ]

        # Check child limit
        prefix = f"cc:{user}."
        active = sum(
            1 for a in runtime.actors.values()
            if a.address.startswith(prefix) and a.state not in ("ended",)
        )
        if active >= _MAX_CHILDREN:
            return [_reply(app_id, chat_id, f"Max sessions ({_MAX_CHILDREN}) reached")]

        # New session: spawn feishu thread actor + cc actor
        app_id = msg.payload.get("app_id", "")
        thread_addr = f"feishu:{app_id}:{chat_id}:thread:{session_name}"

        actions: list[Action] = [
            SpawnActor(
                address=thread_addr,
                handler="feishu_inbound",
                kwargs={
                    "tag": tag,
                    "metadata": {"chat_id": chat_id, "tag": tag, "mode": "child"},
                    "transport": Transport(type="feishu_thread", config={"chat_id": chat_id}),
                },
            ),
            SpawnActor(
                address=cc_addr,
                handler="cc_session",
                kwargs={
                    "tag": tag,
                    "state": "suspended",
                    "parent": f"cc:{user}.root",
                    "downstream": [thread_addr],
                    "metadata": {"chat_id": chat_id, "tag": tag},
                },
            ),
            _reply(app_id, chat_id, f"Session '{session_name}' spawned"),
        ]

        # Wire thread → cc (use runtime.wire after spawn)
        # This is done by setting downstream on cc_addr above,
        # and wiring thread → cc via runtime after actions execute.
        # The runtime will wire after SpawnActor completes.

        return actions

    def _handle_kill(self, actor: Actor, msg: Message, runtime: ActorRuntime | None) -> list[Action]:
        user = msg.payload.get("user", "")
        chat_id = msg.payload.get("chat_id", "")
        app_id = msg.payload.get("app_id", "")
        text = msg.payload.get("text", "")
        session_name = _parse_kill_args(text)

        if not session_name:
            return [_reply(app_id, chat_id, "Usage: /kill <name>")]

        if session_name == "root":
            return [_reply(app_id, chat_id, "Cannot kill root session")]

        if not runtime:
            return [_reply(app_id, chat_id, "Internal error: no runtime")]

        cc_addr = f"cc:{user}.{session_name}"
        existing = runtime.lookup(cc_addr)

        if not existing or existing.state == "ended":
            return [_reply(app_id, chat_id, f"Session '{session_name}' not found")]

        actions: list[Action] = [StopActor(address=cc_addr)]

        # Also stop the feishu thread actor if it exists in downstream
        for ds_addr in existing.downstream:
            if ds_addr.startswith("feishu:") and ":thread:" in ds_addr:
                actions.append(StopActor(address=ds_addr))

        actions.append(_reply(app_id, chat_id, f"Session '{session_name}' killed"))
        return actions

    def _handle_sessions(self, actor: Actor, msg: Message, runtime: ActorRuntime | None) -> list[Action]:
        user = msg.payload.get("user", "")
        chat_id = msg.payload.get("chat_id", "")
        app_id = msg.payload.get("app_id", "")

        if not runtime:
            return [_reply(app_id, chat_id, "Internal error: no runtime")]

        prefix = f"cc:{user}."
        sessions = [
            a for a in runtime.actors.values()
            if a.address.startswith(prefix) and a.state != "ended"
        ]

        if not sessions:
            return [_reply(app_id, chat_id, "No active sessions")]

        lines = []
        for s in sorted(sessions, key=lambda a: a.address):
            icon = "\U0001f7e2" if s.state == "active" else "\U0001f7e1"
            name = s.address.split(".")[-1] if "." in s.address else s.address
            lines.append(f"{icon} {s.tag or name} ({s.state})")

        return [_reply(app_id, chat_id, "\n".join(lines))]

    def _handle_init(self, actor: Actor, msg: Message, runtime: ActorRuntime | None) -> list[Action]:
        """Handle init_session from _handle_register for root sessions."""
        chat_id = msg.payload.get("chat_id", "")
        user = msg.payload.get("user", "")
        mode = msg.payload.get("mode", "")

        if mode == "root":
            return [
                SpawnActor(
                    address=f"tool_card:{user}.root",
                    handler="tool_card",
                    kwargs={
                        "tag": "root",
                        "metadata": {"chat_id": chat_id, "mode": "root"},
                        "transport": Transport(
                            type="feishu_chat",
                            config={"chat_id": chat_id},
                        ),
                    },
                ),
            ]
        return []
```

- [ ] **Step 4: Register in HANDLER_REGISTRY**

In `channel_server/core/handler.py`, add import and registry entry:

```python
from channel_server.core.handlers.session_mgr import SessionMgrHandler

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
    "admin": AdminHandler(),
    "session_mgr": SessionMgrHandler(),  # NEW
}
```

Also update `channel_server/core/handlers/__init__.py` to export `SessionMgrHandler` for consistency with existing pattern (all handlers are importable from the package).

- [ ] **Step 5: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/handlers/test_session_mgr.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handlers/session_mgr.py channel_server/core/handler.py tests/channel_server/core/handlers/test_session_mgr.py
git commit -m "feat: add SessionMgrHandler for session lifecycle orchestration"
```

---

### Task 4: Update AdminHandler to Intercept Session Commands

**Files:**
- Modify: `channel_server/core/handlers/admin.py:19-58`
- Modify: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Update test for new routing behavior**

In `tests/channel_server/core/test_handler.py`, replace `test_admin_session_command_passthrough` (lines 423-435):

```python
def test_admin_session_command_routes_to_session_mgr():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    for cmd in ("/spawn research", "/kill research", "/sessions"):
        msg = make_msg(sender="feishu_user:u1", payload={"text": cmd})
        actions = AdminHandler().handle(actor, msg)
        assert len(actions) == 1
        assert isinstance(actions[0], Send)
        assert actions[0].to == "system:session-mgr"
        assert actions[0].message is msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py::test_admin_session_command_routes_to_session_mgr -v`
Expected: FAIL — still routing to downstream

- [ ] **Step 3: Update AdminHandler**

In `channel_server/core/handlers/admin.py`, change the session command handling (around line 28) from forwarding to downstream to forwarding to session-mgr:

```python
# Session commands → route to session-mgr
if text.startswith(self.SESSION_COMMANDS):
    return [Send(to="system:session-mgr", message=msg)]
```

This replaces the existing line that forwards to `actor.downstream`.

- [ ] **Step 4: Run all admin handler tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py -k admin -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/handlers/admin.py tests/channel_server/core/test_handler.py
git commit -m "feat: AdminHandler routes session commands to session-mgr"
```

---

### Task 5: Spawn session-mgr Actor at Startup

**Files:**
- Modify: `channel_server/app.py`

- [ ] **Step 1: Add session-mgr spawn in app startup**

In `channel_server/app.py`, in the `start()` method, after the admin actor spawn (around line 107), add:

```python
# Spawn session-mgr actor (global singleton, no transport)
if not self.runtime.lookup("system:session-mgr"):
    await self.runtime.spawn("system:session-mgr", "session_mgr", tag="session-mgr")
```

- [ ] **Step 2: Verify server starts**

Run: `cd /Users/h2oslabs/cc-openclaw && timeout 5 python -m channel_server.app 2>&1 || true`
Expected: Server starts without error (may timeout, that's fine)

- [ ] **Step 3: Commit**

```bash
git add channel_server/app.py
git commit -m "feat: spawn system:session-mgr actor at server startup"
```

---

### Task 6: Inject chat_id and app_id in Feishu Adapter Messages

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`

- [ ] **Step 1: Add chat_id and app_id injection in on_feishu_event**

In `channel_server/adapters/feishu/adapter.py`, in the `on_feishu_event` method (around line 267-320), ensure the Message payload includes `chat_id`, `app_id`, `user_id`, and `user` fields when constructing the message delivered to the actor:

Find where the Message is constructed and add:

```python
payload = {
    "text": text,
    "msg_type": msg_type,
    "chat_id": chat_id,        # NEW: inject for downstream handlers
    "app_id": self.app_id,     # NEW: for multi-app address construction
    "user_id": sender_id,       # NEW
    "user": user_name,          # NEW: resolved display name
    # ... existing fields
}
```

The exact location depends on where Message is built — look for `Message(sender=..., payload=...)` in `on_feishu_event`.

- [ ] **Step 2: Verify no test regressions**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add channel_server/adapters/feishu/adapter.py
git commit -m "feat: inject chat_id/user_id/user in feishu message payload"
```

---

### Task 7: CC Adapter Forwards Session Commands to session-mgr

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py:102-133` (handle_message routing)

- [ ] **Step 1: Modify handle_message to forward session commands**

In `channel_server/adapters/cc/adapter.py`, in `handle_message` (lines 102-133), change the routing for `spawn_session`, `kill_session`, `list_sessions`:

```python
async def handle_message(self, ws, msg: dict) -> None:
    action = msg.get("action", "")
    if action == "register":
        await self._handle_register(ws, msg)
    elif action in ("spawn_session", "kill_session", "list_sessions"):
        await self._forward_session_cmd(ws, msg)
    elif action == "pong":
        pass
    # ... rest unchanged
```

- [ ] **Step 2: Create _forward_session_cmd method**

Add a new method that converts the WebSocket action into a message for session-mgr:

```python
async def _forward_session_cmd(self, ws, msg: dict) -> None:
    """Forward session command to system:session-mgr."""
    address = self._ws_to_address.get(id(ws))
    if not address:
        await ws.send(json.dumps({"action": "error", "message": "Not registered"}))
        return

    # Extract user from address cc:user.root -> user
    parts = address.replace("cc:", "").split(".")
    user = parts[0] if parts else "unknown"

    # Find chat_id from root actor's downstream feishu actor
    root_actor = self.runtime.lookup(address)
    chat_id = ""
    if root_actor:
        for ds_addr in root_actor.downstream:
            ds = self.runtime.lookup(ds_addr)
            if ds and ds.transport and ds.transport.type == "feishu_chat":
                chat_id = ds.transport.config.get("chat_id", "")
                break

    # Convert WS action to text command
    action = msg.get("action", "")
    session_name = msg.get("session_name", "")
    tag = msg.get("tag", "")

    if action == "spawn_session":
        text = f"/spawn {session_name}"
        if tag:
            text += f" --tag {tag}"
    elif action == "kill_session":
        text = f"/kill {session_name}"
    elif action == "list_sessions":
        text = "/sessions"
    else:
        return

    from channel_server.core.actor import Message
    self.runtime.send(
        "system:session-mgr",
        Message(
            sender=address,
            payload={"text": text, "user": user, "chat_id": chat_id},
        ),
    )

    # Send ack back to CC process
    await ws.send(json.dumps({
        "action": f"{action}_ack",
        "ok": True,
        "text": f"Command forwarded: {text}",
    }))
```

Note: The old `_handle_spawn`, `_handle_kill`, `_handle_list` methods remain temporarily for the rollback path. They will be removed in Task 11.

- [ ] **Step 3: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/ -v --timeout=30`
Expected: ALL PASS (some adapter tests may need updates)

- [ ] **Step 4: Commit**

```bash
git add channel_server/adapters/cc/adapter.py
git commit -m "feat: CC adapter forwards session commands to session-mgr"
```

---

### Task 8: Fix Tool Card Location (root_id support)

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py:647-670` (create_tool_card)

- [ ] **Step 1: Add root_id parameter to create_tool_card**

In `channel_server/adapters/feishu/adapter.py`, modify `create_tool_card` (lines 647-670) to accept optional `root_id`:

```python
async def create_tool_card(self, chat_id: str, text: str, root_id: str | None = None) -> str | None:
    """Create a tool card. If root_id is provided, create inside that thread."""
    card = self._build_tool_card(text)
    content = json.dumps(card)

    try:
        if root_id:
            # Create inside thread using ReplyMessageRequest
            req = (
                ReplyMessageRequest.builder()
                .message_id(root_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(content)
                    .reply_in_thread(True)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self.feishu_client.im.v1.message.reply, req
            )
        else:
            # Create in main chat
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(
                self.feishu_client.im.v1.message.create, req
            )

        if resp.success():
            return resp.data.message_id
        logger.error("create_tool_card failed: %s", resp.msg)
        return None
    except Exception:
        logger.exception("create_tool_card error")
        return None
```

- [ ] **Step 2: Add transport handler actions for thread anchor + tool card creation**

In `channel_server/adapters/feishu/adapter.py`, in `_handle_thread_transport` (lines 393-426), add cases for the new on_spawn actions:

```python
async def _handle_thread_transport(self, actor: Actor, payload: dict) -> dict | None:
    chat_id = actor.transport.config.get("chat_id", "")
    root_id = actor.transport.config.get("root_id", "")
    action = payload.get("action")

    if action == "create_thread_anchor":
        tag = payload.get("tag", "")
        anchor_msg_id = await self.create_thread_anchor(chat_id, tag)
        if anchor_msg_id:
            await self.pin_message(anchor_msg_id)
            # Update transport config with root_id for future messages
            actor.transport.config["root_id"] = anchor_msg_id
            return {"anchor_msg_id": anchor_msg_id}
        return None

    if action == "create_tool_card":
        tag = payload.get("tag", "")
        anchor = actor.metadata.get("anchor_msg_id", "")
        card_msg_id = await self.create_tool_card(chat_id, f"\U0001f7e2 [{tag}]", root_id=anchor or None)
        if card_msg_id:
            return {"card_msg_id": card_msg_id}
        return None

    # ... existing action handling below
```

- [ ] **Step 3: Verify no regressions**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add channel_server/adapters/feishu/adapter.py
git commit -m "feat: create_tool_card supports root_id for in-thread placement"
```

---

### Task 9: Add on_spawn to FeishuInboundHandler (Thread Actor Init)

**Files:**
- Modify: `channel_server/core/handlers/feishu.py`
- Test: `tests/channel_server/core/handlers/test_feishu.py`

- [ ] **Step 1: Write failing test**

Add to `tests/channel_server/core/handlers/test_feishu.py`:

```python
def test_feishu_inbound_on_spawn_child_creates_anchor_and_card():
    """on_spawn for child mode creates thread anchor + tool card."""
    from channel_server.core.actor import Transport, TransportSend

    actor = make_actor(
        address="feishu:oc_test:thread:dev",
        tag="dev",
        handler="feishu_inbound",
        metadata={"chat_id": "oc_test", "tag": "dev", "mode": "child"},
    )
    actor.transport = Transport(type="feishu_thread", config={"chat_id": "oc_test"})

    actions = FeishuInboundHandler().on_spawn(actor)
    assert len(actions) == 2

    anchor_action = actions[0]
    assert isinstance(anchor_action, TransportSend)
    assert anchor_action.payload["action"] == "create_thread_anchor"
    assert anchor_action.payload["tag"] == "dev"

    card_action = actions[1]
    assert isinstance(card_action, TransportSend)
    assert card_action.payload["action"] == "create_tool_card"
    assert card_action.payload["tag"] == "dev"


def test_feishu_inbound_on_spawn_no_mode_noop():
    """on_spawn without mode metadata returns empty."""
    actor = make_actor(handler="feishu_inbound")
    actions = FeishuInboundHandler().on_spawn(actor)
    assert actions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/handlers/test_feishu.py::test_feishu_inbound_on_spawn_child_creates_anchor_and_card -v`
Expected: FAIL — on_spawn returns empty

- [ ] **Step 3: Add on_spawn to FeishuInboundHandler**

In `channel_server/core/handlers/feishu.py`, add `on_spawn` method:

```python
def on_spawn(self, actor: Actor) -> list[Action]:
    """Create thread anchor + tool card for child sessions."""
    mode = actor.metadata.get("mode", "")
    if mode != "child":
        return []

    chat_id = actor.metadata.get("chat_id", "")
    tag = actor.metadata.get("tag", "")

    return [
        TransportSend(payload={
            "action": "create_thread_anchor",
            "chat_id": chat_id,
            "tag": tag,
        }),
        TransportSend(payload={
            "action": "create_tool_card",
            "chat_id": chat_id,
            "tag": tag,
        }),
    ]
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/handlers/test_feishu.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add channel_server/core/handlers/feishu.py tests/channel_server/core/handlers/test_feishu.py
git commit -m "feat: FeishuInboundHandler.on_spawn creates thread anchor + tool card"
```

---

### Task 10: Add on_spawn to CCSessionHandler (tmux Process)

**Files:**
- Modify: `channel_server/core/handlers/cc.py`
- Modify: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Write failing test**

Add to `tests/channel_server/core/test_handler.py`:

```python
def test_cc_session_on_spawn_emits_tmux_spawn():
    from channel_server.core.actor import TransportSend

    actor = make_actor(
        address="cc:testuser.dev",
        tag="dev",
        handler="cc_session",
        metadata={"chat_id": "oc_test", "tag": "dev"},
    )
    actions = CCSessionHandler().on_spawn(actor)
    assert len(actions) == 1
    assert isinstance(actions[0], TransportSend)
    assert actions[0].payload["action"] == "spawn_tmux"
    assert actions[0].payload["user"] == "testuser"
    assert actions[0].payload["session_name"] == "dev"


def test_cc_session_on_spawn_root_noop():
    """Root sessions don't spawn tmux via on_spawn (already running)."""
    actor = make_actor(
        address="cc:testuser.root",
        tag="root",
        handler="cc_session",
        metadata={"chat_id": "oc_test"},
    )
    actions = CCSessionHandler().on_spawn(actor)
    assert actions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py::test_cc_session_on_spawn_emits_tmux_spawn -v`
Expected: FAIL — on_spawn returns empty

- [ ] **Step 3: Add on_spawn to CCSessionHandler**

In `channel_server/core/handlers/cc.py`, add `on_spawn` method:

```python
def on_spawn(self, actor: Actor) -> list[Action]:
    """Spawn tmux process for child sessions."""
    # Root sessions are already running when they register — skip
    parts = actor.address.replace("cc:", "").split(".")
    if len(parts) < 2 or parts[1] == "root":
        return []

    user = parts[0]
    session_name = parts[1]
    tag = actor.metadata.get("tag", session_name)
    chat_id = actor.metadata.get("chat_id", "")

    return [
        TransportSend(payload={
            "action": "spawn_tmux",
            "user": user,
            "session_name": session_name,
            "tag": tag,
            "chat_id": chat_id,
        }),
    ]
```

- [ ] **Step 4: Register spawn_tmux transport handler in CC adapter**

In `channel_server/adapters/cc/adapter.py`, in `push_to_cc` (lines 211-221) or as a new transport handler, handle the `spawn_tmux` action:

```python
async def push_to_cc(self, actor: Actor, payload: dict) -> dict | None:
    action = payload.get("action")

    if action == "spawn_tmux":
        user = payload.get("user", "")
        session_name = payload.get("session_name", "")
        tag = payload.get("tag", "")
        chat_id = payload.get("chat_id", "")
        success = self.spawn_cc_process(user, session_name, tag=tag, chat_id=chat_id)
        return {"tmux_started": success}

    if action == "kill_tmux":
        user = payload.get("user", "")
        session_name = payload.get("session_name", "")
        self.kill_cc_process(user, session_name)
        return {"tmux_killed": True}

    # Existing: send JSON over WebSocket
    ws = self._address_to_ws.get(actor.address)
    if ws:
        await ws.send(json.dumps(payload))
    return None
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/core/test_handler.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handlers/cc.py channel_server/adapters/cc/adapter.py tests/channel_server/core/test_handler.py
git commit -m "feat: CCSessionHandler.on_spawn emits spawn_tmux, adapter handles it"
```

---

### Task 11: Simplify _handle_register + Remove Dead Code

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`

- [ ] **Step 1: Simplify _handle_register to send init_session**

In `channel_server/adapters/cc/adapter.py`, after the root session wiring in `_handle_register` (around line 178-189), add the init_session message:

```python
# If new root actor: notify session-mgr for tool card init
if session == "root" and chat_ids:
    chat_id = next((c for c in chat_ids if c != "*"), None)
    if chat_id:
        from channel_server.core.actor import Message as ActorMessage
        self.runtime.send(
            "system:session-mgr",
            ActorMessage(
                sender=address,
                payload={
                    "user": user,
                    "session_name": "root",
                    "chat_id": chat_id,
                    "mode": "root",
                },
                metadata={"type": "init_session"},
            ),
        )
```

- [ ] **Step 2: Remove old _handle_spawn, _handle_kill, _handle_list methods**

Delete the following methods from `channel_server/adapters/cc/adapter.py`:
- `_handle_spawn` (lines 274-445) — replaced by SessionMgrHandler + on_spawn hooks
- `_handle_kill` (lines 447-489) — replaced by SessionMgrHandler._handle_kill
- `_handle_list` (lines 491-521) — replaced by SessionMgrHandler._handle_sessions

Keep `spawn_cc_process` and `kill_cc_process` — they're still needed by the transport handler.

- [ ] **Step 3: Run all tests**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS (adapter tests may need updates if they tested the old methods directly)

- [ ] **Step 4: Commit**

```bash
git add channel_server/adapters/cc/adapter.py
git commit -m "refactor: simplify _handle_register, remove _handle_spawn/_kill/_list"
```

---

### Task 12: Integration Test — End-to-End Session Lifecycle

**Files:**
- Modify: `tests/channel_server/test_integration.py`

- [ ] **Step 1: Write integration test**

Add to `tests/channel_server/test_integration.py`:

```python
@pytest.mark.asyncio
async def test_session_lifecycle_via_session_mgr():
    """Full lifecycle: spawn via session-mgr, verify actors, kill."""
    from channel_server.core.runtime import ActorRuntime
    from channel_server.core.actor import Message, Transport

    rt = ActorRuntime()

    # Register transport handlers (mock — don't actually call feishu/tmux)
    transport_log = []
    async def mock_feishu_thread(actor, payload):
        transport_log.append(("feishu_thread", payload))
        if payload.get("action") == "create_thread_anchor":
            return {"anchor_msg_id": "om_anchor_test"}
        if payload.get("action") == "create_tool_card":
            return {"card_msg_id": "om_card_test"}
        return None

    async def mock_ws(actor, payload):
        transport_log.append(("ws", payload))
        if payload.get("action") == "spawn_tmux":
            return {"tmux_started": True}
        return None

    rt.register_transport_handler("feishu_thread", mock_feishu_thread)
    rt.register_transport_handler("websocket", mock_ws)
    rt.register_transport_handler("feishu_chat", lambda a, p: None)

    # Spawn session-mgr
    await rt.spawn("system:session-mgr", "session_mgr", tag="session-mgr")

    # Simulate /spawn dev command
    rt.send(
        "system:session-mgr",
        Message(
            sender="system:admin",
            payload={"text": "/spawn dev", "user": "alice", "chat_id": "oc_chat1"},
        ),
    )

    # Let the runtime process
    await asyncio.sleep(0.5)

    # Verify: feishu thread actor created
    thread_actor = rt.lookup("feishu:oc_chat1:thread:dev")
    assert thread_actor is not None

    # Verify: cc actor created (suspended, waiting for WS)
    cc_actor = rt.lookup("cc:alice.dev")
    assert cc_actor is not None
    assert cc_actor.state == "suspended"

    # Verify: on_spawn hooks ran
    anchor_calls = [c for c in transport_log if c[1].get("action") == "create_thread_anchor"]
    assert len(anchor_calls) >= 1

    tmux_calls = [c for c in transport_log if c[1].get("action") == "spawn_tmux"]
    assert len(tmux_calls) >= 1

    # Kill the session
    rt.send(
        "system:session-mgr",
        Message(
            sender="system:admin",
            payload={"text": "/kill dev", "user": "alice", "chat_id": "oc_chat1"},
        ),
    )

    await asyncio.sleep(0.5)

    cc_actor = rt.lookup("cc:alice.dev")
    assert cc_actor is None or cc_actor.state == "ended"

    await rt.shutdown()
```

- [ ] **Step 2: Run integration test**

Run: `cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/test_integration.py::test_session_lifecycle_via_session_mgr -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/channel_server/test_integration.py
git commit -m "test: integration test for session lifecycle via session-mgr"
```

---

### Task 13: MCP Reply/Send_file Thread Routing (Deferred)

> **Note:** This task addresses spec Goal 5 but is architecturally independent from Tasks 1-12. It can be implemented in a follow-up PR if needed. The core session-mgr refactoring works without it.

**Problem:** `channel.py` ChannelClient methods (`send_reply`, `send_file`) send WebSocket messages with `action: "reply"` / `action: "send_file"` directly. The CC adapter's `_route_to_actor` delivers these to the CC actor, and CCSessionHandler (cc.py:64-65) already routes them to downstream feishu actors. However, the response from the feishu actor goes through the feishu transport handler which uses `root_id` from the actor's transport config — meaning child session replies already go to the correct thread IF the CC actor's downstream is correctly wired to the feishu_thread actor.

**Verification needed:** Check if the existing routing already works correctly after Tasks 1-11 are complete (since spawn now creates proper downstream wiring). If child session replies still go to main chat, the fix is in the feishu adapter's transport handler to use `root_id` from the thread actor's transport config.

**Files:**
- Potentially modify: `channel_server/adapters/cc/channel.py`
- Potentially modify: `channel_server/core/handlers/cc.py`

- [ ] **Step 1: Test after Tasks 1-12**

After completing all prior tasks, test whether child session replies land in the correct thread. If they do, this task is already done via the actor routing. If not, investigate the routing gap.

- [ ] **Step 2: Fix if needed and commit**

---

### Task 14: Manual Verification (Final)

- [ ] **Step 1: Start the server**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m channel_server.app
```

- [ ] **Step 2: Verify in Feishu**

Test the following manually:
1. Send `/spawn test-session` in Feishu → should create thread anchor + tool card in thread (not main chat)
2. Send `/sessions` → should list sessions without going through LLM
3. Send `/kill test-session` → should kill the session
4. Root session should have a tool card in main chat
5. Normal messages should still flow through to CC session as before

- [ ] **Step 3: Commit any fixes**

```bash
git add -A && git commit -m "fix: manual verification adjustments"
```
