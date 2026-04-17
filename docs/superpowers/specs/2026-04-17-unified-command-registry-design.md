# Unified Command Registry — Commands as Adapter-Layer Meta-Operations

**Date:** 2026-04-17
**Status:** Draft

## Problem

`/spawn`, `/kill`, `/sessions` handling is scattered across three layers:

1. **`FeishuInboundHandler`** (`core/handlers/feishu.py:52-57`) — detects commands in inbound Feishu text, forwards via `Send(to="system:admin")`.
2. **`AdminHandler`** (`core/handlers/admin.py:19-30`) — re-checks the same prefix list, forwards via `Send(to="system:session-mgr")`.
3. **`CCAdapter._handle_spawn`** (`adapters/cc/adapter.py:361-436`) — parallel async path for CC-side MCP tool `spawn_session`, performs the real I/O (Feishu anchor + pin + tool card + tmux).
4. **`SessionMgrHandler`** (`core/handlers/session_mgr.py:83-154`) — pure-function spawn that registers an actor in `state="suspended"` **without** starting tmux.

This creates four concrete problems:

1. **Fragmented registration.** Adding a new command requires edits in at least three files, with duplicated quoted-prefix stripping in `feishu.py:52`, `admin.py:22`, and `session_mgr.py:59`.
2. **I/O model mismatch.** `/spawn` needs async I/O (create anchor, start tmux), but actor handlers are pure functions returning `list[Action]`. The only action type that reaches I/O is `TransportSend`, which isn't enough to orchestrate anchor → tool card → tmux in order.
3. **Thread-reply regression surface.** Feishu threads auto-prepend a `> quoted content\n` prefix to replies. The `startswith("/spawn")` check in each of the three layers must independently handle this — fixed three times in commits `585803c`, `a04c4b2`.
4. **Feishu/CC asymmetry.** A `/spawn` originating from Feishu goes through `SessionMgrHandler._handle_spawn` (pure function, only returns `SpawnActor` with `state="suspended"`, no tmux). A `spawn_session` from CC MCP goes through `CCAdapter._handle_spawn` (full async I/O including tmux). The former is a broken path — commit `fe500d5` restored full I/O on the CC side but Feishu-side spawns still create suspended actors with no tmux.

## Goals

1. **Single registration point.** Adding a new command = add one file under `commands/builtin/` and decorate one async function. No edits to adapters, handlers, or runtime.
2. **One place to do I/O.** Each command's I/O lives in its own async function alongside the command definition. No more "I/O in adapter but dispatch in handler."
3. **Any entry point works.** Main Feishu chat, Feishu thread reply (with quoted prefix), and CC MCP tool all dispatch through the same registry with the same semantics.
4. **Compositional topology.** A command invoked from within a running CC session automatically inherits the caller's actor as parent — without the command function having to know it's being nested.
5. **Remove the Feishu/CC asymmetry.** One code path performs the full spawn I/O regardless of entry point.

## Non-Goals

- Per-actor command overrides (a specific actor exposing its own `/spawn` variant). YAGNI — context injection through scope chain covers every current use case.
- Permission / ACL system. Current code has no ACL; preserving the status quo.
- Refactoring the actor model itself. Actors, handlers, and their pure-function contract stay unchanged.
- A full CLI framework (click/typer). Hand-rolled arg binding is sufficient for the current command surface (<10 commands).

## Design

### Architecture Overview

Commands are treated as **meta-operations on the actor system**, not as messages within it. They bypass the actor pipeline entirely and execute in the async adapter layer, where they have direct access to adapter I/O and the runtime's `spawn` / `stop` APIs.

