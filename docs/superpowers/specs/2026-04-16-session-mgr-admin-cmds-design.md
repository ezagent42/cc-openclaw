# Session Manager & Admin Command Refactoring

**Date:** 2026-04-16
**Status:** Draft
**Depends on:** [Actor Model Remediation](2026-04-16-actor-model-remediation-design.md)

## Problem

Two issues remain after the actor model remediation:

1. **`_handle_register` and `_handle_spawn` have divergent initialization.**
   `_handle_spawn` creates a complete actor with thread anchor, tool card, and tmux window.
   `_handle_register` only attaches a WebSocket transport and auto-spawns a minimal actor — no tool card, no unified init path.
   Result: root session has no tool card; child session resume logic is incomplete.

2. **`/spawn`, `/kill`, `/sessions` have two divergent entry points.**
   - **Feishu text path:** User sends `/spawn voice-widget` → admin actor → cc:user.root → Claude interprets → calls MCP tool → channel server executes. Wasteful: deterministic commands flow through LLM.
   - **CC WebSocket path:** CC process sends `{"action": "spawn_session", ...}` → adapter._handle_spawn() executes directly. Short-circuits LLM, but business logic lives in the adapter.
   
   Both paths must be unified: session-mgr becomes the single entry point for session lifecycle, regardless of origin (Feishu text or CC MCP tool).

3. **Tool cards are created in the wrong place.**
   All child session tool cards appear in the main chat window instead of inside their respective threads.

4. **MCP reply/send_file bypass actor routing.**
   `reply` and `send_file` MCP tools call the feishu API directly with `chat_id`, bypassing the actor's downstream routing. This means child session replies and file sends always appear in the main chat instead of their thread.

## Goals

1. All sessions (including root) get a tool card on initialization.
2. `/spawn`, `/kill`, `/sessions` are intercepted by AdminHandler and never reach the CC session.
3. Child session tool cards are created inside their thread, not in the main chat.
4. Initialization logic is unified: root and child sessions share the same path through `session-mgr`.
5. MCP reply/send_file route through actor downstream so messages land in the correct thread.

## Architecture

### New Actor Topology

```
Entry point 1: Feishu text
  User message → feishu adapter → feishu:{chat_id} → system:admin
     ├── normal messages → cc:user.root → feishu:{chat_id}
     └── /spawn /kill /sessions → system:session-mgr

Entry point 2: CC MCP tool
  CC process → WebSocket → CC adapter
     └── spawn_session/kill_session/list_sessions → system:session-mgr

                                    system:session-mgr
                                        ↓
                                SessionMgrHandler (orchestration)
                                  ├── SpawnActor (feishu thread)
                                  ├── SpawnActor (cc session)
                                  └── reply → feishu:{chat_id}
```

Both entry points converge on `system:session-mgr`. The CC adapter no longer handles session lifecycle business logic — it forwards session commands to session-mgr as messages.

### New Components

**`system:session-mgr` actor** — created at server startup. Lightweight orchestrator: receives session commands, decides which actors to create/destroy, returns Action lists. No transport of its own.

**`SessionMgrHandler`** (`core/handlers/session_mgr.py`) — pure function handler. Handles three command types (spawn, kill, sessions) plus init_session for register unification.

### Modified Components

**`AdminHandler`** (`core/handlers/admin.py`) — adds interception: `/spawn`, `/kill`, `/sessions` → `Send(to="system:session-mgr", message=msg)`. All other messages forwarded downstream as before.

**Feishu adapter** — injects `chat_id`, `user_id`, `user` into message payload on every forwarded message. Eliminates runtime.lookup dependency from handlers.

**CC adapter `_handle_register`** (`adapters/cc/adapter.py`) — stripped to pure transport duties: WebSocket mapping + transport attach. New root actors trigger `init_session` message to session-mgr for tool card creation.

