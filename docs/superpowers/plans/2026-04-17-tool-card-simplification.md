# Tool Card Simplification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace persistent interactive tool cards with direct text messages per tool notification, removing the tool_card actor entirely.

**Architecture:** tool_notify messages flow through CCSessionHandler → downstream feishu actors as plain text. The tool_card actor, ToolCardHandler, and all card creation/update logic are removed.

**Tech Stack:** Python 3.11+, asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-04-17-tool-card-simplification-design.md`

---

## File Map

| Action | File | Change |
|--------|------|--------|
| Modify | `channel_server/core/handlers/cc.py` | tool_notify → downstream as text; remove tool_card StopActor from on_stop |
| Modify | `channel_server/core/handlers/feishu.py` | Remove create_tool_card from on_spawn |
| Modify | `channel_server/core/handlers/session_mgr.py` | Remove tool_card SpawnActor from _handle_init |
| Modify | `channel_server/adapters/cc/adapter.py` | Rewrite _route_anonymous_tool_notify to find cc actor |
| Modify | `channel_server/adapters/feishu/adapter.py` | Remove tool_notify from transport handlers; remove dead code |
| Delete | `channel_server/core/handlers/tool_card.py` | Entire file |
| Modify | `channel_server/core/handlers/__init__.py` | Remove ToolCardHandler export |
| Modify | `channel_server/core/handler.py` | Remove ToolCardHandler from registry |
| Modify | `tests/channel_server/core/test_handler.py` | Update tests |
| Modify | `tests/channel_server/core/handlers/test_session_mgr.py` | Update init_session test |

---

### Task 1: Update CCSessionHandler — tool_notify to downstream as text

**Files:**
- Modify: `channel_server/core/handlers/cc.py:58-62, 88-97`
- Test: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Update test for tool_notify routing**

In `tests/channel_server/core/test_handler.py`, replace `test_cc_session_tool_notify` with:

```python
def test_cc_session_tool_notify_sends_to_downstream():
    actor = make_actor(
        address="cc:user.dev",
        tag="dev",
        downstream=["feishu:test_app:oc_test:thread:dev"],
    )
    msg = make_msg(
        sender="cc:user.dev",
        payload={"action": "tool_notify", "text": "⚙️ Running: git status"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "feishu:test_app:oc_test:thread:dev"
    # action key stripped — only text remains
    assert actions[0].message.payload == {"text": "⚙️ Running: git status"}
    assert "tool_notify" not in actions[0].message.payload
```

- [ ] **Step 2: Update test for on_stop (no more tool_card StopActor)**

In `tests/channel_server/core/test_handler.py`, update `test_cc_session_on_stop_stops_children`:

```python
def test_cc_session_on_stop_stops_children():
    from channel_server.core.actor import StopActor

    actor = make_actor(
        address="cc:user.dev",
        tag="dev",
        handler="cc_session",
        downstream=["feishu:test_app:oc_test:om_anchor"],
    )

    actions = CCSessionHandler().on_stop(actor)
    stop_addrs = {a.address for a in actions if isinstance(a, StopActor)}
    # Only downstream feishu actors — no tool_card
    assert "feishu:test_app:oc_test:om_anchor" in stop_addrs
    assert not any("tool_card:" in addr for addr in stop_addrs)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/channel_server/core/test_handler.py::test_cc_session_tool_notify_sends_to_downstream tests/channel_server/core/test_handler.py::test_cc_session_on_stop_stops_children -v`
Expected: FAIL

- [ ] **Step 4: Update CCSessionHandler**

In `channel_server/core/handlers/cc.py`, change the tool_notify block (lines 58-62):

```python
        if action == "tool_notify":
            # Send as plain text to downstream feishu actors (strip action key)
            text_msg = Message(sender=msg.sender, payload={"text": msg.payload.get("text", "")})
            return [Send(to=addr, message=text_msg) for addr in actor.downstream]
```

In `on_stop` (lines 88-97), remove the tool_card StopActor:

```python
    def on_stop(self, actor: Actor) -> list[Action]:
        """Stop child actors (feishu_thread) when CC session ends."""
        actions: list[Action] = []
        # Stop downstream feishu thread actors
        for addr in actor.downstream:
            actions.append(StopActor(address=addr))
        return actions
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/channel_server/core/test_handler.py -v`
Expected: ALL PASS (ToolCardHandler tests will still pass since file exists — removed in Task 3)

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handlers/cc.py tests/channel_server/core/test_handler.py
git commit -m "feat: tool_notify routes to downstream feishu as plain text"
```

---

### Task 2: Remove tool card from on_spawn and init_session

**Files:**
- Modify: `channel_server/core/handlers/feishu.py:78-98`
- Modify: `channel_server/core/handlers/session_mgr.py:213-234`
- Test: `tests/channel_server/core/handlers/test_session_mgr.py`
- Test: `tests/channel_server/core/handlers/test_feishu.py`

- [ ] **Step 1: Update feishu on_spawn test**

In `tests/channel_server/core/handlers/test_feishu.py`, update `test_feishu_inbound_on_spawn_child_creates_anchor_and_card`:

```python
def test_feishu_inbound_on_spawn_child_creates_anchor():
    """on_spawn for child mode creates thread anchor only (no tool card)."""
    from channel_server.core.actor import Transport, TransportSend

    actor = make_actor(
        address="feishu:test_app:oc_test:thread:dev",
        tag="dev",
        handler="feishu_inbound",
        metadata={"chat_id": "oc_test", "tag": "dev", "mode": "child"},
    )
    actor.transport = Transport(type="feishu_thread", config={"chat_id": "oc_test"})

    actions = FeishuInboundHandler().on_spawn(actor)
    assert len(actions) == 1  # only thread anchor, no tool card

    anchor_action = actions[0]
    assert isinstance(anchor_action, TransportSend)
    assert anchor_action.payload["action"] == "create_thread_anchor"
```

- [ ] **Step 2: Update session_mgr init test**

In `tests/channel_server/core/handlers/test_session_mgr.py`, update `test_init_session_root`:

```python
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

    # Root init no longer spawns tool_card — returns empty
    assert actions == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/channel_server/core/handlers/test_feishu.py::test_feishu_inbound_on_spawn_child_creates_anchor tests/channel_server/core/handlers/test_session_mgr.py::test_init_session_root -v`
Expected: FAIL

- [ ] **Step 4: Update FeishuInboundHandler.on_spawn**

In `channel_server/core/handlers/feishu.py`, change on_spawn to only create thread anchor:

```python
    def on_spawn(self, actor: Actor) -> list[Action]:
        """Create thread anchor for child sessions."""
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
        ]
```

- [ ] **Step 5: Update SessionMgrHandler._handle_init**

In `channel_server/core/handlers/session_mgr.py`, simplify _handle_init:

```python
    def _handle_init(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None") -> list[Action]:
        """Handle init_session from _handle_register for root sessions."""
        # Root sessions no longer need tool_card — tool_notify goes as text to main chat
        return []
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/channel_server/core/handlers/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add channel_server/core/handlers/feishu.py channel_server/core/handlers/session_mgr.py tests/channel_server/core/handlers/
git commit -m "feat: remove tool card creation from on_spawn and init_session"
```

---

### Task 3: Delete ToolCardHandler and remove from registry

**Files:**
- Delete: `channel_server/core/handlers/tool_card.py`
- Modify: `channel_server/core/handlers/__init__.py`
- Modify: `channel_server/core/handler.py`
- Modify: `tests/channel_server/core/test_handler.py`

- [ ] **Step 1: Remove ToolCardHandler tests from test_handler.py**

Delete these test functions from `tests/channel_server/core/test_handler.py`:
- `test_tool_card_accumulates_history`
- `test_tool_card_starts_empty`
- `test_tool_card_includes_card_msg_id`
- `test_tool_card_empty_card_msg_id`
- `test_tool_card_on_stop_updates_card`
- `test_tool_card_on_stop_no_transport_noop`

Also remove `ToolCardHandler` from the imports at the top of the file.

- [ ] **Step 2: Remove from __init__.py**

In `channel_server/core/handlers/__init__.py`, remove the ToolCardHandler import and __all__ entry:

```python
"""Handler implementations for the actor model."""
from channel_server.core.handlers.feishu import FeishuInboundHandler
from channel_server.core.handlers.cc import CCSessionHandler
from channel_server.core.handlers.forward import ForwardAllHandler
from channel_server.core.handlers.admin import AdminHandler
from channel_server.core.handlers.session_mgr import SessionMgrHandler

__all__ = [
    "FeishuInboundHandler",
    "CCSessionHandler",
    "ForwardAllHandler",
    "AdminHandler",
    "SessionMgrHandler",
]
```

- [ ] **Step 3: Remove from handler.py registry**

In `channel_server/core/handler.py`, remove `ToolCardHandler` from the import and from `HANDLER_REGISTRY`:

```python
from channel_server.core.handlers import (
    AdminHandler,
    CCSessionHandler,
    FeishuInboundHandler,
    ForwardAllHandler,
    SessionMgrHandler,
)

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "admin": AdminHandler(),
    "session_mgr": SessionMgrHandler(),
}
```

- [ ] **Step 4: Delete tool_card.py**

```bash
rm channel_server/core/handlers/tool_card.py
```

- [ ] **Step 5: Fix remaining test references to tool_card**

In `tests/channel_server/core/test_handler.py`, also remove `test_get_handler_returns_correct_types` assertion for tool_card (or update the test to not check tool_card).

In `tests/channel_server/core/test_runtime.py`, find any test using `"tool_card"` handler and change to a different handler (e.g. `"forward_all"`).

In `tests/channel_server/adapters/test_cc_adapter.py`, update `test_route_anonymous_tool_notify` and `test_route_anonymous_tool_notify_no_match` — these spawn `tool_card:*` actors. Rewrite to match the new routing logic (finding cc actors by downstream feishu chat_id match).

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: delete ToolCardHandler and remove from registry"
```

---

### Task 4: Remove tool_card from _handle_spawn + Rewrite _route_anonymous_tool_notify

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`

- [ ] **Step 0: Remove tool_card creation from _handle_spawn**

In `channel_server/adapters/cc/adapter.py`, the `_handle_spawn` method (still live for CC MCP spawn calls) creates tool_card actors at lines ~420-433. Remove the tool card creation block (create_tool_card call + tool_card actor spawn) and remove `tool_card:{user}.{session_name}` from the rollback error path. Keep the rest of _handle_spawn intact.

- [ ] **Step 1: Rewrite _route_anonymous_tool_notify**

In `channel_server/adapters/cc/adapter.py`, replace `_route_anonymous_tool_notify` (lines 275-299):

```python
    def _route_anonymous_tool_notify(self, msg: dict) -> None:
        """Route a tool_notify from an anonymous WS connection (e.g. hook).

        Finds the cc:* actor serving this chat_id by checking downstream
        feishu actors' transport config, then delivers the message to that
        cc actor. CCSessionHandler routes it to feishu as plain text.
        """
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        if not chat_id or not text:
            log.warning("_route_anonymous_tool_notify: missing chat_id or text")
            return

        from channel_server.core.actor import Message as ActorMessage

        # Find cc actor that has this chat_id in its downstream feishu actor
        for addr, actor in self.runtime.actors.items():
            if not addr.startswith("cc:") or actor.state == "ended":
                continue
            for ds_addr in actor.downstream:
                ds = self.runtime.lookup(ds_addr)
                if (ds and ds.transport
                        and ds.transport.type in ("feishu_chat", "feishu_thread")
                        and ds.transport.config.get("chat_id") == chat_id):
                    self.runtime.send(addr, ActorMessage(
                        sender="hook:tool_notify",
                        payload={"action": "tool_notify", "text": text},
                    ))
                    return

        log.debug("_route_anonymous_tool_notify: no cc actor for chat_id=%s", chat_id)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add channel_server/adapters/cc/adapter.py
git commit -m "refactor: _route_anonymous_tool_notify finds cc actor instead of tool_card"
```

---

### Task 5: Remove dead code from feishu adapter

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`

- [ ] **Step 1: Remove tool_notify from transport handlers**

In `_handle_chat_transport` (around line 416), remove:
```python
        elif action == "tool_notify":
            await self._update_card(payload.get("card_msg_id", ""), payload.get("text", ""))
```

In `_handle_thread_transport`, remove the `tool_notify` case AND the `create_tool_card` action case (added in the session-mgr refactoring but now dead code since on_spawn no longer emits create_tool_card).

- [ ] **Step 2: Remove dead methods**

Delete these methods from the feishu adapter:
- `_update_card` (line ~615)
- `create_tool_card` (line ~698)
- `_build_tool_card` (line ~890)

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add channel_server/adapters/feishu/adapter.py
git commit -m "refactor: remove tool_notify handler and dead tool card methods from feishu adapter"
```

---

### Task 6: Manual Verification

- [ ] **Step 1: Restart channel server**

```bash
kill $(jq -r .pid .channel-server.pid) 2>/dev/null; sleep 2
nohup uv run python -m channel_server.app > /tmp/channel-server.log 2>&1 &
sleep 3
cat .channel-server.pid
```

- [ ] **Step 2: Test in Feishu**

1. Send a message that triggers tool use in any CC session
2. Verify tool_notify text messages appear in the correct thread/chat
3. Verify no interactive cards are created
4. Verify /spawn creates a session without tool card errors
