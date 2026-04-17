# Unified Command Registry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate `/spawn`, `/kill`, `/sessions` handling into a single adapter-layer command registry. Commands bypass the actor pipeline and execute as async meta-operations with composable scope chains mirroring the actor tree.

**Architecture:** A new `channel_server/commands/` package hosts a `CommandDispatcher` called by `FeishuAdapter` and `CCAdapter` before the actor pipeline. A `CommandScope` tree is derived on demand from the actor `parent` chain, injecting `current_actor` / `parent_actor` into each command's context so child-session spawns automatically nest. I/O (Feishu anchor, pin, tool card, tmux) lives inside each command's async function, not in lifecycle hooks.

**Tech Stack:** Python 3.11+, asyncio, pytest, `unittest.mock.MagicMock`, `shlex`, `dataclasses`.

**Spec:** `docs/superpowers/specs/2026-04-17-unified-command-registry-design.md`

---

## File Map

| Action | File | Change |
|--------|------|--------|
| Create | `channel_server/commands/__init__.py` | Package exports |
| Create | `channel_server/commands/errors.py` | `UnknownCommand`, `BadArgs`, `CommandError` |
| Create | `channel_server/commands/context.py` | `CommandContext` dataclass |
| Create | `channel_server/commands/parse.py` | `normalize_command_text`, `parse_command`, `bind_args` |
| Create | `channel_server/commands/scope.py` | `CommandScope`, `CommandEntry` |
| Create | `channel_server/commands/registry.py` | `ROOT_SCOPE` singleton, `resolve_scope` |
| Create | `channel_server/commands/dispatcher.py` | `CommandDispatcher` |
| Create | `channel_server/commands/builtin/__init__.py` | Imports all builtin modules to trigger registration |
| Create | `channel_server/commands/builtin/help.py` | `/help` command |
| Create | `channel_server/commands/builtin/sessions.py` | `/sessions` command |
| Create | `channel_server/commands/builtin/kill.py` | `/kill` command |
| Create | `channel_server/commands/builtin/spawn.py` | `/spawn` command (merges `CCAdapter._handle_spawn` + `SessionMgrHandler._handle_spawn`) |
| Create | `tests/channel_server/commands/__init__.py` | Test package |
| Create | `tests/channel_server/commands/conftest.py` | `FakeFeishuAdapter`, `FakeCCAdapter`, `make_ctx` helpers |
| Create | `tests/channel_server/commands/test_parse.py` | Unit tests for parse/normalize/bind_args |
| Create | `tests/channel_server/commands/test_scope.py` | Unit tests for CommandScope |
| Create | `tests/channel_server/commands/test_dispatcher.py` | Integration tests for dispatcher + builtins |
| Create | `tests/channel_server/commands/test_registry.py` | Tests for `resolve_scope` recursion + depth cap |
| Modify | `channel_server/adapters/feishu/adapter.py` | Call `dispatch_from_adapter` before actor pipeline |
| Modify | `channel_server/adapters/cc/adapter.py` | Translate WS actions → dispatch; delete `_handle_spawn` |
| Modify | `channel_server/core/handlers/feishu.py` | Remove command detection (lines ~50-57); clear `mode=="child"` anchor branch of `on_spawn` |
| Delete | `channel_server/core/handlers/admin.py` | Entire file (Phase 4) |
| Delete | `channel_server/core/handlers/session_mgr.py` | Entire file (Phase 4) |
| Modify | Server startup (`channel_server/app.py` or wherever `ActorRuntime(...)` is constructed — locate via grep) | Construct `CommandDispatcher`; pass into adapters; remove `system:admin` + `system:session-mgr` registration |

---

## Phase Overview

- **Phase 1 — Scaffold (Tasks 1-6):** Build `commands/` package with unit tests. No adapter changes. System behaves identically.
- **Phase 2 — Adapter Hookup (Tasks 7-9):** Dispatcher inserted at FeishuAdapter + CCAdapter entry. Only `/help` handled by new path; others fall through to legacy.
- **Phase 3 — Port Commands (Tasks 10-13):** Port `/sessions`, `/kill`, `/spawn` one at a time. Each port deletes its old path.
- **Phase 4 — Cleanup (Tasks 14-16):** Flip `fallback_on_unknown=False`, delete dead handlers, clean `on_spawn`.

---

## Task 1: Errors module

**Files:**
- Create: `channel_server/commands/errors.py`
- Create: `channel_server/commands/__init__.py`

- [ ] **Step 1: Create the package init**

Write `channel_server/commands/__init__.py`:

```python
"""Unified command registry — adapter-layer meta-operations on the actor system."""
```

- [ ] **Step 2: Create errors module**

Write `channel_server/commands/errors.py`:

```python
"""Exceptions raised by the command registry."""
from __future__ import annotations


class CommandError(Exception):
    """Base class for command-layer errors. Surfaces to the user as 'command failed'."""


class UnknownCommand(CommandError):
    """Raised by a scope when no matching command is found in the chain."""
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"unknown command: /{name}")


class BadArgs(CommandError):
    """Raised by bind_args when tokens cannot be bound to the command's schema."""
```

- [ ] **Step 3: Commit**

```bash
git add channel_server/commands/__init__.py channel_server/commands/errors.py
git commit -m "feat(commands): add errors module"
```

---

## Task 2: CommandContext

**Files:**
- Create: `channel_server/commands/context.py`

- [ ] **Step 1: Define the context dataclass**

Write `channel_server/commands/context.py`:

```python
"""CommandContext — data passed from dispatcher to command function."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from channel_server.core.actor import Message


@dataclass
class CommandContext:
    """Everything a command function needs.

    `adapter` and `runtime` are typed `Any` to avoid import cycles; in practice
    they are the adapter instance that received the command and the project's
    ActorRuntime.
    """
    source: str                         # "feishu" | "cc_mcp"
    user: str                           # feishu_user:xxx
    chat_id: str | None                 # top-level Feishu chat id
    current_actor: str | None           # actor this command originated from
    parent_actor: str | None            # current_actor's parent
    raw_msg: Message | None             # original inbound message (for reply)
    adapter: Any                        # adapter that received the command
    runtime: Any                        # ActorRuntime
```

- [ ] **Step 2: Commit**

```bash
git add channel_server/commands/context.py
git commit -m "feat(commands): add CommandContext dataclass"
```

---

## Task 3: Parse module — normalize + parse_command (test-first)

**Files:**
- Create: `tests/channel_server/commands/__init__.py`
- Create: `tests/channel_server/commands/test_parse.py`
- Create: `channel_server/commands/parse.py`

- [ ] **Step 1: Create test package**

Write `tests/channel_server/commands/__init__.py` (empty file is fine):

```python
```

- [ ] **Step 2: Write failing tests for `normalize_command_text`**

Write `tests/channel_server/commands/test_parse.py`:

```python
"""Unit tests for commands.parse."""
from __future__ import annotations

import pytest

from channel_server.commands.parse import (
    normalize_command_text,
    parse_command,
    CommandInvocation,
)


# ---------- normalize_command_text ----------

def test_normalize_empty_returns_none():
    assert normalize_command_text("") is None
    assert normalize_command_text("   \n\n  ") is None

def test_normalize_non_command_returns_none():
    assert normalize_command_text("hello world") is None

def test_normalize_simple_command():
    assert normalize_command_text("/spawn") == "/spawn"

def test_normalize_command_with_args():
    assert normalize_command_text("/spawn foo") == "/spawn foo"

def test_normalize_strips_quoted_prefix():
    # Feishu thread-reply quote: "> @林懿伦 said ...\n/spawn foo"
    text = "> @user said blah\n/spawn foo"
    assert normalize_command_text(text) == "/spawn foo"

def test_normalize_ignores_trailing_blank_lines():
    assert normalize_command_text("/spawn\n\n") == "/spawn"

def test_normalize_only_quote_no_command():
    assert normalize_command_text("> @user said blah") is None


# ---------- parse_command ----------

def test_parse_simple_command():
    inv = parse_command("/spawn")
    assert inv == CommandInvocation(name="spawn", raw_args="", tokens=[])

def test_parse_command_with_positional():
    inv = parse_command("/spawn foo")
    assert inv.name == "spawn"
    assert inv.tokens == ["foo"]

def test_parse_command_with_quoted_arg():
    inv = parse_command('/spawn "foo bar"')
    assert inv.tokens == ["foo bar"]

def test_parse_command_with_named_arg():
    inv = parse_command("/spawn foo --tag beta")
    assert inv.tokens == ["foo", "--tag", "beta"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_parse.py -v
```

Expected: ImportError / ModuleNotFoundError on `channel_server.commands.parse`.

- [ ] **Step 4: Implement `normalize_command_text` + `parse_command`**