**CC adapter `_handle_spawn`/`_handle_kill`/`_handle_sessions`** (`adapters/cc/adapter.py`) — no longer execute business logic. Forward session commands to `system:session-mgr` as messages, receive results back, relay to CC process via WebSocket.

**`feishu_adapter.create_tool_card`** — accepts optional `root_id` parameter so cards can be created inside a thread.

### Unchanged Components

- `cc:*` actors, `feishu:{chat_id}:{anchor}` actors, `tool_card:*` actors — data structures unchanged.
- Runtime core, Actor dataclass — unchanged (lifecycle hooks added, see below).

## Detailed Design

### 1. SessionMgrHandler Command Processing

File: `core/handlers/session_mgr.py`

Handler signature: `handle(actor, msg) → list[Action]`

#### `/spawn <name> [--tag <tag>]`

```python
def _handle_spawn(self, actor, msg):
    user = msg.payload["user"]
    chat_id = msg.payload["chat_id"]
    session_name = parse_arg(text)
    tag = parse_flag(text, "--tag") or session_name

    # Validate: session already exists?
    existing = runtime.lookup(f"cc:{user}.{session_name}")
    if existing and existing.state == "active":
        return [reply_error(chat_id, "Session already active")]

    # Resume suspended session
    if existing and existing.state == "suspended":
        return [
            Send(to=f"cc:{user}.{session_name}",
                 message=Message(type="resume", payload={"tag": tag, "chat_id": chat_id})),
            reply_ok(chat_id, f"Session '{session_name}' resumed"),
        ]

    # New session: spawn feishu thread actor + cc actor
    thread_addr = f"feishu:{chat_id}:thread:{session_name}"
    cc_addr = f"cc:{user}.{session_name}"

    return [
        SpawnActor(thread_addr, handler="feishu_thread",
                   metadata={"chat_id": chat_id, "tag": tag, "mode": "child"}),
        SpawnActor(cc_addr, handler="cc_session", state="suspended",
                   parent=f"cc:{user}.root",
                   downstream=[thread_addr],
                   metadata={"chat_id": chat_id, "tag": tag}),
        # Wire thread → cc. Use existing runtime.wire() method (runtime.py:148).
        # Since session-mgr handler receives runtime as 3rd arg (see "Handler
        # Dependency on Runtime" section), it can call runtime.wire() directly
        # for topology setup, keeping this as a side effect rather than an Action.
        # Alternative: add a WireActor action type to the Action union if we want
        # to keep handlers fully pure. For now, pragmatic: call runtime.wire().
        # runtime.wire(thread_addr, cc_addr)  — called after actions are returned
        reply_ok(chat_id, f"Session '{session_name}' spawned"),
    ]
```

#### `/kill <name>`

```python
def _handle_kill(self, actor, msg):
    user = msg.payload["user"]
    chat_id = msg.payload["chat_id"]
    session_name = parse_arg(text)
    cc_addr = f"cc:{user}.{session_name}"

    existing = runtime.lookup(cc_addr)
    if not existing or existing.state == "ended":
        return [reply_error(chat_id, "Session not found")]

    thread_addr = find_thread_addr(existing)

    return [
        StopActor(cc_addr),       # on_stop: kills tmux
        StopActor(thread_addr),   # on_stop: updates anchor card
        reply_ok(chat_id, f"Session '{session_name}' killed"),
    ]
```

#### `/sessions`

```python
def _handle_sessions(self, actor, msg):
    user = msg.payload["user"]
    chat_id = msg.payload["chat_id"]
    sessions = [a for a in runtime.actors.values()
                if a.address.startswith(f"cc:{user}.")]

    lines = [f"{'🟢' if s.state == 'active' else '🟡'} {s.tag} ({s.state})"
             for s in sessions]

    return [reply_ok(chat_id, "\n".join(lines) or "No sessions")]
```

#### `init_session` (from register)

