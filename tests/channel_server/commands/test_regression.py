"""Regression tests pinning the four stated goals of the unified command registry.

See: docs/superpowers/specs/2026-04-17-unified-command-registry-design.md
"""
from __future__ import annotations

import pytest

import channel_server.commands.builtin  # register builtins
from channel_server.commands.dispatcher import CommandDispatcher
from channel_server.commands.registry import ROOT_SCOPE


def _ctx_partial(**overrides):
    base = {"source": "feishu", "user": "feishu_user:alice", "chat_id": "oc_chat",
            "app_id": "fake_app",
            "raw_msg": None}
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
        await ctx.feishu.reply(ctx, "pong")

    try:
        d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
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
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn iotest",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    # Anchor must be created before tmux is spawned
    assert fake_adapters.feishu.created_anchors
    assert fake_adapters.cc.spawned


# Goal #3 — quoted prefix doesn't break matching
@pytest.mark.asyncio
async def test_goal_3_quoted_prefix_matches(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    handled = await d.dispatch_from_adapter(
        adapter=fake_adapters,
        raw_text="> @林懿伦 earlier message\n/spawn quoted",
        source_actor=None, ctx_partial=_ctx_partial(),
    )
    assert handled is True
    assert "cc:alice.quoted" in fake_runtime.actors


# Goal #4a — Feishu spawn now starts tmux (old bug: legacy SessionMgrHandler
# created suspended actor but never called spawn_cc_process)
@pytest.mark.asyncio
async def test_goal_4a_feishu_spawn_starts_tmux(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn fromfeishu",
        source_actor=None, ctx_partial=_ctx_partial(source="feishu"),
    )
    assert fake_adapters.cc.spawned, \
        "Feishu-entry spawn must call spawn_cc_process (goal #4 fix)"


# Goal #4b — CC MCP spawn and Feishu spawn produce equivalent actor shape
@pytest.mark.asyncio
async def test_goal_4b_entry_symmetry(fake_adapters, fake_runtime):
    d = CommandDispatcher(fake_runtime, fake_adapters.feishu, fake_adapters.cc)

    # Path A: Feishu entry (top-level, no current_actor)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn via_feishu",
        source_actor=None, ctx_partial=_ctx_partial(source="feishu"),
    )

    # Path B: CC MCP entry with source_actor=None (also top-level)
    await d.dispatch_from_adapter(
        adapter=fake_adapters, raw_text="/spawn via_cc",
        source_actor=None, ctx_partial=_ctx_partial(source="cc_mcp"),
    )

    a_feishu = fake_runtime.actors["cc:alice.via_feishu"]
    a_cc = fake_runtime.actors["cc:alice.via_cc"]

    # Modulo tag/name differences, actor shape matches. Both start suspended
    # (real behavior — WS connection transitions to active later).
    assert a_feishu.state == a_cc.state == "suspended"
    assert a_feishu.handler == a_cc.handler == "cc_session"
    assert len(a_feishu.downstream) == len(a_cc.downstream)
    # Both called spawn_cc_process
    assert len(fake_adapters.cc.spawned) == 2