Write `channel_server/commands/parse.py`:

```python
"""Command-text parsing: normalize → parse_command → bind_args."""
from __future__ import annotations

import shlex
from dataclasses import dataclass, fields, MISSING
from typing import Any, Type

from channel_server.commands.errors import BadArgs


# ---------- Stage 1: normalize ----------

def normalize_command_text(text: str) -> str | None:
    """Extract the command line from inbound text; return None if not a command.

    Handles Feishu thread-reply quoted prefix ("> ...\n") by taking the last
    non-empty line. Returns None for empty input or text that does not start
    with "/" on the last line.
    """
    if not text:
        return None
    last_line = next(
        (ln for ln in reversed(text.splitlines()) if ln.strip()),
        "",
    ).strip()
    if not last_line.startswith("/"):
        return None
    return last_line


# ---------- Stage 2: parse ----------

@dataclass
class CommandInvocation:
    name: str            # without leading "/"
    raw_args: str
    tokens: list[str]    # shlex.split of raw_args


def parse_command(normalized: str) -> CommandInvocation:
    """Split '/name args...' into name + tokens. Assumes input already normalized."""
    body = normalized[1:]  # strip leading "/"
    head, _, rest = body.partition(" ")
    rest = rest.strip()
    tokens = shlex.split(rest) if rest else []
    return CommandInvocation(name=head.strip(), raw_args=rest, tokens=tokens)


# ---------- Stage 3: bind_args (implemented in Task 4) ----------
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_parse.py -v
```

Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add channel_server/commands/parse.py tests/channel_server/commands/__init__.py tests/channel_server/commands/test_parse.py
git commit -m "feat(commands): normalize + parse_command with tests"
```

---

## Task 4: Parse module — bind_args

**Files:**
- Modify: `tests/channel_server/commands/test_parse.py`
- Modify: `channel_server/commands/parse.py`

- [ ] **Step 1: Add failing tests for `bind_args`**

Append to `tests/channel_server/commands/test_parse.py`:

```python
from dataclasses import dataclass

from channel_server.commands.parse import bind_args


@dataclass
class _SpawnArgs:
    tag: str = ""


@dataclass
class _KillArgs:
    name: str   # required (no default)


# ---------- bind_args ----------

def test_bind_none_schema_returns_tokens():
    assert bind_args(None, ["a", "b"]) == ["a", "b"]

def test_bind_positional():
    assert bind_args(_SpawnArgs, ["foo"]) == _SpawnArgs(tag="foo")

def test_bind_no_args_uses_defaults():
    assert bind_args(_SpawnArgs, []) == _SpawnArgs(tag="")

def test_bind_named_flag():
    assert bind_args(_SpawnArgs, ["--tag", "foo"]) == _SpawnArgs(tag="foo")

def test_bind_named_equals():
    assert bind_args(_SpawnArgs, ["--tag=foo"]) == _SpawnArgs(tag="foo")

def test_bind_missing_required_raises():
    with pytest.raises(BadArgs):
        bind_args(_KillArgs, [])

def test_bind_extra_positional_raises():
    with pytest.raises(BadArgs):
        bind_args(_SpawnArgs, ["foo", "bar"])

def test_bind_unknown_named_raises():
    with pytest.raises(BadArgs):
        bind_args(_SpawnArgs, ["--wat", "x"])
```

Also add this to the top-level imports of the test file:
```python
from channel_server.commands.errors import BadArgs
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_parse.py -v
```

Expected: 8 failures (ImportError on `bind_args`).

- [ ] **Step 3: Implement `bind_args`**

Append to `channel_server/commands/parse.py` (replacing the placeholder comment):

```python
def bind_args(schema: Type[Any] | None, tokens: list[str]) -> Any:
    """Bind tokens to a dataclass schema. Raises BadArgs on mismatch.

    Supports:
      • positional: tokens consumed in field order
      • named: "--key value" or "--key=value"
      • named args are matched first; remaining tokens fill positional
    """
    if schema is None:
        return list(tokens)

    schema_fields = fields(schema)
    known_names = {f.name for f in schema_fields}
    values: dict[str, Any] = {}
    positional_leftover: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                key, _, val = body.partition("=")
                i += 1
            else:
                key = body
                if i + 1 >= len(tokens):
                    raise BadArgs(f"missing value for --{key}")
                val = tokens[i + 1]
                i += 2
            if key not in known_names:
                raise BadArgs(f"unknown argument: --{key}")
            values[key] = val
        else:
            positional_leftover.append(tok)
            i += 1

    # Fill positional from leftovers in field order (skipping ones already set)
    for f in schema_fields:
        if f.name in values:
            continue
        if positional_leftover:
            values[f.name] = positional_leftover.pop(0)

    if positional_leftover:
        raise BadArgs(f"too many arguments: {positional_leftover}")

    # Instantiate — dataclass raises TypeError on missing required fields
    try:
        return schema(**values)
    except TypeError as e:
        raise BadArgs(str(e)) from e
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_parse.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add channel_server/commands/parse.py tests/channel_server/commands/test_parse.py
git commit -m "feat(commands): bind_args with positional + named binding"
```

---

## Task 5: CommandScope

**Files:**
- Create: `tests/channel_server/commands/test_scope.py`
- Create: `channel_server/commands/scope.py`

- [ ] **Step 1: Write failing tests**

Write `tests/channel_server/commands/test_scope.py`:

```python
"""Unit tests for CommandScope."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from channel_server.commands.errors import UnknownCommand, BadArgs
from channel_server.commands.scope import CommandScope


def _ctx_partial(**overrides):
    base = {
        "source": "test", "user": "u", "chat_id": "c",
        "current_actor": None, "parent_actor": None,
        "raw_msg": None, "adapter": MagicMock(), "runtime": MagicMock(),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_register_and_dispatch():
    scope = CommandScope()
    seen = []

    @scope.register("x")
    async def cmd(args, ctx):
        seen.append((args, ctx.source))

    await scope.dispatch("x", ["a"], _ctx_partial(source="main"))
    assert seen == [(["a"], "main")]


@pytest.mark.asyncio
async def test_unknown_command_raises():
    scope = CommandScope()
    with pytest.raises(UnknownCommand):
        await scope.dispatch("nope", [], _ctx_partial())


@pytest.mark.asyncio
async def test_parent_chain_fallback():
    root = CommandScope()
    child = CommandScope(parent=root, default_ctx={"parent_actor": "system:admin"})

    seen = []
    @root.register("x")
    async def cmd(args, ctx):
        seen.append(ctx.parent_actor)

    await child.dispatch("x", [], _ctx_partial())
    assert seen == ["system:admin"]


@pytest.mark.asyncio
async def test_child_ctx_overrides_parent():
    root = CommandScope(default_ctx={"source": "root"})
    child = CommandScope(parent=root, default_ctx={"source": "child"})

    seen = []
    @root.register("x")
    async def cmd(args, ctx):
        seen.append(ctx.source)

    await child.dispatch("x", [], {})   # supply nothing, let defaults merge
    assert seen == ["child"]


@pytest.mark.asyncio
async def test_bind_args_invoked():
    @dataclass
    class MyArgs:
        tag: str = ""

    scope = CommandScope()

    @scope.register("x", args=MyArgs)
    async def cmd(args, ctx):
        assert isinstance(args, MyArgs)
        assert args.tag == "foo"

    await scope.dispatch("x", ["foo"], _ctx_partial())


@pytest.mark.asyncio
async def test_bad_args_propagates_from_scope():
    @dataclass
    class MyArgs:
        name: str

    scope = CommandScope()

    @scope.register("x", args=MyArgs)
    async def cmd(args, ctx):
        pass

    with pytest.raises(BadArgs):
        await scope.dispatch("x", [], _ctx_partial())


@pytest.mark.asyncio
async def test_list_commands_walks_chain():
    root = CommandScope()
    child = CommandScope(parent=root)

    @root.register("a", help="A")
    async def a(args, ctx): pass

    @child.register("b", help="B")
    async def b(args, ctx): pass

    names = [n for n, _ in child.list_commands_with_help()]
    assert "a" in names and "b" in names
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_scope.py -v
```

Expected: ImportError on `channel_server.commands.scope`.

- [ ] **Step 3: Implement CommandScope**

Write `channel_server/commands/scope.py`:

```python
"""CommandScope — registry node supporting parent-chain dispatch and ctx merging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Type

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import UnknownCommand
from channel_server.commands.parse import bind_args


@dataclass
class CommandEntry:
    fn: Callable
    args_schema: Type[Any] | None
    help: str


