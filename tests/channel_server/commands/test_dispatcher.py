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
