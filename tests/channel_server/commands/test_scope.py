"""Unit tests for CommandScope."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from channel_server.commands.errors import UnknownCommand, BadArgs
from channel_server.commands.scope import CommandScope


def _ctx_partial(**overrides):
    base = {
        "source": "test", "user": "u", "chat_id": "c", "app_id": "fake_app",
        "raw_msg": None, "feishu": MagicMock(), "cc": MagicMock(),
        "runtime": MagicMock(),
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

    # Pass partial ctx without 'source' so scope defaults take over.
    ctx = {k: v for k, v in _ctx_partial().items() if k != "source"}
    await child.dispatch("x", [], ctx)
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