```python
def _handle_init(self, actor, msg):
    chat_id = msg.payload["chat_id"]
    user = msg.payload["user"]
    mode = msg.payload["mode"]  # "root" or "child"

    if mode == "root":
        return [
            SpawnActor(f"tool_card:{user}.root", handler="tool_card",
                       metadata={"chat_id": chat_id, "mode": "root"}),
        ]
    # child sessions go through _handle_spawn
```

#### Reply helpers

`reply_ok` and `reply_error` return `Send(to=f"feishu:{chat_id}", message=...)` — session-mgr replies directly to the feishu chat actor using chat_id from the payload.

### 2. on_spawn / on_stop Lifecycle Hooks

Runtime extension — `spawn()` calls a new `on_spawn` hook. `stop()` already calls `on_stop` (defined in the Handler protocol in `handler.py`). The `on_spawn` hook must be added to the Handler protocol.

```python
async def spawn(self, address, handler, **kwargs):
    actor = Actor(address=address, handler=handler, **kwargs)
    self.actors[address] = actor

    handler_fn = self.handlers.get(handler)
    if hasattr(handler_fn, "on_spawn"):
        actions = handler_fn.on_spawn(actor)
        for action in actions:
            await self._execute(actor, action)

# stop() already exists — on_stop is already called. No changes needed.
```

Actions are executed sequentially — later actions can depend on metadata written by earlier ones.

**Note:** The Handler protocol in `handler.py` currently defines `on_stop` but not `on_spawn`. Add `on_spawn` as an optional method to the protocol.

### 3. feishu_thread Handler Hooks

```python
class FeishuThreadHandler:
    def on_spawn(self, actor) -> list[Action]:
        chat_id = actor.metadata["chat_id"]
        tag = actor.metadata["tag"]
        mode = actor.metadata.get("mode", "child")

        if mode == "child":
            return [
                # Step 1: create thread anchor in main chat → writes anchor_msg_id to metadata
                TransportSend(payload={
                    "action": "create_thread_anchor",
                    "chat_id": chat_id, "tag": tag,
                }),
                # Step 2: create tool card inside thread (reads anchor_msg_id from metadata)
                TransportSend(payload={
                    "action": "create_tool_card",
                    "chat_id": chat_id, "tag": tag,
                    # transport handler reads actor.metadata["anchor_msg_id"] as root_id
                }),
            ]
        return []  # root mode handled separately by tool_card actor

    def on_stop(self, actor) -> list[Action]:
        return [
            TransportSend(payload={
                "action": "update_anchor_card",
                "template": "grey",
                "label": f"🔴 [{actor.tag}] ended",
            }),
        ]
```

Step 2 depends on step 1's result: `create_thread_anchor` returns `anchor_msg_id` which gets merged into `actor.metadata` by `_execute_transport_send`. Step 2's transport handler reads `actor.metadata["anchor_msg_id"]` to pass as `root_id`. This works because runtime executes actions sequentially.

**Error handling:** If step 1 fails (feishu API error), `anchor_msg_id` won't be in metadata. The transport handler for step 2 must check `actor.metadata.get("anchor_msg_id")` — if missing, skip tool card creation and log a warning. The session still functions (just without a tool card), so this is a degraded-but-operational state, not a fatal error.

**Transport handler return values:** The current `_handle_thread_transport` in the feishu adapter returns `None` for most actions. New transport actions (`create_thread_anchor`, `create_tool_card`) must return a dict with the relevant IDs (e.g., `{"anchor_msg_id": msg_id}`) so `_execute_transport_send` can merge them into `actor.metadata`. This is a required change to the feishu adapter's transport handlers.

### 4. cc_session Handler Hooks

