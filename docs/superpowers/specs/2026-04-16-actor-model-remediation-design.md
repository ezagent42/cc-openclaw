# Actor Model Full Remediation — Design Spec

**Date**: 2026-04-16
**Status**: Draft
**Scope**: channel_server (adapters + core)

## Problem

The actor model refactor left significant legacy code that bypasses actor isolation:
shared mutable state on adapters, side effects outside handlers, blocking API calls
in async context. These cause race conditions (ACK emoji disappearing), message loss
(orphan thread actors), and fragile concurrency (unsynchronized dict access from
multiple threads).

## Goals

1. Adapters are stateless transport pipes — receive/send only, zero business logic
2. All business logic lives in handlers — pure functions: (actor, msg) -> list[Action]
3. All actor state lives in actor.metadata — no shared mutable dicts on adapters
4. All Feishu API calls are async — no threading.Thread, no blocking in event loop
5. Tests enforce these boundaries to prevent regression

## Non-Goals

- Changing the actor/runtime/mailbox architecture itself
- Changing the WS protocol between channel.py and adapter
- Adding new features (reaction forwarding, thread management, etc.)

---

## Architecture

### File Structure After Refactor

```
channel_server/
├── core/
│   ├── actor.py              # Actor, Message, Action types (unchanged)
│   ├── handler.py            # Handler protocol + registry (thin)
│   ├── runtime.py            # ActorRuntime (minor changes for TransportSend return values)
│   └── handlers/
│       ├── __init__.py
│       ├── feishu.py          # FeishuInboundHandler (ACK, route, echo, dedup, on_stop)
│       ├── cc.py              # CCSessionHandler (forward, on_stop cascade)
│       ├── tool_card.py       # ToolCardHandler
│       ├── admin.py           # AdminHandler
│       └── forward.py         # ForwardAllHandler
├── adapters/
│   ├── feishu/
│   │   ├── adapter.py         # FeishuAdapter — stateless transport (~200 lines)
│   │   └── parsers.py         # Message parsers (unchanged)
│   └── cc/
│       ├── adapter.py         # CCAdapter — WS management only
│       └── channel.py         # MCP channel plugin (unchanged)
```

### Responsibility Boundaries

```
┌─────────────────────────────────────────────────────────┐
│ Adapter (transport layer)                               │
│ - Parse inbound events → runtime.send()                 │
│ - Execute outbound TransportSend → async SDK call       │
│ - NO state, NO business logic, NO filtering             │
├─────────────────────────────────────────────────────────┤
│ Handler (business logic layer)                          │
│ - handle(actor, msg) → list[Action]                     │
│ - on_stop(actor) → list[Action]                         │
│ - All state in actor.metadata                           │
│ - Pure functions, no I/O, no side effects               │
├─────────────────────────────────────────────────────────┤
│ Runtime (orchestration layer)                           │
│ - Actor lifecycle, mailbox, loop                        │
│ - Execute actions (Send, TransportSend, StopActor, etc) │
│ - Message-level dedup (by message_id)                   │
└─────────────────────────────────────────────────────────┘
```

---

## A. Shared Mutable State Migration

### _last_msg_id + _ack_reactions → actor.metadata

**Current**: `FeishuAdapter._last_msg_id: dict[str, str]` and `_ack_reactions: dict[str, str]`
written from WS thread, read/popped from asyncio transport handler.

**After**: Each feishu actor's metadata:
```python
actor.metadata = {
    "ack_msg_id": "om_xxx",           # message currently ACK'd
    "ack_reaction_id": "r_xxx",       # reaction ID to remove later
}
```

FeishuInboundHandler manages these in its serial loop:
- Inbound message → `UpdateActor(metadata={"ack_msg_id": msg_id})` + `TransportSend(action="ack_react")`
- Outbound reply → `TransportSend(action="remove_ack")` reading from actor.metadata
- No concurrent access — actor loop is serial

### _recent_sent → FeishuInboundHandler + actor.metadata

**Current**: `FeishuAdapter._recent_sent: set[str]` written from daemon threads,
read from WS callback.

**After**: When the feishu actor sends a message (outbound), the transport handler
returns the sent message_id. The runtime stores it in actor.metadata
(`sent_msg_ids: list[str]`, bounded ring buffer). FeishuInboundHandler checks
this on inbound and skips if matched.

All access is within the actor's serial loop — no concurrent reads/writes.

### _seen (dedup) → runtime.send()

