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

2. **`/spawn`, `/kill`, `/sessions` flow through the LLM.**
   User sends `/spawn voice-widget` in Feishu → admin actor → cc:user.root → Claude interprets → calls MCP tool → channel server executes.
   These are deterministic commands that waste tokens and add latency.

3. **Tool cards are created in the wrong place.**
   All child session tool cards appear in the main chat window instead of inside their respective threads.

## Goals

1. All sessions (including root) get a tool card on initialization.
2. `/spawn`, `/kill`, `/sessions` are intercepted by AdminHandler and never reach the CC session.
3. Child session tool cards are created inside their thread, not in the main chat.
4. Initialization logic is unified: root and child sessions share the same path through `session-mgr`.

## Architecture

### New Actor Topology

```
User message → feishu adapter
                    ↓
            feishu:{chat_id}  (main chat actor)
                    ↓
            system:admin  (AdminHandler — pure router)
               ├── normal messages → cc:user.root → feishu:{chat_id}
               └── /spawn /kill /sessions → system:session-mgr
                                                ↓
                                        SessionMgrHandler (orchestration)
                                          ├── SpawnActor (feishu thread)
                                          ├── SpawnActor (cc session)
                                          └── reply → feishu:{chat_id}
```

### New Components

**`system:session-mgr` actor** — created at server startup. Lightweight orchestrator: receives session commands, decides which actors to create/destroy, returns Action lists. No transport of its own.

**`SessionMgrHandler`** (`core/handlers/session_mgr.py`) — pure function handler. Handles three command types (spawn, kill, sessions) plus init_session for register unification.

### Modified Components

**`AdminHandler`** (`core/handlers/admin.py`) — adds interception: `/spawn`, `/kill`, `/sessions` → `Send(to="system:session-mgr", message=msg)`. All other messages forwarded downstream as before.

**Feishu adapter** — injects `chat_id`, `user_id`, `user` into message payload on every forwarded message. Eliminates runtime.lookup dependency from handlers.

**`_handle_register`** (`adapters/cc/adapter.py`) — stripped to pure transport duties: WebSocket mapping + transport attach. New root actors trigger `init_session` message to session-mgr for tool card creation.

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
        # Wire thread → cc. Current UpdateActor only targets self (the emitting actor).
        # Need a new WireActor action type with a target field:
        WireActor(target=thread_addr, downstream=[cc_addr]),
        # Implementation: add WireActor dataclass to core/actor.py and
        # handle it in runtime._execute() by updating the target actor's downstream.
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
                           state="active", transport=Transport(type="ws", ws=ws))
    elif actor.state in ("suspended", "disconnected"):
        self.runtime.attach(address, transport=Transport(type="ws", ws=ws))
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

**Fix:** Add optional `root_id` parameter:

```python
async def create_tool_card(self, chat_id, label, root_id=None):
    body = {"receive_id": chat_id, "msg_type": "interactive", "content": card_json}
    if root_id:
        body["root_id"] = root_id   # card appears inside thread
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

Recommendation: pragmatic approach — `handle(actor, msg, runtime)`. The runtime reference is read-only for validation/query; mutations still go through returned Actions. Existing handlers that don't need runtime keep the `handle(actor, msg)` signature — runtime passes the third arg only if the handler accepts it (inspect signature or use `**kwargs`).

## Race Conditions

**Duplicate `/spawn` for the same session name:** Between `runtime.lookup()` in the handler and `SpawnActor` execution, a second `/spawn` could pass the same check. Mitigation: `runtime.spawn()` raises `ValueError` on duplicate address. The action executor should catch this and send an error reply to the feishu actor rather than crashing the session-mgr loop. Add a try/except in `_execute` around `SpawnActor` handling.

## Migration Strategy

This refactoring can be done incrementally:

1. **Add lifecycle hooks to runtime** (on_spawn, on_stop) — no behavior change, hooks are empty.
2. **Add `chat_id` injection in feishu adapter** — backward compatible, extra field in payload.
3. **Create `SessionMgrHandler` and `system:session-mgr` actor** — new code, no existing behavior changed.
4. **Update `AdminHandler`** to intercept session commands → session-mgr. This is the switch-over point.
5. **Add on_spawn to feishu_thread handler** — tool card + thread anchor creation moves from adapter to handler.
6. **Add on_spawn to cc_session handler** — tmux spawn moves from adapter to handler.
7. **Simplify `_handle_register`** — remove business init, delegate to session-mgr.
8. **Fix tool card location** — add `root_id` to `create_tool_card`, pass in feishu_thread on_spawn.
9. **Remove dead code** from `_handle_spawn` in CCAdapter (business logic moved to handlers).

## Testing

- **SessionMgrHandler**: unit test — input messages, assert returned Action lists. No feishu/tmux mocking needed.
- **AdminHandler**: unit test — verify session commands route to session-mgr, others route downstream.
- **Lifecycle hooks**: integration test — spawn actor, verify on_spawn actions executed.
- **Tool card location**: manual verification in Feishu — child card inside thread, root card in main chat.