class CommandScope:
    def __init__(
        self,
        parent: "CommandScope | None" = None,
        default_ctx: dict | None = None,
    ):
        self._commands: dict[str, CommandEntry] = {}
        self._parent = parent
        self._default_ctx = default_ctx or {}

    # ---- registration ----
    def register(self, name: str, args: Type[Any] | None = None, help: str = ""):
        def deco(fn: Callable) -> Callable:
            self._commands[name] = CommandEntry(fn=fn, args_schema=args, help=help)
            return fn
        return deco

    # ---- dispatch ----
    async def dispatch(
        self, name: str, tokens: list[str], ctx_dict: dict
    ) -> Any:
        merged = {**self._default_ctx, **ctx_dict}
        if name in self._commands:
            entry = self._commands[name]
            bound = bind_args(entry.args_schema, tokens)
            return await entry.fn(bound, CommandContext(**merged))
        if self._parent is not None:
            return await self._parent.dispatch(name, tokens, merged)
        raise UnknownCommand(name)

    # ---- help generation ----
    def list_commands_with_help(self) -> list[tuple[str, str]]:
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        scope: "CommandScope | None" = self
        while scope is not None:
            for name, entry in scope._commands.items():
                if name not in seen:
                    seen.add(name)
                    out.append((name, entry.help))
            scope = scope._parent
        return sorted(out)
```

- [ ] **Step 4: Install pytest-asyncio if not already present**

Check:
```bash
cd /Users/h2oslabs/cc-openclaw && python -c "import pytest_asyncio" 2>&1
```

If ImportError, install:
```bash
cd /Users/h2oslabs/cc-openclaw && pip install pytest-asyncio
```

Also verify `pyproject.toml` or `pytest.ini` has `asyncio_mode = auto`. If not, add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_scope.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add channel_server/commands/scope.py tests/channel_server/commands/test_scope.py pyproject.toml
git commit -m "feat(commands): CommandScope with parent-chain dispatch"
```

---

## Task 6: Registry (ROOT_SCOPE + resolve_scope) and dispatcher

**Files:**
- Create: `channel_server/commands/registry.py`
- Create: `channel_server/commands/dispatcher.py`
- Create: `tests/channel_server/commands/test_registry.py`
- Create: `tests/channel_server/commands/conftest.py`

- [ ] **Step 1: Create registry with singleton + resolve_scope**

Write `channel_server/commands/registry.py`:

```python
"""Global ROOT_SCOPE singleton + scope resolution from actor tree."""
from __future__ import annotations

from typing import Any

from channel_server.commands.errors import CommandError
from channel_server.commands.scope import CommandScope


ROOT_SCOPE = CommandScope()

_MAX_SCOPE_DEPTH = 16


def resolve_scope(
    source_actor_addr: str | None,
    runtime: Any,
    *,
    _depth: int = 0,
) -> CommandScope:
    """Build a CommandScope chain from an actor's parent chain.

    Scopes are ephemeral — constructed per dispatch. ROOT_SCOPE is returned
    when the actor has no parent or doesn't exist (dangling reference).
    """
    if source_actor_addr is None:
        return ROOT_SCOPE
    if _depth >= _MAX_SCOPE_DEPTH:
        raise CommandError(f"scope recursion exceeded {_MAX_SCOPE_DEPTH} levels")
    actor = runtime.lookup(source_actor_addr)
    if actor is None or actor.parent is None:
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

- [ ] **Step 2: Create test conftest with FakeAdapters**

Write `tests/channel_server/commands/conftest.py`:

```python
"""Shared fixtures for command tests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from channel_server.core.actor import Actor


class FakeAdapter:
    """Records reply / reply_error calls."""
    def __init__(self):
        self.replies: list[tuple[dict, str]] = []
        self.errors: list[tuple[dict, str]] = []

    async def reply(self, ctx, text):
        self.replies.append((ctx, text))

    async def reply_error(self, ctx_partial, text):
        self.errors.append((ctx_partial, text))


class FakeFeishuAdapter(FakeAdapter):
    """Records Feishu I/O calls for spawn tests."""
    def __init__(self):
        super().__init__()
        self.created_anchors: list[tuple[str, str]] = []
        self.pinned: list[str] = []
        self.tool_cards: list[tuple[str, str]] = []
        self.unpinned: list[str] = []

    async def create_thread_anchor(self, chat_id, tag):
        anchor = f"anchor_{tag}"
        self.created_anchors.append((chat_id, tag))
        return anchor

    async def pin_message(self, anchor):
        self.pinned.append(anchor)

    async def unpin_message(self, anchor):
        self.unpinned.append(anchor)

    async def create_tool_card(self, chat_id, tag):
        card = f"card_{tag}"
        self.tool_cards.append((chat_id, tag))
        return card

    def app_id_for(self, chat_id):
        return "fake_app"


class FakeCCAdapter(FakeAdapter):
    """Records CC I/O calls."""
    def __init__(self):
        super().__init__()
        self.spawned_tmux: list[tuple[str, str, str, str]] = []
        self.killed_tmux: list[str] = []

    async def spawn_tmux(self, user, session_name, tag, cc_addr):
        self.spawned_tmux.append((user, session_name, tag, cc_addr))

    async def kill_tmux(self, user, session_name):
        self.killed_tmux.append(f"{user}.{session_name}")


class FakeAdapterBundle:
    """What ctx.adapter exposes — composed from both sub-adapters."""
    def __init__(self):
        self.feishu = FakeFeishuAdapter()
        self.cc = FakeCCAdapter()
        self.replies: list[tuple[dict, str]] = []
        self.errors: list[tuple[dict, str]] = []

    async def reply(self, ctx, text):
        self.replies.append((ctx, text))

    async def reply_error(self, ctx_partial, text):
        self.errors.append((ctx_partial, text))


@pytest.fixture
def fake_adapters():
    return FakeAdapterBundle()


@pytest.fixture
def fake_runtime():
    rt = MagicMock()
    rt._actors = {}
    rt.actors = rt._actors
    rt.lookup.side_effect = lambda addr: rt._actors.get(addr)
    def _spawn(address, **kwargs):
        rt._actors[address] = Actor(address=address, handler=kwargs.get("handler", ""),
                                    tag=kwargs.get("tag", ""),
                                    parent=kwargs.get("parent"),
                                    downstream=kwargs.get("downstream", []),
                                    state=kwargs.get("state", "active"),
                                    metadata=kwargs.get("metadata", {}))
    rt.spawn.side_effect = _spawn
    def _stop(addr):
        if addr in rt._actors:
            rt._actors[addr].state = "ended"
    rt.stop.side_effect = _stop
    return rt
```

- [ ] **Step 3: Write tests for resolve_scope**

Write `tests/channel_server/commands/test_registry.py`:

```python
"""Tests for resolve_scope and ROOT_SCOPE."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE, resolve_scope
from channel_server.core.actor import Actor


def _make_rt(actors: dict[str, Actor]):
    rt = MagicMock()
    rt.lookup.side_effect = lambda addr: actors.get(addr)
    return rt


def test_resolve_none_returns_root():
    rt = _make_rt({})
    assert resolve_scope(None, rt) is ROOT_SCOPE


def test_resolve_unknown_actor_returns_root():
    rt = _make_rt({})
    assert resolve_scope("cc:ghost", rt) is ROOT_SCOPE


def test_resolve_actor_without_parent_returns_root():
    a = Actor(address="system:admin", tag="", handler="", parent=None)
    rt = _make_rt({"system:admin": a})
    assert resolve_scope("system:admin", rt) is ROOT_SCOPE


def test_resolve_two_level_chain():
    root_actor = Actor(address="system:admin", tag="", handler="", parent=None)
    child = Actor(address="cc:alice.main", tag="main", handler="cc_session",
                  parent="system:admin")
    rt = _make_rt({"system:admin": root_actor, "cc:alice.main": child})

    scope = resolve_scope("cc:alice.main", rt)
    assert scope._default_ctx["current_actor"] == "cc:alice.main"
    assert scope._default_ctx["parent_actor"] == "system:admin"


def test_resolve_recursion_depth_cap():
    # Build a 20-deep chain to exceed the 16-level cap
    actors = {}
    prev = None
    for i in range(20):
        addr = f"depth:{i}"
        actors[addr] = Actor(address=addr, tag="", handler="", parent=prev)
        prev = addr
    rt = _make_rt(actors)

    with pytest.raises(CommandError):
        resolve_scope("depth:19", rt)
```

- [ ] **Step 4: Implement dispatcher**

Write `channel_server/commands/dispatcher.py`:

```python
"""CommandDispatcher — adapter-layer entrypoint for the command registry."""
from __future__ import annotations

import logging
from typing import Any

from channel_server.commands.errors import UnknownCommand, BadArgs, CommandError
from channel_server.commands.parse import normalize_command_text, parse_command
from channel_server.commands.registry import ROOT_SCOPE, resolve_scope

log = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(self, runtime: Any, *, fallback_on_unknown: bool = False):
        self._runtime = runtime
        # During migration, True lets unregistered commands fall through to the
        # legacy actor pipeline. Flipped to False at Phase 4 cleanup.
        self._fallback_on_unknown = fallback_on_unknown

    @property
    def fallback_on_unknown(self) -> bool:
        return self._fallback_on_unknown

    def set_fallback(self, value: bool) -> None:
        self._fallback_on_unknown = value

    async def dispatch_from_adapter(
        self,
        *,
        adapter: Any,
        raw_text: str,
        source_actor: str | None,
        ctx_partial: dict,
    ) -> bool:
        """Returns True iff the command was handled here (adapter should stop).

        Returns False for non-command text OR, when fallback_on_unknown is True,
        for unregistered commands (so adapter can deliver to the legacy pipeline).
        """
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
                return False
            await adapter.reply_error(
                ctx_partial,
                f"未知命令: /{e.name}\n发送 /help 查看可用命令",
            )
        except BadArgs as e:
            await adapter.reply_error(ctx_partial, f"参数错误: {e}")
        except CommandError as e:
            await adapter.reply_error(ctx_partial, f"命令失败: {e}")
        except Exception:
            log.exception("unexpected error in command %s", invocation.name)
            await adapter.reply_error(
                ctx_partial, f"命令内部错误: /{invocation.name}"
            )
        return True
```

- [ ] **Step 5: Add dispatcher tests**

Append to `tests/channel_server/commands/test_registry.py`:

```python
from channel_server.commands.dispatcher import CommandDispatcher
from channel_server.commands.scope import CommandScope


@pytest.mark.asyncio
async def test_dispatcher_non_command_returns_false(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="hello",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "current_actor": None, "parent_actor": None,
                                        "raw_msg": None}
    )
    assert handled is False


@pytest.mark.asyncio
async def test_dispatcher_unknown_command_with_fallback_returns_false(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fallback_on_unknown=True)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/nope",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "current_actor": None, "parent_actor": None,
                                        "raw_msg": None}
    )
    assert handled is False
    assert fake_adapters.errors == []


@pytest.mark.asyncio
async def test_dispatcher_unknown_command_no_fallback_replies_error(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fallback_on_unknown=False)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/nope",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "current_actor": None, "parent_actor": None,
                                        "raw_msg": None}
    )
    assert handled is True
    assert len(fake_adapters.errors) == 1
    assert "未知命令" in fake_adapters.errors[0][1]
```

- [ ] **Step 6: Run all commands tests**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/ -v
```

Expected: all green, including the new registry + dispatcher tests.

- [ ] **Step 7: Commit**

```bash
git add channel_server/commands/registry.py channel_server/commands/dispatcher.py tests/channel_server/commands/conftest.py tests/channel_server/commands/test_registry.py
git commit -m "feat(commands): ROOT_SCOPE + resolve_scope + CommandDispatcher"
```

---

## Task 7: /help builtin command

**Files:**
- Create: `channel_server/commands/builtin/__init__.py`
- Create: `channel_server/commands/builtin/help.py`
- Modify: `tests/channel_server/commands/test_dispatcher.py` (new test)

- [ ] **Step 1: Write builtin init**

Write `channel_server/commands/builtin/__init__.py`:

```python
"""Builtin commands — importing this package triggers their registration in ROOT_SCOPE."""
from channel_server.commands.builtin import help as _help  # noqa: F401
```

- [ ] **Step 2: Write failing test**

Write `tests/channel_server/commands/test_dispatcher.py`:

```python
"""Integration tests for CommandDispatcher + builtin commands."""
from __future__ import annotations

import pytest

from channel_server.commands.dispatcher import CommandDispatcher

# Import builtins so they register on ROOT_SCOPE
import channel_server.commands.builtin  # noqa: F401


def _ctx_partial(**overrides):
    base = {
        "source": "feishu", "user": "u", "chat_id": "c",
        "current_actor": None, "parent_actor": None, "raw_msg": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_help_replies_with_command_list(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/help",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    assert handled is True
    assert len(fake_adapters.replies) == 1
    text = fake_adapters.replies[0][1]
    assert "/help" in text
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py -v
```

Expected: UnknownCommand (help not registered yet).

- [ ] **Step 4: Implement /help**

Write `channel_server/commands/builtin/help.py`:

```python
"""/help — list available commands with help text."""
from __future__ import annotations

from channel_server.commands.context import CommandContext
from channel_server.commands.registry import ROOT_SCOPE, resolve_scope


@ROOT_SCOPE.register("help", help="显示可用命令")
async def help_cmd(args, ctx: CommandContext):
    scope = resolve_scope(ctx.current_actor, ctx.runtime)
    lines = ["可用命令:"]
    for name, help_text in scope.list_commands_with_help():
        if help_text:
            lines.append(f"/{name} — {help_text}")
        else:
            lines.append(f"/{name}")
    text = "\n".join(lines)
    await ctx.adapter.reply(ctx, text)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add channel_server/commands/builtin/ tests/channel_server/commands/test_dispatcher.py
git commit -m "feat(commands): /help builtin with auto-generated list"
```

---

## Task 8: Wire dispatcher into FeishuAdapter (Phase 2)

**Files:**
- Modify: `channel_server/adapters/feishu/adapter.py`
- Modify: server startup / DI (exact file TBD — find by reading adapter init)

- [ ] **Step 1: Locate adapter init + startup wiring**

Read these files to find where FeishuAdapter is instantiated and where runtime is created:

```bash
cd /Users/h2oslabs/cc-openclaw && grep -rn "FeishuAdapter(" channel_server/ --include="*.py"
cd /Users/h2oslabs/cc-openclaw && grep -rn "ActorRuntime(" channel_server/ --include="*.py"
```

- [ ] **Step 2: Instantiate dispatcher at startup**

In the startup file (likely `channel_server/app.py` or similar), after the runtime is created, add:

```python
from channel_server.commands.dispatcher import CommandDispatcher
import channel_server.commands.builtin  # trigger registration

# After `runtime = ActorRuntime(...)`:
command_dispatcher = CommandDispatcher(runtime, fallback_on_unknown=True)

# Pass into adapter constructors:
feishu_adapter = FeishuAdapter(..., dispatcher=command_dispatcher)
cc_adapter = CCAdapter(..., dispatcher=command_dispatcher)
```

- [ ] **Step 3: Accept dispatcher in FeishuAdapter**

In `channel_server/adapters/feishu/adapter.py`, add `dispatcher` to `__init__` and store as `self._dispatcher`.

Find the inbound webhook handler (the function that receives incoming Feishu messages; look for where it parses `event.message.content` and constructs a `Message` / calls `runtime.send`). At the top of that handler, before it builds/delivers the Message, insert:

```python
# New: give the command registry first crack at this text
if self._dispatcher is not None:
    chat_id = event_chat_id                         # the existing variable
    source_actor = self._identify_source_actor(event)  # implemented next step
    handled = await self._dispatcher.dispatch_from_adapter(
        adapter=self,
        raw_text=event_text,                         # the already-extracted text
        source_actor=source_actor,
        ctx_partial={
            "source": "feishu",
            "user": f"feishu_user:{event_user_id}",
            "chat_id": chat_id,
            "current_actor": source_actor,
            "parent_actor": None,   # filled by resolve_scope if needed
            "raw_msg": None,        # constructed by legacy path only
        },
    )
    if handled:
        return
# ...existing code that delivers to runtime...
```

- [ ] **Step 4: Implement `_identify_source_actor`**

Add this method to `FeishuAdapter`:

```python
def _identify_source_actor(self, event) -> str | None:
    """Return the actor address for the origin of this inbound event.

    - Main chat (no thread context) → None, dispatcher uses ROOT_SCOPE.
    - Thread reply → find the feishu_inbound actor whose transport config
      matches this chat + root_id.
    """
    root_id = getattr(event, "root_id", None) or getattr(event.message, "root_id", None)
    if not root_id:
        return None
    chat_id = event.chat_id
    for addr, actor in self._runtime.actors.items():
        if not actor.address.startswith("feishu:"):
            continue
        cfg = actor.transport.config if actor.transport else {}
        if cfg.get("chat_id") == chat_id and cfg.get("root_id") == root_id:
            return actor.address
    return None
```

Note: if FeishuAdapter does not already hold a reference to runtime, add `runtime` to its constructor.

- [ ] **Step 5: Implement `reply` and `reply_error` on FeishuAdapter**

Add:

```python
async def reply(self, ctx, text: str) -> None:
    """Send a plain-text reply to the same chat/thread where the command came from."""
    # Use the existing outbound send API; pass chat_id and optional root_id/anchor.
    # (Exact call depends on the adapter's existing outbound method — search for
    # `create_reply` or `send_text` in this file for the pattern to reuse.)
    await self._send_text(ctx.chat_id, text, root_id=getattr(ctx, "thread_root_id", None))


async def reply_error(self, ctx_partial: dict, text: str) -> None:
    """Same as reply but callable with the partial dict used before ctx is built."""
    await self._send_text(ctx_partial.get("chat_id"), text,
                          root_id=ctx_partial.get("thread_root_id"))
```

If `_send_text` doesn't exist, find the existing outbound-text method and wrap it.

- [ ] **Step 6: Run end-to-end smoke test manually**

Start the server and send `/help` in main Feishu chat and in an existing thread. Both should receive the auto-generated help list.

Also send `/spawn foo` — this should NOT be served by the new dispatcher (returns False because `fallback_on_unknown=True`), so the legacy `SessionMgrHandler` path still runs and a session is created as before.

- [ ] **Step 7: Commit**

```bash
git add channel_server/adapters/feishu/adapter.py channel_server/app.py  # or wherever startup lives
git commit -m "feat(commands): wire CommandDispatcher into FeishuAdapter"
```

---

## Task 9: Wire dispatcher into CCAdapter (Phase 2)

**Files:**
- Modify: `channel_server/adapters/cc/adapter.py`

- [ ] **Step 1: Accept dispatcher in CCAdapter**

Add `dispatcher` kwarg to `CCAdapter.__init__`; store as `self._dispatcher`.

- [ ] **Step 2: Define the WS→command translation map and helper**

At the top of `adapters/cc/adapter.py`:

```python
import shlex

# Meta-operations on the actor system that route through the command registry.
# Other WS actions (reply, react, send_file, send_summary, update_title, forward,
# tool_notify, ...) continue to flow through the existing actor-message path.
WS_ACTION_TO_COMMAND: dict[str, str] = {
    "spawn_session": "spawn",
    "kill_session":  "kill",
    "list_sessions": "sessions",
}


def ws_args_to_text(cmd_name: str, payload: dict) -> str:
    """Serialize a WS action payload into shell-tokenizable command text."""
    if cmd_name == "spawn":
        name = payload.get("session_name") or payload.get("name", "")
        tag = payload.get("tag", "")
        parts = []
        if name:
            parts.append(shlex.quote(name))
        if tag:
            parts.extend(["--tag", shlex.quote(tag)])
        return " ".join(parts)
    if cmd_name == "kill":
        name = payload.get("session_name") or payload.get("name", "")
        return shlex.quote(name) if name else ""
    if cmd_name == "sessions":
        return ""
    return ""
```

- [ ] **Step 3: Insert dispatcher call in WS action handler**

Find the location in `adapters/cc/adapter.py` that dispatches WS actions (around lines 111-116 — `if action == "spawn_session":` etc.). Insert at the top of that branch tree:

```python
if action in WS_ACTION_TO_COMMAND and self._dispatcher is not None:
    cmd_name = WS_ACTION_TO_COMMAND[action]
    raw_text = f"/{cmd_name} {ws_args_to_text(cmd_name, msg)}".strip()
    source_actor = self._ws_to_actor(ws)
    ctx_partial = {
        "source": "cc_mcp",
        "user": self._ws_user(ws),
        "chat_id": self._ws_chat(ws),
        "current_actor": source_actor,
        "parent_actor": None,
        "raw_msg": None,
    }
    handled = await self._dispatcher.dispatch_from_adapter(
        adapter=self,
        raw_text=raw_text,
        source_actor=source_actor,
        ctx_partial=ctx_partial,
    )
    if handled:
        return
```

- [ ] **Step 4: Implement `_ws_to_actor`, `_ws_user`, `_ws_chat`**

The CC adapter already tracks which WS connection belongs to which session. Find the dict (likely `self._sessions`, `self._ws_to_session`, etc.) and add small helpers:

```python
def _ws_to_actor(self, ws) -> str | None:
    """Return the cc:* actor address bound to this WS connection, or None."""
    session = self._ws_to_session.get(ws)            # adapt to actual attr name
    if not session:
        return None
    return f"cc:{session.user}.{session.name}"

def _ws_user(self, ws) -> str:
    session = self._ws_to_session.get(ws)
    return f"feishu_user:{session.user}" if session else ""

def _ws_chat(self, ws) -> str | None:
    session = self._ws_to_session.get(ws)
    return session.chat_id if session else None
```

- [ ] **Step 5: Add `reply` / `reply_error` on CCAdapter**

For `/help` invoked from a CC MCP call, the reply must land in the Feishu chat associated with that session. Delegate to the Feishu adapter:

```python
async def reply(self, ctx, text: str) -> None:
    # Reply goes back to the Feishu chat the session is bound to.
    await self._feishu_adapter.reply(ctx, text)

async def reply_error(self, ctx_partial: dict, text: str) -> None:
    await self._feishu_adapter.reply_error(ctx_partial, text)
```

If `CCAdapter` doesn't already hold `self._feishu_adapter`, add it via constructor wiring at startup.

- [ ] **Step 6: Smoke test**

From inside a running CC session, invoke the `list_sessions` MCP tool. The response should come via the unified path (`/sessions` is not yet ported, so with `fallback_on_unknown=True` the dispatcher returns False and the legacy `_handle_list_sessions` runs). Verify /help from a CC session replies into the thread.

- [ ] **Step 7: Commit**

```bash
git add channel_server/adapters/cc/adapter.py
git commit -m "feat(commands): wire CommandDispatcher into CCAdapter via WS action translation"
```

---

## Task 10: Port /sessions

**Files:**
- Create: `channel_server/commands/builtin/sessions.py`
- Modify: `channel_server/commands/builtin/__init__.py`
- Modify: `channel_server/core/handlers/session_mgr.py` (remove `_handle_sessions`)
- Modify: `channel_server/core/handlers/admin.py` (remove `/sessions` from routing)
- Modify: `tests/channel_server/commands/test_dispatcher.py`

- [ ] **Step 1: Write failing test**

Append to `tests/channel_server/commands/test_dispatcher.py`:

```python
from channel_server.core.actor import Actor


@pytest.mark.asyncio
async def test_sessions_lists_user_cc_actors(fake_adapters, fake_runtime):
    # Seed fake runtime with two cc actors for alice
    fake_runtime._actors["cc:alice.main"] = Actor(
        address="cc:alice.main", tag="main", handler="cc_session",
        parent="system:admin", state="active",
    )
    fake_runtime._actors["cc:alice.sub"] = Actor(
        address="cc:alice.sub", tag="sub", handler="cc_session",
        parent="cc:alice.main", state="active",
    )

    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/sessions",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice"),
    )
    assert handled is True
    reply = fake_adapters.replies[-1][1]
    assert "main" in reply and "sub" in reply
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py::test_sessions_lists_user_cc_actors -v
```

Expected: UnknownCommand.

- [ ] **Step 3: Implement /sessions**

Write `channel_server/commands/builtin/sessions.py`:

```python
"""/sessions — list active CC sessions owned by the invoker."""
from __future__ import annotations

from channel_server.commands.context import CommandContext
from channel_server.commands.registry import ROOT_SCOPE


@ROOT_SCOPE.register("sessions", help="列出活跃 sessions")
async def sessions_cmd(args, ctx: CommandContext):
    user_prefix = f"cc:{ctx.user.removeprefix('feishu_user:')}."
    rows = []
    for addr, actor in ctx.runtime.actors.items():
        if actor.address.startswith(user_prefix) and actor.state != "ended":
            rows.append(f"• {actor.tag} ({actor.address}) — {actor.state}")
    if not rows:
        await ctx.adapter.reply(ctx, "当前没有活跃 sessions")
        return
    await ctx.adapter.reply(ctx, "活跃 sessions:\n" + "\n".join(rows))
```

- [ ] **Step 4: Register in builtin init**

Edit `channel_server/commands/builtin/__init__.py`:

```python
from channel_server.commands.builtin import help as _help       # noqa: F401
from channel_server.commands.builtin import sessions as _sessions  # noqa: F401
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py -v
```

Expected: all pass.

- [ ] **Step 6: Remove `/sessions` from legacy routes**

In `channel_server/core/handlers/admin.py`, change `SESSION_COMMANDS` tuple to exclude `/sessions`:

```python
SESSION_COMMANDS = ("/spawn", "/kill")  # /sessions now handled by new registry
```

In `channel_server/core/handlers/session_mgr.py`, delete the `_handle_sessions` method and its dispatch branch in `handle()`. Add a one-line comment noting the move.

Verify by grepping:
```bash
cd /Users/h2oslabs/cc-openclaw && grep -n "_handle_sessions\|list_sessions" channel_server/core/handlers/
```