**Current**: `FeishuAdapter._seen: set[str]` — dedup by message_id.

**After**: `runtime.send()` accepts an optional `message_id` parameter. The runtime
maintains a bounded dedup set. If a message_id was already delivered, it drops
the duplicate. This is infrastructure-level idempotency, not business logic.

```python
def send(self, to: str, message: Message, *, message_id: str = "") -> None:
    if message_id and message_id in self._dedup:
        return
    if message_id:
        self._dedup.add(message_id)
        # bounded cleanup
    ...
```

### _chat_id_map → FeishuInboundHandler + persistent actor metadata

**Current**: `FeishuAdapter._chat_id_map: dict[str, str]` with sync disk I/O.

**After**: FeishuInboundHandler detects DM messages (`chat_type == "p2p"`) and
emits `UpdateActor(metadata={"chat_id_map": {open_id: chat_id}})`. The persistence
layer (already exists) saves actor state periodically — no hot-path disk I/O.

---

## B. Side Effect Migration

### ACK Reaction — from adapter to FeishuInboundHandler

**Current flow (broken)**:
```
on_feishu_event() → threading.Thread(_send_reaction) → shared _last_msg_id
_handle_chat_transport() → _last_msg_id.pop() → threading.Thread(_remove_reaction)
```

**After flow (correct)**:
```
FeishuInboundHandler.handle():
  inbound msg from feishu_user:
    → UpdateActor(metadata={"ack_msg_id": msg_id})
    → TransportSend(action="ack_react", message_id=msg_id)
    → Send(to=downstream)  # forward to CC

  outbound msg from cc:
    → TransportSend(action="remove_ack",
                    message_id=actor.metadata["ack_msg_id"],
                    reaction_id=actor.metadata["ack_reaction_id"])
    → TransportSend(payload=msg.payload)  # send reply text
```

All within the actor's serial loop. The TransportSend for ack_react returns
`{"ack_reaction_id": "r_xxx"}` which the runtime merges into actor.metadata.

### Transport Handler Action Dispatch → stays, but simplified

The transport handler's if/elif dispatch (action → API call) is legitimate
transport-layer routing ("which API endpoint to call"). It stays, but:
- Remove all shared state access (_last_msg_id.pop, etc.)
- Remove all business logic
- Make all branches async
- Return metadata updates when needed (e.g., ack_reaction_id)

### Topology Wiring — from CCAdapter to runtime

**Current**: `CCAdapter._handle_register()` directly mutates `actor.downstream`.

**After**: Runtime gets a `wire(from_addr, to_addr)` method. CCAdapter calls
`self.runtime.wire("system:admin", address)` instead of directly appending
to downstream lists.

---

## C. Async Migration

### All Feishu API Calls → async SDK

Every method in FeishuAdapter that calls `self.feishu_client.im.v1.*` switches
from sync to async:

| Method | Current | After |
|--------|---------|-------|
| _send_message | .reply() / .create() in Thread | .areply() / .acreate() |
| _send_reaction | BaseRequest + .request() in Thread | BaseRequest + .arequest() |
| _remove_reaction | BaseRequest + .request() in Thread | BaseRequest + .arequest() |
| _send_file | .create() x2 in Thread | .acreate() x2 |
| _update_card | .patch() in Thread | .apatch() |
| _update_anchor_card | .patch() in Thread | .apatch() |
| create_thread_anchor | .create() / .reply() blocking | .acreate() / .areply() |
| create_tool_card | .create() blocking | .acreate() |
| pin_message | .create() blocking | .acreate() |
| unpin_message | .delete() blocking | .adelete() |
| _download_resource | .get() in Thread | .aget() |
| send_startup_notification | .create() in Thread | .acreate() |

All `threading.Thread(target=..., daemon=True).start()` calls are removed.
Transport handlers become `async def`.

### Runtime TransportSend — handle async + return values

`_execute_transport_send` already handles awaitables via `asyncio.ensure_future`.
Extend to capture return values and merge into actor.metadata:

```python
async def _execute_transport_send(self, actor, action):
    callback = self._transport_handlers.get(actor.transport.type)
    result = await callback(actor, action.payload)  # always async now
    if isinstance(result, dict):
        actor.metadata.update(result)  # e.g., ack_reaction_id
```

### Blocking calls in _handle_spawn

`create_thread_anchor()`, `pin_message()`, `create_tool_card()` are called
from async `_handle_spawn()`. After async migration, these become awaitable:

```python
anchor_msg_id = await self.feishu_adapter.create_thread_anchor(chat_id, tag)
if anchor_msg_id:
    await self.feishu_adapter.pin_message(anchor_msg_id)
```

---

## D. Test Strategy

### 1. Adapter Statelessness Tests

```python
def test_feishu_adapter_has_no_business_state():
    """Adapter must not hold _last_msg_id, _ack_reactions, _seen, _recent_sent."""
    adapter = FeishuAdapter(runtime, client)
    for attr in ["_last_msg_id", "_ack_reactions", "_seen", "_recent_sent"]:
        assert not hasattr(adapter, attr)

def test_on_feishu_event_only_sends_to_runtime():
    """on_feishu_event must call runtime.send() and nothing else."""
    # Mock runtime, verify send() called, no API calls made
```

### 2. Handler Pure Function Tests

```python
def test_feishu_handler_emits_ack_on_inbound():
    actor = make_actor(metadata={})
    msg = make_msg(sender="feishu_user:u1", payload={"message_id": "om_1", ...})
    actions = FeishuInboundHandler().handle(actor, msg)
    # Must contain: UpdateActor (ack_msg_id), TransportSend (ack_react), Send (forward)
    assert any(isinstance(a, UpdateActor) for a in actions)
    assert any(isinstance(a, TransportSend) and a.payload["action"] == "ack_react" for a in actions)
    assert any(isinstance(a, Send) for a in actions)

def test_feishu_handler_removes_ack_on_outbound():
    actor = make_actor(metadata={"ack_msg_id": "om_1", "ack_reaction_id": "r_1"})
    msg = make_msg(sender="cc:user.root", payload={"text": "reply"})
    actions = FeishuInboundHandler().handle(actor, msg)
    assert any(isinstance(a, TransportSend) and a.payload["action"] == "remove_ack" for a in actions)
```

### 3. Serial Correctness Tests

```python
def test_rapid_messages_no_ack_interference():
    """Two messages A, B in sequence. A's reply must not remove B's ACK."""
    handler = FeishuInboundHandler()
    actor = make_actor(metadata={})

    # Message A
    actions_a = handler.handle(actor, msg_a)
    apply_updates(actor, actions_a)  # ack_msg_id = A

    # Message B (before A is replied to)
    actions_b = handler.handle(actor, msg_b)
    apply_updates(actor, actions_b)  # ack_msg_id = B

    # Reply to A — should remove B's ack (B is the latest)
    # This is correct serial behavior: latest ack is removed on any reply
    actions_reply = handler.handle(actor, reply_to_a)
    remove_action = find_action(actions_reply, "remove_ack")
    assert remove_action.payload["message_id"] == "om_B"
```

### 4. Architecture Compliance Tests (Regression Prevention)

```python
def test_adapter_files_have_no_metadata_writes():
    """Scan adapter source — must not contain 'actor.metadata' assignments."""
    import ast
    for path in glob("channel_server/adapters/**/*.py"):
        source = Path(path).read_text()
        assert "actor.metadata" not in source or "# compliance-exempt" in source

def test_handler_files_have_no_api_calls():
    """Scan handler source — must not import lark_oapi or call feishu_client."""
    for path in glob("channel_server/core/handlers/*.py"):
        source = Path(path).read_text()
        assert "lark_oapi" not in source
        assert "feishu_client" not in source

def test_transport_handlers_are_async():
    """All transport handler methods must be async."""
    import inspect
    adapter = FeishuAdapter(runtime, client)
    assert inspect.iscoroutinefunction(adapter._handle_chat_transport)
    assert inspect.iscoroutinefunction(adapter._handle_thread_transport)
```

---

## Migration Order

1. **Create handlers/ directory** — extract existing handlers from handler.py into separate files
2. **Migrate FeishuInboundHandler** — add ACK logic, echo prevention, dedup (biggest change)
3. **Migrate shared state** — remove _last_msg_id, _ack_reactions from adapter; add dedup to runtime
4. **Async migration** — convert all feishu API methods to async, remove threading.Thread
5. **Simplify transport handlers** — remove side effects, make async
6. **Runtime changes** — TransportSend return values, wire() method, dedup in send()
7. **Simplify CCAdapter** — remove topology wiring from _handle_register
8. **Tests** — all 4 categories
9. **Cleanup** — remove dead code, unused imports

Each step should be independently testable and committable.