```
┌─ External entries ────────────────────────────────────┐
│                                                       │
│   Feishu webhook              CC WebSocket            │
│        │                             │                │
│        ▼                             ▼                │
│   FeishuAdapter               CCAdapter               │
│   (async I/O)                 (async I/O)             │
│        │  parse command              │  parse action  │
│        │  (normalize + split)        │                │
│        └──────────────┬───────────────┘               │
│                       ▼                               │
│         ┌──── CommandDispatcher ──────┐               │
│         │  resolve scope from         │               │
│         │  source actor; dispatch     │               │
│         │  command; catch errors →    │               │
│         │  adapter.reply_error        │               │
│         └───────────┬─────────────────┘               │
│                     ▼                                 │
│         ┌──── CommandScope tree ──────┐               │
│         │  ROOT_SCOPE ← derived scopes│               │
│         │  built on-demand from       │               │
│         │  actor parent chain         │               │
│         └───────────┬─────────────────┘               │
│                     ▼                                 │
│         ┌──── Command function ───────┐               │
│         │  async def spawn(args,ctx): │               │
│         │    await adapter.XX(...)    │  ← all I/O    │
│         │    runtime.spawn(...)       │  ← state      │
│         └─────────────────────────────┘               │
└───────────────────────────────────────────────────────┘
                     │ runtime.spawn(), runtime.stop()
                     ▼
┌─ ActorRuntime (existing, unchanged) ───────────────────┐
│   actor tree + message pipeline + pure handlers        │
│   on_spawn / on_stop hooks stay pure (return Actions)  │
└────────────────────────────────────────────────────────┘
```

**Key claim:** The actor model remains the communication substrate for ongoing message flow (thread actor ↔ CC actor). Commands are one-shot meta-operations on that substrate and do not participate in it.

### New Package: `channel_server/commands/`

```
commands/
├── __init__.py           # exports: ROOT_SCOPE, CommandDispatcher, CommandContext
├── scope.py              # CommandScope class
├── dispatcher.py         # CommandDispatcher.dispatch_from_adapter
├── parse.py              # normalize_command_text, parse_command, bind_args
├── registry.py           # ROOT_SCOPE singleton
├── context.py            # CommandContext dataclass
├── errors.py             # UnknownCommand, BadArgs, CommandError
└── builtin/
    ├── __init__.py       # imports all command modules (triggers registration)
    ├── spawn.py
    ├── kill.py
    ├── sessions.py
    └── help.py
```

**Note on runtime method names.** The pseudo-code below uses `runtime.spawn(...)`, `runtime.stop(...)`, `runtime.lookup(...)`. `sessions` / `list` uses `runtime.actors` (the dict of `address → Actor`). These match the actual `ActorRuntime` surface; no new methods are required.

### CommandContext (`commands/context.py`)

```python
@dataclass
class CommandContext:
    source: str                         # "feishu" | "cc_mcp"
    user: str                           # feishu_user:xxx
    chat_id: str | None                 # top-level Feishu chat id
    current_actor: str | None           # actor address this command originated from
    parent_actor: str | None            # current_actor's parent (from scope default_ctx)
    raw_msg: Message | None             # original inbound message, for reply
    adapter: "AdapterProtocol"          # injected by dispatcher; used by command for reply/I/O
    runtime: "ActorRuntime"             # injected by dispatcher
```

Adapters are passed through ctx so command functions can call, e.g., `ctx.adapter.feishu.create_thread_anchor(...)` without module-level imports.

### CommandScope (`commands/scope.py`)

```python
class CommandScope:
    def __init__(self, parent: "CommandScope | None" = None, default_ctx: dict | None = None):
        self._commands: dict[str, CommandEntry] = {}
        self._parent = parent
        self._default_ctx = default_ctx or {}

    def register(self, name: str, args=None, help: str | None = None):
        def deco(fn):
            self._commands[name] = CommandEntry(fn=fn, args_schema=args, help=help)
            return fn
        return deco

    async def dispatch(self, name: str, tokens: list[str], ctx_dict: dict):
        merged = {**self._default_ctx, **ctx_dict}
        if name in self._commands:
            entry = self._commands[name]
            args = bind_args(entry.args_schema, tokens) if entry.args_schema else tokens
            return await entry.fn(args, CommandContext(**merged))
        if self._parent:
            return await self._parent.dispatch(name, tokens, merged)
        raise UnknownCommand(name)

    def list_commands_with_help(self) -> list[tuple[str, str]]:
        """Collect (name, help) pairs from this scope up through the parent chain."""
        seen = set()
        out = []
        scope = self
        while scope:
            for name, entry in scope._commands.items():
                if name not in seen:
                    seen.add(name)
                    out.append((name, entry.help or ""))
            scope = scope._parent
        return sorted(out)
```

