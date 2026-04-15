# Actor Model Channel Server — Design Spec

**Date:** 2026-04-15
**Status:** Approved
**Author:** 林懿伦 + Claude (root session)

## Overview

Replace the current ad-hoc routing in channel-server with an actor model architecture. Every participant (CC session, Feishu chat position, system service) is modeled as an actor with a unique address. Channel-server becomes an actor runtime that manages actor lifecycle, message dispatch, and persistence.

## Motivation

Current problems:
- Routing logic scattered across `exact_routes`, `thread_routes`, `session_threads`, `_tool_threads` — 5+ dict structures with no unifying abstraction
- Output routing (reply, tool_notify, send_summary) bypasses inbound routing logic — asymmetric paths
- All sessions share one `chat_id`; the routing destination (main chat vs thread) is determined ad-hoc by each handler
- channel_server.py is ~2000 lines with mixed concerns
- Adding new IM channels (beyond Feishu) would require duplicating routing logic

## Architecture

### Approach: Event-Driven Actor Runtime (Option C)

Core actor runtime is IM-agnostic. Feishu and CC are adapters. This supports:
- Future IM channels (Slack, Discord, etc.) as new adapters
- Future migration of core to Rust+PyO3 (core interface is the stable boundary)

### Directory Structure

```
channel_server/
  __init__.py
  app.py                — Entry point: init runtime + adapters, start service

  core/
    __init__.py
    actor.py            — Actor, Transport, Message, Action dataclasses
    handler.py          — Handler protocol + built-in handlers
    runtime.py          — ActorRuntime: registry, mailbox, message loop
    persistence.py      — actors.json read/write

  adapters/
    __init__.py
    feishu/
      __init__.py
      adapter.py        — Feishu event inbound + Feishu API outbound
      parsers.py        — Feishu message parsing (from message_parsers.py)
    cc/
      __init__.py
      adapter.py        — WebSocket server (accepts CC connections)
      channel.py        — MCP client (runs in CC session process)
```

Old `feishu/` directory is retired.

## Core: Actor Model

### Actor

Every participant in the system is an actor. Actors have a unique address, a handler that defines behavior, and an optional transport for external I/O.

```python
@dataclass
class Actor:
    address: str           # Unique address: "feishu:oc_xxx", "cc:linyilun.root"
    tag: str               # Human-readable label
    state: str             # "active" | "suspended" | "ended"
    parent: str | None     # Parent actor address (child→root relationship)
    downstream: list[str]  # Downstream actor addresses (who I forward to)
    handler: str           # Handler type: "feishu_inbound", "cc_session", etc.
    transport: Transport | None  # Connection to external system
    metadata: dict         # Free-form extension fields
    created_at: str
    updated_at: str
```

### Transport

The connection between an actor and the external system it represents.

```python
@dataclass
class Transport:
    type: str              # "websocket" | "feishu_chat" | "feishu_thread"
    config: dict           # type-specific: ws connection id, chat_id, anchor_msg_id, etc.
```

Transport is optional. System actors (e.g. `system:slash_handler`) have no transport — their input and output are purely actor messages.

### Message

```python
@dataclass
class Message:
    sender: str          # Sender actor address
    type: str            # "text" | "command" | "file" | "system" | "error"
    payload: dict        # Message content
    metadata: dict       # Tracing info (timestamp, message_id, etc.)
```

### Actions (Handler Return Values)

Inspired by Erlang gen_server return tuples. Handlers are pure functions that return action lists; runtime executes side effects.

```python
@dataclass
class Send:
    to: str
    message: Message

@dataclass
class TransportSend:
    """Send through actor's own transport (e.g. Feishu API, WebSocket push)"""
    payload: dict

@dataclass
class UpdateActor:
    """Modify actor's own state"""
    changes: dict

@dataclass
class SpawnActor:
    """Create a new actor"""
    address: str
    handler: str
    kwargs: dict

@dataclass
class StopActor:
    address: str
```

### Actor Address Naming

- CC actor: `cc:{user}.{session}` — `cc:linyilun.root`, `cc:linyilun.dev`
- Feishu chat actor: `feishu:{chat_id}` — `feishu:oc_xxx`
- Feishu thread actor: `feishu:{chat_id}:{anchor_msg_id}` — `feishu:oc_xxx:om_abc`
- System actor: `system:{name}` — `system:slash_handler`
- Tool card actor: `cc:{user}.{session}:tool_card` — `cc:linyilun.root:tool_card`

All actors support a `tag` field for human-readable display.

## Core: Runtime

Minimal runtime inspired by Erlang VM. Each actor runs as an independent asyncio task with its own mailbox.