- [ ] **Step 7: Manual smoke**

Send `/sessions` in: (a) main Feishu chat, (b) an existing Feishu thread, (c) via CC MCP tool. All three should produce the same list.

- [ ] **Step 8: Commit**

```bash
git add channel_server/commands/builtin/sessions.py channel_server/commands/builtin/__init__.py channel_server/core/handlers/admin.py channel_server/core/handlers/session_mgr.py tests/channel_server/commands/test_dispatcher.py
git commit -m "feat(commands): port /sessions to new registry; remove from legacy"
```

---

## Task 11: Port /kill

**Files:**
- Create: `channel_server/commands/builtin/kill.py`
- Modify: `channel_server/commands/builtin/__init__.py`
- Modify: `channel_server/core/handlers/session_mgr.py` (remove `_handle_kill`)
- Modify: `channel_server/core/handlers/admin.py` (remove `/kill` from routing)
- Modify: `tests/channel_server/commands/test_dispatcher.py`

- [ ] **Step 1: Write failing test**

Append to `tests/channel_server/commands/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_kill_stops_actor_and_tmux(fake_adapters, fake_runtime):
    fake_runtime._actors["cc:alice.foo"] = Actor(
        address="cc:alice.foo", tag="foo", handler="cc_session",
        parent="system:admin", state="active",
        downstream=["feishu:fake_app:oc_chat:thread:foo"],
    )
    fake_runtime._actors["feishu:fake_app:oc_chat:thread:foo"] = Actor(
        address="feishu:fake_app:oc_chat:thread:foo",
        tag="foo", handler="feishu_inbound",
        parent="cc:alice.foo",
        metadata={"root_id": "anchor_foo"},
    )

    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/kill foo",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice", chat_id="oc_chat"),
    )
    assert handled is True
    stopped_addrs = [call.args[0] for call in fake_runtime.stop.call_args_list]
    assert "cc:alice.foo" in stopped_addrs
    assert "feishu:fake_app:oc_chat:thread:foo" in stopped_addrs
    assert fake_adapters.feishu.unpinned == ["anchor_foo"]
    assert fake_adapters.cc.killed_tmux == ["alice.foo"]


@pytest.mark.asyncio
async def test_kill_missing_name_bad_args(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/kill",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice"),
    )
    assert handled is True
    assert len(fake_adapters.errors) == 1
    assert "参数错误" in fake_adapters.errors[0][1]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py::test_kill_stops_actor_and_tmux -v
```

Expected: UnknownCommand.

- [ ] **Step 3: Implement /kill**

Write `channel_server/commands/builtin/kill.py`:

```python
"""/kill <name> — stop a CC session: kill tmux, unpin anchor, stop actors."""
from __future__ import annotations

from dataclasses import dataclass

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE


@dataclass
class KillArgs:
    name: str


@ROOT_SCOPE.register("kill", args=KillArgs, help="/kill <name> — end a session")
async def kill_cmd(args: KillArgs, ctx: CommandContext):
    user = ctx.user.removeprefix("feishu_user:")
    cc_addr = f"cc:{user}.{args.name}"

    actor = ctx.runtime.lookup(cc_addr)
    if actor is None:
        raise CommandError(f"session '{args.name}' not found")

    # Find the feishu thread actor in downstream to recover root_id (anchor)
    thread_addr = None
    anchor = None
    for d_addr in actor.downstream:
        if d_addr.startswith("feishu:"):
            thread_addr = d_addr
            thread_actor = ctx.runtime.lookup(d_addr)
            if thread_actor is not None:
                anchor = (
                    thread_actor.metadata.get("root_id")
                    or (thread_actor.transport.config.get("root_id")
                        if thread_actor.transport else None)
                )
            break

    # Tear down I/O
    await ctx.adapter.cc.kill_tmux(user, args.name)
    if anchor:
        await ctx.adapter.feishu.unpin_message(anchor)

    # Stop actors
    ctx.runtime.stop(cc_addr)
    if thread_addr:
        ctx.runtime.stop(thread_addr)

    await ctx.adapter.reply(ctx, f"Session {args.name} ended")
```

- [ ] **Step 4: Register in builtin init**

Edit `channel_server/commands/builtin/__init__.py`:

```python
from channel_server.commands.builtin import help as _help          # noqa: F401
from channel_server.commands.builtin import sessions as _sessions  # noqa: F401
from channel_server.commands.builtin import kill as _kill          # noqa: F401
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/ -v
```

Expected: all pass.

- [ ] **Step 6: Remove `/kill` from legacy**

- `channel_server/core/handlers/admin.py`: `SESSION_COMMANDS = ("/spawn",)` (only spawn left).
- `channel_server/core/handlers/session_mgr.py`: delete `_handle_kill` + its dispatch branch.

- [ ] **Step 7: Manual smoke**

- `/spawn foo` (still legacy) — session created
- `/kill foo` (new) — session ends, anchor unpinned, reply says "Session foo ended"
- Same from thread; same from CC MCP

- [ ] **Step 8: Commit**

```bash
git add channel_server/commands/builtin/ channel_server/core/handlers/admin.py channel_server/core/handlers/session_mgr.py tests/channel_server/commands/test_dispatcher.py
git commit -m "feat(commands): port /kill to new registry; remove from legacy"
```

---

## Task 12: Port /spawn — merge CCAdapter + SessionMgr paths

This is the biggest task. The new `spawn` command combines the I/O sequence from `CCAdapter._handle_spawn` (anchor, pin, tool card, tmux) with the actor-registration logic from `SessionMgrHandler._handle_spawn`. **This is where Goal #4 (Feishu-spawn now starts tmux, child sessions inherit parent) is closed out.**

**Files:**
- Create: `channel_server/commands/builtin/spawn.py`
- Modify: `channel_server/commands/builtin/__init__.py`
- Modify: `channel_server/adapters/cc/adapter.py` (delete `_handle_spawn` lines 361-436)
- Modify: `channel_server/core/handlers/session_mgr.py` (delete `_handle_spawn`)
- Modify: `channel_server/core/handlers/admin.py` (delete `SESSION_COMMANDS`)
- Modify: `tests/channel_server/commands/test_dispatcher.py`

- [ ] **Step 1: Re-read the reference implementations**

```bash
cd /Users/h2oslabs/cc-openclaw && sed -n '361,436p' channel_server/adapters/cc/adapter.py
cd /Users/h2oslabs/cc-openclaw && sed -n '83,154p' channel_server/core/handlers/session_mgr.py
```

Take notes on:
- The order of Feishu I/O (anchor → pin → tool_card)
- The exact address format for the thread actor (`feishu:{app_id}:{chat_id}:thread:{session_name}`)
- Metadata keys expected by FeishuInboundHandler (`chat_id`, `tag`, `mode`, `root_id`)
- Transport type (`feishu_thread`) and config keys (`chat_id`, `root_id`)
- The CC actor fields (`tag`, `parent`, `downstream`, `state`, `metadata`)
- What `spawn_cc_process` / `spawn_tmux` expects as arguments

- [ ] **Step 2: Write failing test — main chat spawn**

Append to `tests/channel_server/commands/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_spawn_main_chat_full_io_sequence(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn foo",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice", chat_id="oc_chat"),
    )
    assert handled is True

    # I/O order: anchor, pin, tool card, tmux
    assert fake_adapters.feishu.created_anchors == [("oc_chat", "foo")]
    assert fake_adapters.feishu.pinned == ["anchor_foo"]
    assert fake_adapters.feishu.tool_cards == [("oc_chat", "foo")]
    assert fake_adapters.cc.spawned_tmux == [
        ("alice", "foo", "foo", "cc:alice.foo")
    ]

    # Actor registration — active, not suspended (Goal #4 fix)
    cc_actor = fake_runtime._actors["cc:alice.foo"]
    assert cc_actor.state == "active"
    # Address scheme preserved from legacy
    assert "feishu:fake_app:oc_chat:thread:foo" in fake_runtime._actors


@pytest.mark.asyncio
async def test_spawn_from_cc_session_sets_parent_actor(fake_adapters, fake_runtime):
    # Seed an existing main session
    fake_runtime._actors["cc:alice.main"] = Actor(
        address="cc:alice.main", tag="main", handler="cc_session",
        parent="system:admin", state="active",
    )
    fake_runtime._actors["system:admin"] = Actor(
        address="system:admin", tag="admin", handler="admin", parent=None,
    )

    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn sub",
        source_actor="cc:alice.main",
        ctx_partial=_ctx_partial(user="feishu_user:alice", chat_id="oc_chat"),
    )
    assert handled is True

    cc_actor = fake_runtime._actors["cc:alice.sub"]
    # New actor's parent is the session that invoked /spawn
    assert cc_actor.parent == "cc:alice.main"


@pytest.mark.asyncio
async def test_spawn_with_quoted_prefix(fake_adapters, fake_runtime):
    """Goal #3 regression: Feishu thread reply auto-prepends quoted content."""
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters,
        raw_text="> @林懿伦 在上面说了些什么\n/spawn quotetest",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice", chat_id="oc_chat"),
    )
    assert handled is True
    assert "cc:alice.quotetest" in fake_runtime._actors
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_dispatcher.py -k spawn -v
```

