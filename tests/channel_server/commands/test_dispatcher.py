"""Integration tests for CommandDispatcher + builtin commands."""
from __future__ import annotations

import pytest

from channel_server.commands.dispatcher import CommandDispatcher

# Import builtins so they register on ROOT_SCOPE
import channel_server.commands.builtin  # noqa: F401


def _ctx_partial(**overrides):
    """Partial ctx to pass into dispatch_from_adapter. The dispatcher injects
    `feishu`, `cc`, `runtime` from the adapter/runtime wiring — tests override
    those by passing a bundle adapter via `adapter=`."""
    base = {
        "source": "feishu", "user": "feishu_user:alice", "chat_id": "oc_chat",
        "app_id": "fake_app",
        "current_actor": None, "parent_actor": None,
        "thread_root_id": None, "raw_msg": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_help_replies_with_command_list(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/help",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    assert handled is True
    assert len(fake_adapters.replies) == 1
    text = fake_adapters.replies[0][1]
    assert "/help" in text


from channel_server.core.actor import Actor


@pytest.mark.asyncio
async def test_sessions_lists_user_cc_actors(fake_adapters, fake_runtime):
    # Seed fake runtime with two cc actors for alice
    fake_runtime.spawn(
        address="cc:alice.main", handler="cc_session", tag="main",
        parent="system:admin", state="active",
    )
    fake_runtime.spawn(
        address="cc:alice.sub", handler="cc_session", tag="sub",
        parent="cc:alice.main", state="active",
    )

    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/sessions",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice"),
    )
    assert handled is True
    reply = fake_adapters.replies[-1][1]
    assert "main" in reply and "sub" in reply


from channel_server.core.actor import Transport


@pytest.mark.asyncio
async def test_kill_stops_actor_and_kills_tmux(fake_adapters, fake_runtime):
    # Seed the CC actor + downstream feishu thread
    fake_runtime.spawn(
        address="cc:alice.foo", handler="cc_session", tag="foo",
        parent="system:admin", state="active",
        downstream=["feishu:fake_app:oc_chat:thread:foo"],
    )
    fake_runtime.spawn(
        address="feishu:fake_app:oc_chat:thread:foo",
        handler="feishu_inbound", tag="foo",
        parent="cc:alice.foo",
        transport=Transport(type="feishu_thread",
                            config={"chat_id": "oc_chat", "root_id": "anchor_foo"}),
    )

    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/kill foo",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice", chat_id="oc_chat"),
    )
    assert handled is True
    # Actors stopped via the runtime's async stop
    assert "cc:alice.foo" in fake_runtime.stop_calls
    assert "feishu:fake_app:oc_chat:thread:foo" in fake_runtime.stop_calls
    # Tmux process killed via sync kill_cc_process
    assert fake_adapters.cc.killed == [("alice", "foo")]


@pytest.mark.asyncio
async def test_kill_missing_name_bad_args(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/kill",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice"),
    )
    assert handled is True
    assert len(fake_adapters.errors) == 1
    assert "参数错误" in fake_adapters.errors[0][1]


@pytest.mark.asyncio
async def test_kill_unknown_session_command_failed(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/kill ghost",
        source_actor=None,
        ctx_partial=_ctx_partial(user="feishu_user:alice"),
    )
    assert handled is True
    assert any("命令失败" in m for _, m in fake_adapters.errors)