```python
class CCSessionHandler:
    def on_spawn(self, actor) -> list[Action]:
        user, session = parse_address(actor.address)
        tag = actor.metadata.get("tag", session)
        chat_id = actor.metadata.get("chat_id", "")

        return [
            TransportSend(payload={
                "action": "spawn_tmux",
                "user": user, "session_name": session,
                "tag": tag, "chat_id": chat_id,
            }),
        ]

    def on_stop(self, actor) -> list[Action]:
        user, session = parse_address(actor.address)
        return [
            TransportSend(payload={
                "action": "kill_tmux",
                "user": user, "session_name": session,
            }),
        ]
```

### 5. _handle_register Simplification

```python
async def _handle_register(self, ws, msg):
    address = resolve_address(msg)

    # 1. Track ws ↔ address mapping (unchanged)
    self._ws_to_address[id(ws)] = address
    self._address_to_ws[address] = ws

    # 2. Lookup or auto-spawn actor
    actor = self.runtime.lookup(address)
    if actor is None or actor.state == "ended":
        self.runtime.spawn(address, handler="cc_session",
                           state="active",
                           transport=Transport(type="websocket",
                                              config={"instance_id": instance_id}))
    elif actor.state in ("suspended", "disconnected"):
        self.runtime.attach(address,
                            transport=Transport(type="websocket",
                                               config={"instance_id": instance_id}))
        actor.state = "active"

    # 3. If new root actor: notify session-mgr for init
    user, session = parse_address(address)
    if session == "root" and msg.get("chat_ids"):
        chat_id = msg["chat_ids"][0]
        self.runtime.send("system:session-mgr", Message(
            type="init_session",
            payload={"user": user, "session_name": "root",
                     "chat_id": chat_id, "mode": "root"},
        ))

    # 4. Topology wiring (admin → root → feishu) — unchanged
    # ...
```

Key change: register no longer does any business initialization. It attaches transport and delegates init to session-mgr.

### 6. Tool Card Location Fix

**Root cause:** `feishu_adapter.create_tool_card()` does not pass `root_id` to the Feishu API, so cards always appear in the main chat.

**Fix:** Add optional `root_id` parameter. When `root_id` is provided, use `ReplyMessageRequest` with `reply_in_thread=True` (same pattern as `create_thread_anchor` in adapter.py:627-635) instead of `CreateMessageRequest`:

```python
async def create_tool_card(self, chat_id, label, root_id=None):
    card_json = build_tool_card(label)
    if root_id:
        # Thread mode: reply to anchor message, creating card inside thread
        req = ReplyMessageRequest.builder()
            .message_id(root_id)
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .reply_in_thread(True)
                .build())
            .build()
    else:
        # Main chat mode: create message directly
        req = CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build())
            .build()
    # call feishu API...
```

**In the new architecture:**
- Child sessions: feishu_thread handler on_spawn creates tool card with `root_id=anchor_msg_id` → card inside thread
- Root session: tool_card actor on_spawn creates tool card without `root_id` → card in main chat

### UI Layout Result

```
Main Chat:
  ┌─ 🟢 [root] tool card              ← root tool card (main chat)
  ├─ 📌 [dev] thread anchor            ← child "dev" anchor
  │     └─ thread:
  │         ├─ 🟢 [dev] tool card      ← dev tool card (inside thread)
  │         ├─ user messages...
  │         └─ CC replies...
  ├─ 📌 [voice] thread anchor          ← child "voice" anchor
  │     └─ thread:
  │         ├─ 🟢 [voice] tool card
  │         └─ ...
  └─ user ↔ root direct conversation
```

## Handler Dependency on Runtime

`SessionMgrHandler` needs `runtime.lookup()` for validation (checking if a session exists) and querying (listing sessions). This means the handler is not a pure function in the strict sense.

Options:
- **Pragmatic:** pass `runtime` as a third argument to `handle()` for handlers that need it. Keep the `handle(actor, msg)` signature for handlers that don't.
- **Purist:** actor.metadata caches a session registry that session-mgr maintains. Adds complexity for little benefit.

Recommendation: pragmatic approach — `handle(actor, msg, runtime)`. The runtime reference is read-only for validation/query; mutations still go through returned Actions.