Expected: UnknownCommand.

- [ ] **Step 4: Implement /spawn**

Write `channel_server/commands/builtin/spawn.py`:

```python
"""/spawn [name] — create a CC session (main or child)."""
from __future__ import annotations

from dataclasses import dataclass

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE
from channel_server.core.actor import Transport


_MAX_CHILDREN = 5


@dataclass
class SpawnArgs:
    name: str = ""


@ROOT_SCOPE.register("spawn", args=SpawnArgs,
                     help="/spawn [name] — create a session")
async def spawn_cmd(args: SpawnArgs, ctx: CommandContext):
    if not args.name:
        raise CommandError("usage: /spawn <name>")

    user = ctx.user.removeprefix("feishu_user:")
    session_name = args.name
    tag = session_name   # current convention: tag == session name

    # Enforce child limit under the invoker's actor
    if ctx.current_actor:
        children = sum(
            1 for a in ctx.runtime.actors.values()
            if a.parent == ctx.current_actor and a.state not in ("ended",)
        )
        if children >= _MAX_CHILDREN:
            raise CommandError(f"Max sessions ({_MAX_CHILDREN}) reached")

    cc_addr = f"cc:{user}.{session_name}"
    if cc_addr in ctx.runtime.actors and ctx.runtime.actors[cc_addr].state != "ended":
        raise CommandError(f"session '{session_name}' already exists")

    # ---- I/O phase (order preserved from CCAdapter._handle_spawn) ----
    anchor = await ctx.adapter.feishu.create_thread_anchor(ctx.chat_id, tag)
    await ctx.adapter.feishu.pin_message(anchor)
    await ctx.adapter.feishu.create_tool_card(ctx.chat_id, tag)

    # ---- Actor registration (preserving legacy address scheme) ----
    app_id = ctx.adapter.feishu.app_id_for(ctx.chat_id)
    thread_addr = f"feishu:{app_id}:{ctx.chat_id}:thread:{session_name}"

    ctx.runtime.spawn(
        address=thread_addr,
        handler="feishu_inbound",
        tag=tag,
        parent=ctx.current_actor,
        downstream=[cc_addr],
        metadata={
            "chat_id": ctx.chat_id,
            "tag": tag,
            "mode": "child",
            "root_id": anchor,
        },
        transport=Transport(
            type="feishu_thread",
            config={"chat_id": ctx.chat_id, "root_id": anchor},
        ),
    )
    ctx.runtime.spawn(
        address=cc_addr,
        handler="cc_session",
        tag=tag,
        parent=ctx.current_actor or f"cc:{user}.root",
        downstream=[thread_addr],
        state="active",   # ← active from the start; Goal #4 fix
        metadata={"chat_id": ctx.chat_id, "tag": tag},
    )

    # ---- Tmux — full I/O ----
    await ctx.adapter.cc.spawn_tmux(user, session_name, tag, cc_addr)

    await ctx.adapter.reply(ctx, f"Session {session_name} started")
```

- [ ] **Step 5: Register in builtin init**

Edit `channel_server/commands/builtin/__init__.py`:

```python
from channel_server.commands.builtin import help as _help          # noqa: F401
from channel_server.commands.builtin import sessions as _sessions  # noqa: F401
from channel_server.commands.builtin import kill as _kill          # noqa: F401
from channel_server.commands.builtin import spawn as _spawn        # noqa: F401
```

- [ ] **Step 6: Run tests**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/ -v
```

Expected: all pass.

- [ ] **Step 7: Remove `/spawn` from legacy**

Delete from `channel_server/adapters/cc/adapter.py`: the entire `_handle_spawn` method (lines 361-436) and the WS action branch that used to call it (the `elif action == "spawn_session"` block). Leave `/spawn` still reachable through the new dispatcher (already wired in Task 9).

Delete from `channel_server/core/handlers/session_mgr.py`: `_handle_spawn` method and its dispatch branch.

Delete from `channel_server/core/handlers/admin.py`: the `SESSION_COMMANDS` tuple and the branch that forwarded them.

- [ ] **Step 8: Full manual smoke**

Run through the full scenario:
1. Main chat `/spawn main` — session created, anchor pinned, tool card created, tmux running, reply "Session main started"
2. In the new thread, send `/sessions` — should list "main"
3. Inside the CC session (Claude running), invoke `spawn_session name=sub` via MCP — new child session created under `main`
4. Back in main chat, `/sessions` — should list both `main` and `sub`
5. `/kill sub` — sub ends; `/sessions` shows only `main`
6. `/kill main` — all ends

- [ ] **Step 9: Commit**

```bash
git add channel_server/commands/builtin/ channel_server/adapters/cc/adapter.py channel_server/core/handlers/session_mgr.py channel_server/core/handlers/admin.py tests/channel_server/commands/test_dispatcher.py
git commit -m "feat(commands): port /spawn with full I/O; fix Feishu-spawn tmux asymmetry"
```

---

## Task 13: Regression tests pinning the four goals

**Files:**
- Create: `tests/channel_server/commands/test_regression.py`

- [ ] **Step 1: Write regression tests**

Write `tests/channel_server/commands/test_regression.py`:

```python
"""Regression tests pinning the four stated goals of the unified command registry.

See: docs/superpowers/specs/2026-04-17-unified-command-registry-design.md
"""
from __future__ import annotations

import pytest

import channel_server.commands.builtin  # register builtins
from channel_server.commands.dispatcher import CommandDispatcher
from channel_server.commands.registry import ROOT_SCOPE
from channel_server.core.actor import Actor


def _ctx_partial(**overrides):
    base = {"source": "feishu", "user": "feishu_user:alice", "chat_id": "oc_chat",
            "current_actor": None, "parent_actor": None, "raw_msg": None}
    base.update(overrides)
    return base


# Goal #1 — adding a command is one file
@pytest.mark.asyncio
async def test_goal_1_adding_command_is_one_file(fake_adapters, fake_runtime):
    """Register a new command inline, confirm it dispatches and shows in /help."""
    called = []

    @ROOT_SCOPE.register("ping_regression", help="ping test")
    async def ping(args, ctx):
        called.append(True)
        await ctx.adapter.reply(ctx, "pong")

    try:
        d = CommandDispatcher(fake_runtime)
        await d.dispatch_from_adapter(
            adapter=fake_adapters, raw_text="/ping_regression",
            source_actor=None, ctx_partial=_ctx_partial(),
        )
        assert called == [True]
        assert fake_adapters.replies[-1][1] == "pong"

        # Auto-appears in /help
        await d.dispatch_from_adapter(
            adapter=fake_adapters, raw_text="/help",
            source_actor=None, ctx_partial=_ctx_partial(),
        )
        assert "ping_regression" in fake_adapters.replies[-1][1]
    finally:
        # Clean up so it doesn't leak into other tests
        ROOT_SCOPE._commands.pop("ping_regression", None)


# Goal #2 — I/O lives in the command, order preserved
@pytest.mark.asyncio
async def test_goal_2_spawn_io_order_anchor_before_tmux(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn iotest",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    # Anchor must be created before tmux is spawned
    assert fake_adapters.feishu.created_anchors
    assert fake_adapters.cc.spawned_tmux
    # (Call ordering is captured by list append order; if they share a clock
    # we could check timestamps, but append-order suffices for the regression.)


# Goal #3 — quoted prefix doesn't break matching
@pytest.mark.asyncio
async def test_goal_3_quoted_prefix_matches(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters,
        raw_text="> @林懿伦 earlier message\n/spawn quoted",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    assert handled is True
    assert "cc:alice.quoted" in fake_runtime._actors


# Goal #4a — Feishu spawn now starts tmux (old bug)
@pytest.mark.asyncio
async def test_goal_4a_feishu_spawn_starts_tmux(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn fromfeishu",
        source_actor=None, ctx_partial=_ctx_partial(source="feishu"),
    )
    assert fake_adapters.cc.spawned_tmux, \
        "Feishu-entry spawn must start tmux (was suspended in old code)"


# Goal #4b — CC MCP spawn and Feishu spawn produce equivalent actor state
@pytest.mark.asyncio
async def test_goal_4b_entry_symmetry(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime)

    # Path A: Feishu entry
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn via_feishu",
        source_actor=None, ctx_partial=_ctx_partial(source="feishu"),
    )

    # Seed parent for CC path
    fake_runtime._actors["system:admin"] = Actor(
        address="system:admin", tag="admin", handler="admin", parent=None,
    )

    # Path B: CC MCP entry with source_actor=None (acts like top-level)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn via_cc",
        source_actor=None, ctx_partial=_ctx_partial(source="cc_mcp"),
    )

    a_feishu = fake_runtime._actors["cc:alice.via_feishu"]
    a_cc = fake_runtime._actors["cc:alice.via_cc"]

    # Modulo tag/name differences, actor shape should match
    assert a_feishu.state == a_cc.state == "active"
    assert a_feishu.handler == a_cc.handler == "cc_session"
    assert len(a_feishu.downstream) == len(a_cc.downstream)