### Scope Resolution (`commands/dispatcher.py`)

Scopes are **not persisted on actors**. They are built on demand from the actor parent chain, keeping `core/actor.py` unchanged.

```python
_MAX_SCOPE_DEPTH = 16

def resolve_scope(
    source_actor_addr: str | None, runtime: ActorRuntime, *, _depth: int = 0
) -> CommandScope:
    if source_actor_addr is None:
        return ROOT_SCOPE
    if _depth >= _MAX_SCOPE_DEPTH:
        raise CommandError(f"scope recursion exceeded {_MAX_SCOPE_DEPTH} levels")
    actor = runtime.lookup(source_actor_addr)
    if actor is None or actor.parent is None:
        # Dangling parent reference (e.g., after parent was stopped) degrades
        # to ROOT_SCOPE; command still runs but without the parent_actor ctx.
        return ROOT_SCOPE
    parent_scope = resolve_scope(actor.parent, runtime, _depth=_depth + 1)
    return CommandScope(
        parent=parent_scope,
        default_ctx={
            "current_actor": source_actor_addr,
            "parent_actor": actor.parent,
        },
    )
```

Recursion is bounded by actor depth in the persistence layer (expected ≤ 4 in practice). The hard cap of 16 is enforced in code above and surfaces as `CommandError` to the user.

### Topology Example (Addresses Goal #4 Composability)

Scenario: `cc:alice.main` is running; inside it, Claude invokes MCP tool `spawn_session name=sub`.

1. `CCAdapter` receives WS action; `identify_source_actor(ws)` returns `"cc:alice.main"`.
2. Dispatcher translates to `dispatch_from_adapter(raw_text="/spawn sub", source_actor="cc:alice.main")`.
3. `resolve_scope("cc:alice.main")` recurses: `parent="system:admin"` → `system:admin.parent=None` → `ROOT_SCOPE`. Builds a two-level derived scope with `default_ctx={current_actor: "cc:alice.main", parent_actor: "system:admin"}`.
4. Leaf scope has no `"spawn"`; lookup falls through to ROOT, which has it. Merged ctx carries `current_actor="cc:alice.main"`.
5. `spawn()` function sees `ctx.current_actor` and sets the new actor's `parent=ctx.current_actor`. The new session becomes a child of `alice.main`.

**`spawn()` is defined exactly once**; topology threads through ctx.

### Command Parsing (`commands/parse.py`)

Three stages, each isolated:

