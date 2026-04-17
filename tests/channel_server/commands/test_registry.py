"""Tests for resolve_scope, ROOT_SCOPE, and CommandDispatcher."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from channel_server.commands.dispatcher import CommandDispatcher
from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE, resolve_scope
from channel_server.core.actor import Actor


def _make_rt(actors: dict[str, Actor]):
    rt = MagicMock()
    rt.lookup.side_effect = lambda addr: actors.get(addr)
    return rt


# ---------- resolve_scope ----------

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
    actors = {}
    prev = None
    for i in range(20):
        addr = f"depth:{i}"
        actors[addr] = Actor(address=addr, tag="", handler="", parent=prev)
        prev = addr
    rt = _make_rt(actors)

    with pytest.raises(CommandError):
        resolve_scope("depth:19", rt)


# ---------- CommandDispatcher ----------

@pytest.mark.asyncio
async def test_dispatcher_non_command_returns_false(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="hello",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "app_id": "fake_app",
                                        "current_actor": None, "parent_actor": None,
                                        "thread_root_id": None, "raw_msg": None}
    )
    assert handled is False


@pytest.mark.asyncio
async def test_dispatcher_unknown_command_with_fallback_returns_false(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc,
                          fallback_on_unknown=True)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/nope",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "app_id": "fake_app",
                                        "current_actor": None, "parent_actor": None,
                                        "thread_root_id": None, "raw_msg": None}
    )
    assert handled is False
    assert fake_adapters.errors == []


@pytest.mark.asyncio
async def test_dispatcher_unknown_command_no_fallback_replies_error(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc,
                          fallback_on_unknown=False)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/nope",
        source_actor=None, ctx_partial={"source": "feishu", "user": "u", "chat_id": "c",
                                        "app_id": "fake_app",
                                        "current_actor": None, "parent_actor": None,
                                        "thread_root_id": None, "raw_msg": None}
    )
    assert handled is True
    assert len(fake_adapters.errors) == 1
    assert "未知命令" in fake_adapters.errors[0][1]