```

- [ ] **Step 2: Run regression tests**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/channel_server/commands/test_regression.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/channel_server/commands/test_regression.py
git commit -m "test(commands): regression tests pinning four stated goals"
```

---

## Task 14: Flip fallback_on_unknown; delete legacy handlers

**Files:**
- Modify: startup file (where `CommandDispatcher` is constructed)
- Delete: `channel_server/core/handlers/admin.py`
- Delete: `channel_server/core/handlers/session_mgr.py`
- Modify: `channel_server/core/handler.py` (handler registry)
- Modify: `channel_server/core/handlers/__init__.py`
- Modify: startup file (remove `system:admin` + `system:session-mgr` actor spawns)

- [ ] **Step 1: Flip the flag**

In the startup file, change:
```python
command_dispatcher = CommandDispatcher(runtime, fallback_on_unknown=True)
```
to:
```python
command_dispatcher = CommandDispatcher(runtime, fallback_on_unknown=False)
```

Now any unknown slash command replies with "未知命令: /xxx" via the new path.

- [ ] **Step 2: Delete handler files**

```bash
cd /Users/h2oslabs/cc-openclaw && git rm channel_server/core/handlers/admin.py channel_server/core/handlers/session_mgr.py
```

- [ ] **Step 3: Remove handler registry entries**

In `channel_server/core/handler.py`, find the `_HANDLER_REGISTRY` dict (or equivalent) and remove `"admin"` and `"session_mgr"` entries.

In `channel_server/core/handlers/__init__.py`, remove `AdminHandler` and `SessionMgrHandler` imports/exports.

- [ ] **Step 4: Remove startup actor spawns**

In the startup file, find where `system:admin` and `system:session-mgr` actors are spawned. Delete those `runtime.spawn(...)` calls.

The existing code used `system:admin` as the main-chat forwarder. Replace with a `ForwardAllHandler`-backed actor of a neutral name if the forwarding behavior (system notifications → downstream) is still needed. Check by searching:

```bash
cd /Users/h2oslabs/cc-openclaw && grep -rn "system:admin" channel_server/ --include="*.py"
```

If any non-command logic still references `system:admin`, rename to a `system:main_forwarder` actor registered with `ForwardAllHandler` before this step passes.

- [ ] **Step 5: Run the full test suite**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v
```

Expected: all tests pass, including any legacy tests that may need fixture updates if they referenced `AdminHandler` / `SessionMgrHandler` directly. Fix fixtures as you go (some tests in `tests/channel_server/core/handlers/test_session_mgr.py` will need deletion).

- [ ] **Step 6: Manual smoke (full scenario)**

1. Restart the server fresh.
2. Main chat `/help` — shows 4 commands (help, spawn, kill, sessions).
3. Main chat `/nope` — "未知命令" reply (fallback_on_unknown=False now).
4. `/spawn main` → thread → `/sessions` → MCP spawn `sub` → `/sessions` (both) → `/kill sub` → `/kill main`.
5. Server restart — actors recover; `/sessions` shows correct state.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(commands): flip fallback off; delete AdminHandler + SessionMgrHandler"
```

---

## Task 15: Remove command detection from FeishuInboundHandler

**Files:**
- Modify: `channel_server/core/handlers/feishu.py`

- [ ] **Step 1: Remove the command-detection branch**

In `channel_server/core/handlers/feishu.py`, the inbound handler has a block around lines 50-57 that detects commands and forwards to `system:admin`. Delete those lines; the handler's remaining job is to forward inbound text to downstream actors (which `FeishuAdapter` now does only for non-commands, since commands are intercepted before the Message pipeline).

Before:
```python
text = msg.payload.get("text", "").strip()
command_text = text.split("\n")[-1].strip() if "\n" in text else text
if command_text.startswith(self._SESSION_COMMANDS):
    actions.append(Send(to="system:admin", message=msg))
```

After: delete entirely. Also delete `self._SESSION_COMMANDS` attribute if present.

- [ ] **Step 2: Run tests**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v
```

Some tests in `tests/channel_server/core/handlers/test_feishu.py` may need updates (the ones that asserted `Send(to="system:admin")` on a command). Update those tests to assert the new behavior (non-command pass-through to downstream).

- [ ] **Step 3: Commit**

```bash
git add channel_server/core/handlers/feishu.py tests/channel_server/core/handlers/test_feishu.py
git commit -m "refactor(feishu): remove command detection from inbound handler"
```

---

## Task 16: Clean FeishuInboundHandler.on_spawn to stop creating anchors

**Files:**
- Modify: `channel_server/core/handlers/feishu.py`

The spawn command now creates the anchor and tool card before spawning the thread actor. Leaving the `mode == "child"` branch of `FeishuInboundHandler.on_spawn` in place would create duplicate anchors.

- [ ] **Step 1: Inspect the current branch**

```bash
cd /Users/h2oslabs/cc-openclaw && sed -n '78,100p' channel_server/core/handlers/feishu.py
```

Identify the lines that emit anchor/pin/tool_card `TransportSend` actions when `metadata.get("mode") == "child"`.

- [ ] **Step 2: Delete those lines**

Remove the `mode == "child"` branch. The `on_spawn` hook should now return an empty list (or only non-I/O actions, if any other logic remains).

- [ ] **Step 3: Update/remove tests**

Tests in `tests/channel_server/core/handlers/test_feishu.py` that asserted anchor-creation actions on `on_spawn` should be removed or updated.

- [ ] **Step 4: Run full test suite**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v
```

Expected: green.

- [ ] **Step 5: Final manual smoke**

Run through the full scenario from Task 14, Step 6. Specifically verify:
- Only **one** anchor is created per `/spawn` (check Feishu — no duplicate pinned messages).
- Only **one** tool card appears.

- [ ] **Step 6: Commit**

```bash
git add channel_server/core/handlers/feishu.py tests/channel_server/core/handlers/test_feishu.py
git commit -m "refactor(feishu): remove anchor/tool-card creation from on_spawn; now owned by spawn command"
```

---

## Final Checks

- [ ] **grep for dead references**

```bash
cd /Users/h2oslabs/cc-openclaw && grep -rn "SessionMgrHandler\|AdminHandler\|_handle_spawn\|_handle_kill\|_handle_sessions\|system:session-mgr" channel_server/ --include="*.py"
```

Expected: zero hits.

- [ ] **grep for `SESSION_COMMANDS`**

```bash
cd /Users/h2oslabs/cc-openclaw && grep -rn "SESSION_COMMANDS" channel_server/ --include="*.py"
```

Expected: zero hits.

- [ ] **Full test suite green**

```bash
cd /Users/h2oslabs/cc-openclaw && python -m pytest tests/ -v
```

- [ ] **Final commit if anything trailing**

```bash
git status   # should be clean
```

If anything remains, commit as `chore(commands): final cleanup`.

---

## Notes for the Implementer

- **Always run tests between tasks.** This plan is TDD-oriented; each task's tests should be green before moving on.
- **Don't skip the `fallback_on_unknown=True` → `False` flip (Task 14).** It's the single switch that turns on full unified-registry behavior. If you forget, unknown slash commands silently fall through and users see no feedback.
- **The address scheme is preserved intentionally.** Do not "clean up" the `feishu:{app_id}:{chat_id}:thread:{session_name}` format — it must match what's already persisted on disk.
- **If FeishuAdapter lacks `reply` / `reply_error` / outbound text helpers**, look at how the existing `_handle_outbound` or `_send_text` path works and wrap that. Do not invent new Feishu API calls.
- **CC adapter's `kill_tmux` may not exist yet.** Check `adapters/cc/adapter.py` — if the existing code kills tmux via a different method name, use that. Only introduce a new async method if truly absent.
- **For ambiguity about exact startup file:** `channel_server/app.py` is the most likely location; if not there, search for `ActorRuntime(` and where adapters are constructed.
