"""Shared fixtures for command tests.

Fakes mirror the REAL adapter method names from channel_server/adapters/*.
Do NOT invent method names — the command code uses these exact names.
"""
from __future__ import annotations

import pytest

from channel_server.core.actor import Actor, Transport


class FakeFeishuAdapter:
    """Mirrors FeishuAdapter public surface used by commands.

    Real methods (verified 2026-04-17):
      async create_thread_anchor(chat_id, tag) -> str | None
      async pin_message(message_id) -> bool
      async unpin_message(message_id) -> bool
      attribute: app_id: str
    """
    def __init__(self):
        self.app_id = "fake_app"
        self.created_anchors: list[tuple[str, str]] = []
        self.pinned: list[str] = []
        self.unpinned: list[str] = []
        self.replies: list[tuple[object, str]] = []
        self.errors: list[tuple[dict, str]] = []

    async def create_thread_anchor(self, chat_id, tag):
        self.created_anchors.append((chat_id, tag))
        return f"anchor_{tag}"

    async def pin_message(self, message_id):
        self.pinned.append(message_id)
        return True

    async def unpin_message(self, message_id):
        self.unpinned.append(message_id)
        return True

    async def reply(self, ctx, text):
        self.replies.append((ctx, text))

    async def reply_error(self, ctx_partial, text):
        self.errors.append((ctx_partial, text))


class FakeCCAdapter:
    """Mirrors CCAdapter public surface used by commands.

    Real methods (verified 2026-04-17):
      SYNC spawn_cc_process(user, session_name, tag="", chat_id="") -> bool
      SYNC kill_cc_process(user, session_name) -> None
    """
    def __init__(self):
        self.spawned: list[tuple[str, str, str, str]] = []
        self.killed: list[tuple[str, str]] = []
        self.spawn_cc_process_ok = True

    def spawn_cc_process(self, user, session_name, tag="", chat_id=""):
        self.spawned.append((user, session_name, tag, chat_id))
        return self.spawn_cc_process_ok

    def kill_cc_process(self, user, session_name):
        self.killed.append((user, session_name))


@pytest.fixture
def fake_feishu():
    return FakeFeishuAdapter()


@pytest.fixture
def fake_cc():
    return FakeCCAdapter()


@pytest.fixture
def fake_adapters(fake_feishu, fake_cc):
    """Back-compat bundle for tests that reference .feishu / .cc /
    .replies / .errors. New tests should use fake_feishu / fake_cc directly.
    """
    class _Bundle:
        def __init__(self):
            self.feishu = fake_feishu
            self.cc = fake_cc
            self.replies = fake_feishu.replies
            self.errors = fake_feishu.errors

        async def reply(self, ctx, text):
            await fake_feishu.reply(ctx, text)

        async def reply_error(self, ctx_partial, text):
            await fake_feishu.reply_error(ctx_partial, text)
    return _Bundle()


@pytest.fixture
def fake_runtime():
    """Lightweight actor-store fake. runtime.stop is ASYNC (matches real)."""
    class _Runtime:
        def __init__(self):
            self.actors: dict[str, Actor] = {}
            self.stop_calls: list[str] = []

        def spawn(self, address, handler, *, tag="", parent=None,
                  downstream=None, state="active", metadata=None, transport=None):
            actor = Actor(
                address=address, handler=handler, tag=tag, parent=parent,
                downstream=list(downstream or []),
                state=state, metadata=dict(metadata or {}),
                transport=transport,
            )
            self.actors[address] = actor
            return actor

        async def stop(self, address):
            self.stop_calls.append(address)
            if address in self.actors:
                self.actors[address].state = "ended"

        def lookup(self, address):
            return self.actors.get(address)

    return _Runtime()
