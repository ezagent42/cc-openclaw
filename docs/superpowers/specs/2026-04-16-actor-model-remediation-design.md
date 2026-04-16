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

- Changing the WS protocol between channel.py and adapter
- Adding new features (reaction forwarding, thread management, etc.)

## Scope Clarifications

- **Runtime changes are in scope**: `_execute` becomes async, TransportSend gains
  return values, `wire()` method added, dedup in `send()`. These are necessary to
  support the handler migration but do not change the actor/mailbox model itself.
- **Feishu WS listener thread stays**: `lark_oapi.ws.Client.start()` is a blocking
  call that runs its own event loop. This structural thread is not a workaround —
  it is excluded from the "remove all threading.Thread" scope.
- **Adapter "stateless" exemptions**: Transport-level connection state
  (`CCAdapter._ws_to_address`, `_address_to_ws`) and static config lookups
  (`FeishuAdapter._user_names` from roles.yaml) remain on adapters. These are
  connection/config plumbing, not business state.
- **File downloads**: `download_file`/`download_image_by_key` are called from
  parsers.py in the Feishu WS thread (before actor pipeline). These become async
  and are called via `asyncio.run_coroutine_threadsafe()` from the WS callback,
  or deferred to the handler by passing file_key in the payload and downloading
  in the transport handler. Deferred approach preferred — adapter just passes
  metadata, actor decides if/when to download.

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
│ - Deterministic given (actor, msg), no I/O               │
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
- Inbound message → `UpdateActor(changes={"metadata": {"ack_msg_id": msg_id}})` + `TransportSend(action="ack_react")`
- Outbound reply → `TransportSend(action="remove_ack")` reading from actor.metadata
- No concurrent access — actor loop is serial

### _recent_sent → FeishuInboundHandler + actor.metadata

**Current**: `FeishuAdapter._recent_sent: set[str]` written from daemon threads,
read from WS callback.

**After**: When the feishu actor sends a message (outbound), the async transport
handler returns `{"_sent_msg_id": "om_xxx"}`. The runtime merges this into
actor.metadata. FeishuInboundHandler manages the echo set:
- On outbound TransportSend result, handler receives the sent_msg_id via metadata
- Handler maintains `actor.metadata["sent_msg_ids"]` as a bounded list (max 100)
  via `UpdateActor(changes={"metadata": {"sent_msg_ids": updated_list}})`
- On inbound, handler checks if message_id is in `actor.metadata["sent_msg_ids"]`
  and skips if matched

The handler manages the list directly — no ring buffer in runtime, no custom
merge logic. All access is within the actor's serial loop.

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

### _chat_id_map → FeishuInboundHandler + actor.metadata + file persistence

**Current**: `FeishuAdapter._chat_id_map: dict[str, str]` with sync disk I/O
in hot path.

**After**: FeishuInboundHandler detects DM messages (`chat_type == "p2p"`) and
emits `UpdateActor(changes={"metadata": {"chat_id_map": {open_id: chat_id}}})`.

For persistence: the existing `persistence.py` saves/loads actors to
`.workspace/actors.json`. Actor metadata (including chat_id_map) is already
persisted via `Actor.to_dict()`. The save happens on shutdown and can be
extended to save periodically (e.g., every 60s). No new persistence layer
needed — just ensure the existing one is called on a timer, not in hot path.

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
    → UpdateActor(changes={"metadata": {"ack_msg_id": msg_id}})
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
`CCAdapter._route_to_actor()` walks the actor graph to inject `parent_feishu`
into `send_summary` payloads.

**After**: Runtime gets a `wire(from_addr, to_addr)` method:
```python
def wire(self, from_addr: str, to_addr: str) -> None:
    """Append to_addr to from_addr's downstream if not already present.
    No-op if from_addr doesn't exist. Does not create actors."""
    actor = self.actors.get(from_addr)
    if actor and to_addr not in actor.downstream:
        actor.downstream.append(to_addr)
```

CCAdapter calls `self.runtime.wire("system:admin", address)`.

The `send_summary` parent_feishu injection moves from `_route_to_actor()` to
`CCSessionHandler.handle()` — the handler walks `actor.parent` and its
downstream to find the feishu address, which is actor-level business logic.

### Reaction Events — from adapter to handler

**Current**: `on_feishu_reaction()` broadcasts to all feishu actors from
the adapter.

**After**: `on_feishu_reaction()` in the adapter just does
`runtime.send(main_chat_addr, msg)` — the handler decides what to do with it.

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

### Runtime TransportSend — async execution + return values

This is a significant runtime change: `_execute` and `_execute_transport_send`
become async. This is necessary because transport handlers are now async
(calling async Feishu SDK). The change propagates through the action execution
chain:

```python
# _actor_loop (already async) calls:
async def _execute(self, actor, action):
    if isinstance(action, TransportSend):
        await self._execute_transport_send(actor, action)
    elif isinstance(action, StopActor):
        self.stop(action.address)  # stop stays sync (no I/O)
    # ... other actions stay sync

async def _execute_transport_send(self, actor, action):
    callback = self._transport_handlers.get(actor.transport.type)
    result = await callback(actor, action.payload)  # always async now
    if isinstance(result, dict):
        actor.metadata.update(result)  # e.g., ack_reaction_id
```

The `on_stop` lifecycle also needs to await async actions — `_execute` calls
in the `stop()` method become async too, requiring `stop()` to be async or
to use `asyncio.ensure_future` for transport sends during cleanup.

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

Steps ordered to resolve dependencies (runtime support before handler migration):

1. **Create handlers/ directory** — extract existing handlers from handler.py into separate files. No logic changes, pure move. All existing tests must still pass.
2. **Runtime changes** — `_execute` becomes async, TransportSend captures return values and merges into actor.metadata, add `wire()` method, add dedup in `send()`. This provides the infrastructure that handler migration depends on.
3. **Async migration** — convert all feishu API methods to async SDK, transport handlers become `async def`, remove all `threading.Thread`. Transport handlers return metadata dicts.
4. **Migrate FeishuInboundHandler** — add ACK logic (emit TransportSend for ack_react/remove_ack), echo prevention (check actor.metadata.sent_msg_ids), reaction event handling. Remove corresponding code from adapter.
5. **Migrate shared state** — remove `_last_msg_id`, `_ack_reactions`, `_seen`, `_recent_sent`, `_chat_id_map` from adapter. Adapter becomes stateless transport pipe.
6. **Simplify CCAdapter** — topology wiring via `runtime.wire()`, `send_summary` routing moves to CCSessionHandler, simplify `_handle_register`.
7. **File download refactor** — adapter passes file_key in payload, transport handler does async download on demand.
8. **Tests** — all 4 categories (statelessness, pure handler, serial correctness, compliance)
9. **Cleanup** — remove dead code, unused imports, verify 0 threading.Thread in adapters

Each step is independently testable and committable.