```python
class ActorRuntime:
    actors: dict[str, Actor]
    mailboxes: dict[str, asyncio.Queue]

    # Core API
    def spawn(self, address, handler, **kwargs) -> Actor
    def stop(self, address)
    def send(self, to: str, message: Message)
    def lookup(self, address: str) -> Actor | None

    # Transport management
    def attach(self, address: str, transport: Transport)
    def detach(self, address: str)

    # Persistence
    def save_state(self)
    def restore_state(self)

    # Lifecycle
    async def run()
    async def shutdown()
```

### Per-Actor Concurrency

Each actor gets its own asyncio coroutine (like an Erlang process):

```python
async def _actor_loop(self, actor: Actor):
    mailbox = self.mailboxes[actor.address]
    handler = get_handler(actor.handler)
    while actor.state == "active":
        msg = await mailbox.get()
        actions = handler.handle(actor, msg)
        for action in actions:
            self._execute(action)
```

`send()` is non-blocking — it puts the message in the target's mailbox queue.

## Handlers: Actor Behavior Types

Inspired by Erlang gen_server callback modules. Each handler defines how an actor processes messages.

```python
class Handler(Protocol):
    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        ...

# Handler registry: maps handler name string → Handler instance
HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
    # Future: "slash_filter": SlashFilterHandler(),
}

def get_handler(name: str) -> Handler:
    return HANDLER_REGISTRY[name]
```

### Built-in Handlers

1. **`feishu_inbound`** — Default for Feishu actors. Receives Feishu messages, forwards to all downstream actors.

2. **`cc_session`** — CC actor behavior. Receives messages and pushes via WebSocket transport. Receives CC replies/forwards and converts to Send actions.

3. **`forward_all`** — Unconditional broadcast to all downstream.

4. **`slash_filter`** — (Future) Intercepts slash commands, processes them, forwards non-commands to downstream.

5. **`tool_card`** — Tool notification actor. Receives tool_notify messages, maintains last 5 entries in metadata, updates Feishu interactive card via transport.

### Routing via Actor Topology (Not Middleware)

Following Erlang best practice: routing changes are topology changes, not runtime middleware. To insert a slash command handler:

```
Before: feishu:oc_xxx → cc:linyilun.root
After:  feishu:oc_xxx → system:slash_handler → cc:linyilun.root
```

Change each actor's `downstream` list. No middleware/pipeline engine needed.

## Adapters

Adapters bridge external systems to the actor runtime. They do NOT make routing decisions.

### Feishu Adapter

**Inbound:** Feishu WebSocket event → determine sender actor address (chat_id + optional thread_id) → auto-spawn feishu actor if it doesn't exist → `runtime.send(to=sender_address, message)`.

**Outbound:** When an actor with a Feishu transport needs to send, the adapter calls the appropriate Feishu API (CreateMessage for chat, ReplyMessage for thread, PatchMessage for card update).

### CC Adapter

**Inbound (registration):** CC session connects via WebSocket → sends register message → adapter attaches WebSocket transport to the corresponding CC actor → actor state: suspended → active → flush queued messages.

**Inbound (messages):** CC sends reply/forward/send_summary via MCP tool → adapter converts to actor Message → `runtime.send(to=sender_cc_actor, message)`.

**Outbound:** When a CC actor receives a message, the adapter pushes it through the WebSocket transport to the CC session.

### MCP Tool Interface (channel.py)

Semantic tool names preserved for LLM reliability (Option B from brainstorming):

- `reply(text)` — Reply to current conversation (implicit: send to bound feishu actor)
- `forward(to, text)` — Send to another actor by address
- `send_summary(summary)` — Notify root session's main chat (human-readable, CC should not respond)
- `update_title(title)` — Update this session's thread anchor card
- `send_file(chat_id, file_path)` — Send file
- `react(message_id, emoji)` — Add reaction
- `spawn_session(name, tag)` — Spawn child session
- `kill_session(name)` — Kill child session
- `list_sessions()` — List active sessions

All tools internally convert to actor messages dispatched through the runtime.

## Actor Topology: Example Scenario

User `linyilun` with root session and a spawned `dev` child session:

```
feishu:oc_xxx                       (DM main, handler: feishu_inbound)
  downstream: [cc:linyilun.root]

feishu:oc_xxx:om_anchor_dev         (dev thread, handler: feishu_inbound)
  downstream: [cc:linyilun.dev]

cc:linyilun.root                    (root CC session, handler: cc_session)
  downstream: [feishu:oc_xxx]
  parent: None

cc:linyilun.dev                     (dev CC session, handler: cc_session)
  downstream: [feishu:oc_xxx:om_anchor_dev]
  parent: cc:linyilun.root

cc:linyilun.root:tool_card          (root tool notifications, handler: tool_card)
  downstream: [feishu:oc_xxx]

cc:linyilun.dev:tool_card           (dev tool notifications, handler: tool_card)
  downstream: [feishu:oc_xxx:om_anchor_dev]
```

### Message Flow Examples

**User sends "hello" in DM main:**
```
feishu:oc_xxx receives → handler forwards to downstream → cc:linyilun.root receives → transport pushes to CC via WebSocket
```