**Implementation:** Update `_actor_loop` in `runtime.py` (line 189) to always pass `runtime` as the third argument. Update the Handler Protocol in `handler.py` to include `runtime` as a parameter. All existing handlers gain the parameter but can ignore it. This is a one-time breaking change to the protocol, simpler than signature introspection.

## Race Conditions

**Duplicate `/spawn` for the same session name:** Between `runtime.lookup()` in the handler and `SpawnActor` execution, a second `/spawn` could pass the same check. Mitigation: `runtime.spawn()` raises `ValueError` on duplicate address. The action executor should catch this and send an error reply to the feishu actor rather than crashing the session-mgr loop. Add a try/except in `_execute` around `SpawnActor` handling.

## 7. MCP Reply/Send_file Thread Routing Fix

### Problem

MCP tools `reply` and `send_file` (in `adapters/cc/channel.py`) bypass actor routing. They call `_channel_client.send_reply(chat_id, text)` directly, which always sends to the main chat — even when the CC session is a child session that should reply in its thread.

The correct path is: CC session actor → Send to downstream feishu_thread actor → feishu_thread transport has `root_id` → message appears in thread.

### Current Flow (broken)

```
CC session receives reply action → channel.py _handle_reply()
  → channel_client.send_reply(chat_id, text)  ← direct feishu API call
  → message appears in main chat (no root_id)
```

### New Flow

```
CC session receives reply action → CCSessionHandler.handle()
  → TransportSend or Send(to=downstream feishu actor)
  → feishu actor's transport handler sends with root_id if present
  → message appears in correct location (main chat or thread)
```

### Changes

1. **`_handle_reply` and `_handle_send_file` in channel.py** — instead of calling feishu API directly, emit a message/action through the actor's downstream routing.
2. **CCSessionHandler** — catch-all action handler already routes to downstream (cc.py:64-65). Ensure `reply` and `send_file` actions go through this path instead of bypassing it.
3. **feishu adapter transport handler** — already handles `root_id` from transport config (adapter.py:399-403). No changes needed on the receiving end.

This ensures child sessions reply in their thread and root sessions reply in the main chat, with zero explicit `root_id` management in the MCP tools.

## Migration Strategy

This refactoring can be done incrementally:

1. **Add lifecycle hooks to runtime** — add `on_spawn` to Handler protocol and `spawn()`. `on_stop` already exists.
2. **Add `chat_id` injection in feishu adapter** — backward compatible, extra field in payload.
3. **Create `SessionMgrHandler` and `system:session-mgr` actor** — new code, no existing behavior changed.
4. **Update `AdminHandler`** to intercept session commands → session-mgr. This is the switch-over point.
5. **Add on_spawn to feishu_thread handler** — tool card + thread anchor creation moves from adapter to handler.
6. **Add on_spawn to cc_session handler** — tmux spawn moves from adapter to handler.
7. **Simplify `_handle_register`** — remove business init, delegate to session-mgr.
8. **Fix tool card location** — add `root_id` to `create_tool_card`, pass in feishu_thread on_spawn.
9. **Fix MCP reply/send_file routing** — route through actor downstream instead of direct feishu API.
10. **Unify CC adapter session commands** — `_handle_spawn`/`_handle_kill`/`_handle_sessions` forward to session-mgr instead of executing business logic directly.
11. **Remove dead code** from CCAdapter (business logic moved to handlers and session-mgr).

## Testing

- **SessionMgrHandler**: unit test — input messages, assert returned Action lists. No feishu/tmux mocking needed.
- **AdminHandler**: unit test — verify session commands route to session-mgr, others route downstream.
- **Lifecycle hooks**: integration test — spawn actor, verify on_spawn actions executed.
- **Tool card location**: manual verification in Feishu — child card inside thread, root card in main chat.
- **Reply/send_file routing**: manual verification — child session replies appear in thread, root replies in main chat. File sends same.