**Stage 1 — normalize_command_text** (handles Goal #3 regression):

```python
def normalize_command_text(text: str) -> str | None:
    """Extract command line; return None if not a command."""
    if not text:
        return None
    last_line = next(
        (ln for ln in reversed(text.splitlines()) if ln.strip()),
        ""
    ).strip()
    if not last_line.startswith("/"):
        return None
    return last_line
```

Replaces the three scattered prefix-stripping fragments.

**Stage 2 — parse_command**:

```python
@dataclass
class CommandInvocation:
    name: str          # without leading "/"
    raw_args: str
    tokens: list[str]  # shlex.split of raw_args

def parse_command(normalized: str) -> CommandInvocation:
    body = normalized[1:]
    head, _, rest = body.partition(" ")
    return CommandInvocation(
        name=head.strip(),
        raw_args=rest.strip(),
        tokens=shlex.split(rest) if rest else [],
    )
```

**Stage 3 — bind_args**: Each command declares an optional `@dataclass` schema. `CommandScope.dispatch` calls `bind_args(entry.args_schema, tokens)` internally when invoking the command:
- Positional: tokens consumed in field order.
- Named: `--key value` or `--key=value`.
- Missing required → `BadArgs`.
- Extra unknown → `BadArgs`.

If schema is `None`, the raw token list is passed through.

`BadArgs` raised inside `bind_args` propagates up through `scope.dispatch` unchanged — scopes do **not** catch it. The outermost `CommandDispatcher.dispatch_from_adapter` is the single catch point (see next section).

### Dispatcher Entry Point (`commands/dispatcher.py`)

```python
class CommandDispatcher:
    def __init__(self, runtime: ActorRuntime, *, fallback_on_unknown: bool = False):
        self._runtime = runtime
        # During migration, True lets unregistered commands fall through to the
        # legacy actor pipeline. Flipped to False at Phase 4 cleanup.
        self._fallback_on_unknown = fallback_on_unknown

    async def dispatch_from_adapter(
        self, *, adapter, raw_text: str, source_actor: str | None, ctx_partial: dict
    ) -> bool:
        """Returns True iff the command was handled here and the adapter should stop
        processing. Returns False for non-command text, and — when
        fallback_on_unknown is True — also for unregistered commands, so the adapter
        can deliver them to the legacy pipeline during migration."""
        normalized = normalize_command_text(raw_text)
        if normalized is None:
            return False
        invocation = parse_command(normalized)
        ctx_partial = {**ctx_partial, "adapter": adapter, "runtime": self._runtime}
        try:
            scope = resolve_scope(source_actor, self._runtime)
            await scope.dispatch(invocation.name, invocation.tokens, ctx_partial)
        except UnknownCommand as e:
            if self._fallback_on_unknown:
                return False   # let legacy pipeline see it
            await adapter.reply_error(ctx_partial, f"未知命令: /{e.name}\n发送 /help 查看可用命令")
        except BadArgs as e:
            await adapter.reply_error(ctx_partial, f"参数错误: {e}")
        except CommandError as e:
            await adapter.reply_error(ctx_partial, f"命令失败: {e}")
        return True
```

The `fallback_on_unknown` flag is the migration bridge. In Phase 2, only `/help` is registered; `/spawn`, `/kill`, `/sessions` still need the legacy admin → session-mgr path. With the flag `True`, the dispatcher returns `False` for unknown commands, and the adapter proceeds to deliver the message to the runtime as before. Once every command is ported (end of Phase 3), the flag flips to `False` and unknown commands become hard errors.

### Adapter Integration

**FeishuAdapter** (inbound handler):

```python
async def on_inbound(event):
    ...
    if await command_dispatcher.dispatch_from_adapter(
        adapter=self,
        raw_text=event.text,
        source_actor=self.identify_source(event),
        ctx_partial={
            "source": "feishu",
            "user": event.user,
            "chat_id": event.chat_id,
            "raw_msg": event.as_message(),
        },
    ):
        return  # command handled
    # otherwise deliver to existing Message pipeline
    runtime.send(to=<inbound_actor_addr>, message=Message(...))
```

`identify_source(event)`:
- Main chat (no thread context) → `None` → ROOT_SCOPE.
- Thread reply → lookup thread actor by `root_id` (anchor msg_id) → that actor's address.

**CCAdapter** (WS action handler):

The CC MCP surface includes ~9 WS actions. Only those that are **meta-operations on the actor system** route through the command registry. Conversation-level actions (reply, react, send_file, send_summary, update_title, forward) stay on the direct WS → actor path because they're not commands — they're messages emitted by CC that should flow into the actor pipeline.

```python
# Meta-operation actions (management of sessions) → command registry
WS_ACTION_TO_COMMAND: dict[str, str] = {
    "spawn_session": "spawn",
    "kill_session":  "kill",
    "list_sessions": "sessions",
}

# Everything else (reply, react, send_file, send_summary, update_title,
# forward, tool_notify, ...) continues to flow through the existing
# actor-message path.

def ws_args_to_text(cmd_name: str, payload: dict) -> str:
    """Serialize WS action payload to shell-command form for shlex round-trip.

    Conventions:
    - spawn_session: {name, tag?}     → "<name> [--tag <tag>]"
    - kill_session:  {name}           → "<name>"
    - list_sessions: {}               → ""

    All values are shlex.quote()-ed so that re-parsing with shlex.split() yields
    the original tokens, including when values contain spaces.
    """
    ...
```

Call site:

```python
if action in WS_ACTION_TO_COMMAND:
    cmd_name = WS_ACTION_TO_COMMAND[action]
    raw_text = f"/{cmd_name} {ws_args_to_text(cmd_name, msg)}".strip()
    await command_dispatcher.dispatch_from_adapter(
        adapter=self,
        raw_text=raw_text,
        source_actor=self._ws_to_actor(ws),
        ctx_partial={
            "source": "cc_mcp",
            "user": self._ws_user(ws),
            "chat_id": self._ws_chat(ws),
            "raw_msg": None,
        },
    )
    return
```

`self._ws_to_actor(ws)` looks up the CC actor bound to this WS connection (e.g., `cc:alice.main`).

### Command: spawn (`commands/builtin/spawn.py`)

This is where `CCAdapter._handle_spawn` (lines 361-436) and `SessionMgrHandler._handle_spawn` (lines 126-154) merge. The new single implementation:

```python
@dataclass
class SpawnArgs:
    tag: str = ""

@ROOT_SCOPE.register("spawn", args=SpawnArgs, help="/spawn [tag] — create a session")
async def spawn(args: SpawnArgs, ctx: CommandContext):
    tag = args.tag or generate_tag()
    user = ctx.user
    session_name = derive_session_name(user, tag)

    # I/O phase (order preserved from CCAdapter._handle_spawn)
    anchor = await ctx.adapter.feishu.create_thread_anchor(ctx.chat_id, tag)
    await ctx.adapter.feishu.pin_message(anchor)
    tool_card = await ctx.adapter.feishu.create_tool_card(ctx.chat_id, tag)

    # Register actors — address schemes preserved from the old SessionMgrHandler
    # path for persistence-layer compatibility. Migration is drop-in; stored
    # actors on disk continue to round-trip.
    app_id = ctx.adapter.feishu.app_id_for(ctx.chat_id)
    thread_addr = f"feishu:{app_id}:{ctx.chat_id}:thread:{session_name}"
    cc_addr = f"cc:{user}.{session_name}"

    ctx.runtime.spawn(
        address=thread_addr,
        handler="feishu_inbound",
        tag=tag,
        parent=ctx.current_actor,
        downstream=[cc_addr],
        metadata={"chat_id": ctx.chat_id, "tag": tag, "mode": "child",
                  "root_id": anchor},
        transport=Transport(type="feishu_thread",
                            config={"chat_id": ctx.chat_id, "root_id": anchor}),
    )
    ctx.runtime.spawn(
        address=cc_addr,
        handler="cc_session",
        tag=tag,
        parent=ctx.current_actor or f"cc:{user}.root",
        downstream=[thread_addr],
        state="active",   # active from the start — not "suspended"
        metadata={"chat_id": ctx.chat_id, "tag": tag},
    )

    # Start tmux — full I/O, no more suspended-actor path
    await ctx.adapter.cc.spawn_tmux(user, session_name, tag, cc_addr)

    await ctx.adapter.reply(ctx, f"Session {tag} started")
```

**The fix for Goal #4** lives here: there is no longer a "Feishu path creates suspended actor, CC path creates active actor" asymmetry — one implementation does the full sequence.

### Commands: kill / sessions / help

`kill` symmetrically tears down tmux, unpins anchor, and stops actors via `runtime.stop(addr)`.
`sessions` iterates `runtime.actors.values()` filtered to `cc:*` addresses owned by the invoker.
`help` calls `scope.list_commands_with_help()` — new commands appear automatically.

### Actor Lifecycle Hooks (Unchanged Framework)

`on_spawn` and `on_stop` on handlers remain pure (return `list[Action]`). They are used for **actor-state side effects** (e.g., CC actor notifying its parent thread on startup), not for I/O orchestration. The I/O formerly pushed through them in `SessionMgrHandler._handle_spawn` now lives in the `spawn` command. No framework changes.

## Migration Plan

Each phase ends in a shippable state; the system runs through all phases.

### Phase 1 — Parallel Scaffold (no runtime effect)

- Create `commands/` package with all files (`scope`, `dispatcher`, `parse`, `context`, `errors`, `registry`, `builtin/help.py`).
- Register only `/help` in `ROOT_SCOPE` (no I/O, safest first command).
- Add unit tests for `parse`, `scope`, `bind_args`.
- No adapter changes; old system unchanged.

**Exit criteria:** unit tests pass; CI green.

### Phase 2 — Adapter Hookup (only /help live)

- Modify `FeishuAdapter.on_inbound` and `CCAdapter` WS dispatch to call `command_dispatcher.dispatch_from_adapter` first.
- Unknown commands return from dispatcher as `UnknownCommand` → adapter falls back to old code path.
- `/help` now served by new system; other commands still take old path.

**Exit criteria:** smoke checklist for Phase 2 passes (see Testing section).

### Phase 3 — Port Commands One at a Time

Order: `sessions` → `kill` → `spawn` (spawn last because it's the most I/O-heavy and highest-risk).

For each:
1. Write `commands/builtin/{name}.py`.
2. Add integration test (Layer 2, see Testing).
3. Delete corresponding old path in `admin.py` / `session_mgr.py` / `adapter.py` (`_handle_spawn`).
4. Run smoke checklist.
5. Commit.

**For `spawn` specifically:** merge `CCAdapter._handle_spawn` I/O sequence and `SessionMgrHandler._handle_spawn` actor-registration into the single `commands/builtin/spawn.py`. Preserve the I/O order verbatim from the CC path (which is the complete path). This is where Goal #4 is closed out.

**Exit criteria:** all four commands (`spawn`/`kill`/`sessions`/`help`) run through new system; integration tests pass.

### Phase 4 — Cleanup

- Flip `CommandDispatcher(fallback_on_unknown=False)`.
- Delete `channel_server/core/handlers/session_mgr.py`.
- Delete `channel_server/core/handlers/admin.py`. The admin actor can use the existing `ForwardAllHandler` because:
  - Commands never reach the admin actor — the dispatcher at the adapter layer has already intercepted and replied (`reply_error` for unknown slash commands).
  - That leaves only the system-notification and non-slash forwarding branches, which `ForwardAllHandler` already performs unconditionally.
- Remove command-detection lines from `handlers/feishu.py`.
- **Clear the anchor/tool-card creation branch in `FeishuInboundHandler.on_spawn` (feishu.py:78-98, mode=="child").** The spawn command now creates these I/O artifacts before the thread actor is spawned, so leaving the `on_spawn` branch in place would create duplicate anchors.
- Remove `system:admin` and `system:session-mgr` actor registration from startup code; ensure startup still creates an equivalent "main chat forwarder" actor using `ForwardAllHandler` if needed.
- Update any README / architecture docs.

**Exit criteria:** full end-to-end smoke passes, no references to deleted handlers anywhere in the tree.

## Testing Strategy

### Layer 1 — Pure Component Unit Tests

- `tests/commands/test_parse.py`: `normalize_command_text` cases (empty, quoted prefix with `> ... \n`, multi-line, trailing whitespace); `parse_command` (shlex quoting, empty args).
- `tests/commands/test_scope.py`: decorator registration, flat dispatch, nested dispatch fallback, ctx merge precedence (child overrides parent), `UnknownCommand` when not found.
- `tests/commands/test_bind_args.py`: positional binding, named binding, missing required → `BadArgs`, extra unknown → `BadArgs`.

### Layer 2 — Dispatcher + Commands Integration

Fixture: `FakeFeishuAdapter` and `FakeCCAdapter` that record method calls; real `ActorRuntime`.

| Test | Trigger | Asserts |
|------|---------|---------|
| main chat /spawn | `source_actor=None` | anchor, tool_card, tmux called in order; actor registered with no parent |
| thread /spawn with quoted prefix | `raw_text="> @user said X\n/spawn foo"` | behaves identically to clean text (**Goal #3 regression**) |
| CC MCP spawn_session | `source_actor="cc:alice.main"` | new actor's `parent="cc:alice.main"` (**Goal #4 composability**) |
| /kill foo | valid session | tmux killed, anchor unpinned, actor stopped |
| /sessions | — | returns `cc:*` actors in runtime |
| /unknown | — | `reply_error` called with "未知命令" |
| /kill (no name) | — | `reply_error` called with "参数错误" |

### Layer 3 — Regression Tests (Pin the Goals)

`tests/regression/test_unified_cmd_goals.py`:
- **Goal #1**: Add a new `/ping` command in a single file, verify it's discoverable, dispatchable, and appears in `/help`.
- **Goal #2**: In spawn integration test, assert I/O call order (anchor before tmux).
- **Goal #3**: Covered by parse unit tests above.
- **Goal #4 symmetry**: Side-by-side test — spawn once from Feishu entry, once from CC MCP entry, assert resulting actor states are equivalent modulo `parent`.

### Layer 4 — Manual Smoke Checklists per Phase

**After Phase 2:**
- [ ] Main chat `/help` — replies.
- [ ] Thread `/help` — replies.
- [ ] Main chat `/spawn foo` — still works (old path).
- [ ] Thread `/sessions` — still works (old path).

**After each Phase-3 command port:**
- [ ] Main chat trigger — same behavior as before port.
- [ ] Thread trigger — same behavior as before port.
- [ ] CC MCP trigger (where applicable) — same behavior.
- [ ] Unknown command — friendly error.
- [ ] Bad args — friendly error.

**After Phase 4:**
- [ ] End-to-end: main chat `/spawn parent` → enter thread → child CC `/spawn child` → `/sessions` lists both levels → `/kill child` → `/kill parent`.
- [ ] Restart server, actors recover.
- [ ] `grep` for `SessionMgrHandler` / `AdminHandler` / `_handle_spawn` returns nothing.

### Explicitly Not Tested (YAGNI)

- The decorator mechanics themselves.
- Recursion depth pathologies.
- External adapter I/O (belongs to adapter tests).
- Error-message text wording.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| spawn I/O order subtly differs post-migration | Copy order verbatim from `CCAdapter._handle_spawn`; integration test asserts call order |
| CC MCP action protocol stays on WS side; dispatcher expects text | Adapter translates WS action → command text as a thin adapter layer |
| Hot session surviving Phase 3 mid-migration (some commands on new path, some on old) | Both paths reach the same runtime APIs; actor state not touched by migration. Phase 3 is per-command atomic. |
| Deep actor chain stack overflow in `resolve_scope` | Hard cap at 16 levels; raise `CommandError` if exceeded |
| Users developed muscle memory for old error messages | Preserve existing phrasing ("未知命令", "发送 /help") in dispatcher error paths |

## Estimated Effort

- Phase 1: 0.5 day (pure scaffolding + unit tests)
- Phase 2: 0.5 day (adapter hookup + /help)
- Phase 3: 1.5 days (three commands; spawn dominates)
- Phase 4: 0.5 day (deletion + startup cleanup)
- **Total: ~3 person-days**, each phase independently commitable.

## Open Questions

None at this time. All architectural decisions were walked through and approved in an interactive brainstorming session with 林懿伦 over the `openclaw-channel` (Feishu thread, session tag `unified-cmd`, 2026-04-17). Key decisions captured above:

- Option A (commands bypass actor pipeline, live in adapter layer) — see "Architecture Overview."
- Decorator + scope chain registry form — see "CommandScope."
- Implicit scope resolution (adapter knows source actor, command does not) — see "Scope Resolution."
- I/O lives in the command function, not in lifecycle hooks — see "Command: spawn."