**CC root replies "world":**
```
cc:linyilun.root receives reply → handler sends to downstream → feishu:oc_xxx receives → transport calls Feishu API → message appears in DM
```

**CC dev calls send_summary:**
```
cc:linyilun.dev receives → handler sends to parent's feishu actor (feishu:oc_xxx) → transport sends to DM main chat
```

## Spawn Flow

1. User sends `/spawn dev` → reaches `cc:linyilun.root` → CC calls spawn_session MCP tool
2. CC adapter receives spawn request → converts to SpawnActor action
3. Runtime creates feishu thread actor:
   - Feishu adapter creates interactive card + thread → returns anchor_msg_id
   - Pin the anchor message
   - Spawn `feishu:oc_xxx:om_anchor_dev` (handler: feishu_inbound, downstream: [cc:linyilun.dev])
4. Runtime creates CC actor:
   - Spawn `cc:linyilun.dev` (handler: cc_session, state: suspended, parent: cc:linyilun.root, downstream: [feishu:oc_xxx:om_anchor_dev])
5. Runtime creates tool card actor:
   - Spawn `cc:linyilun.dev:tool_card` (handler: tool_card, downstream: [feishu:oc_xxx:om_anchor_dev])
6. Start CC session process via tmux (cc-openclaw.sh)
7. CC session connects → cc adapter attaches transport → state: active → flush mailbox

## Kill Flow

1. Update feishu thread actor's card to red "ended"
2. Stop `cc:linyilun.dev` (detach transport, kill tmux window)
3. Stop `cc:linyilun.dev:tool_card`
4. Stop `feishu:oc_xxx:om_anchor_dev`
5. All actors state → ended, persist

## Persistence and Restart Recovery

### Persisted (actors.json)

```json
{
  "feishu:oc_xxx": {
    "address": "feishu:oc_xxx",
    "tag": "林懿伦 DM",
    "handler": "feishu_inbound",
    "state": "active",
    "parent": null,
    "downstream": ["cc:linyilun.root"],
    "transport_config": {"type": "feishu_chat", "chat_id": "oc_xxx"},
    "metadata": {}
  },
  "cc:linyilun.root": {
    "address": "cc:linyilun.root",
    "tag": "root",
    "handler": "cc_session",
    "state": "active",
    "parent": null,
    "downstream": ["feishu:oc_xxx"],
    "transport_config": null,
    "metadata": {}
  }
}
```

### Not Persisted

- Transport instances (WebSocket connections, Feishu client references) — rebuilt at runtime
- Mailbox contents (unprocessed messages lost on restart — acceptable)

### Restart Recovery

1. channel-server starts → load actors from actors.json
2. All actors marked `suspended` (transport not yet restored)
3. Feishu adapter starts → rebuild transport for all feishu actors → state: active
4. CC sessions reconnect → cc adapter attaches transport → state: active
5. Messages received while CC actor was suspended queue in mailbox, flushed on attach

Replaces `thread_anchors.json`. Thread anchor info stored in feishu thread actor's transport_config. `chat_id_map.json` remains independent (user discovery mechanism, not actor state).

## Error Handling

Inspired by Erlang "let it crash" + supervisor notification.

### Per-Message Error Isolation

```python
async def _actor_loop(self, actor: Actor):
    while actor.state == "active":
        try:
            msg = await mailbox.get()
            actions = handler.handle(actor, msg)
            for action in actions:
                self._execute(action)
        except Exception as e:
            log.error("Actor %s handler error: %s", actor.address, e)
            if actor.parent:
                self.send(to=actor.parent, message=Message(
                    sender=actor.address, type="error",
                    payload={"error": str(e)}))
```

Single message failure does not crash the actor.

### Transport Disconnect

- WebSocket disconnect → detach transport → actor state: suspended (not ended)
- CC session reconnects → reattach → state: active → flush mailbox
- Configurable timeout: if not reconnected → state: ended → notify parent

### Feishu API Failures

- Send failure → log error, do not crash actor
- Retry strategy handled by adapter layer, not core

### Actor Crash (Repeated Handler Failure)

- N consecutive failures → actor state: ended
- Notify parent actor → parent decides to re-spawn or alert user

### Not Implemented (YAGNI)

- No supervision tree (current scale doesn't need it)
- No hot code reload
- No distributed actors (single machine is sufficient)

## Future: Rust Migration Path

Core interface (Actor, Message, Runtime API) is the stable boundary:

```
Current:  core/*.py (Python)  ←→  adapters/*.py (Python)
Future:   core-rs/ (Rust+PyO3 .so)  ←→  adapters/*.py (Python, import core_rs)
```

Adapters remain Python (Feishu SDK, MCP, WebSocket libs are Python). Core becomes Rust for performance if needed. The adapter-facing API doesn't change.
