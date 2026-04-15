"""Tests for FeishuAdapter — inbound routing, dedup, echo prevention, auto-spawn."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime
from channel_server.adapters.feishu.adapter import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_adapter() -> tuple[FeishuAdapter, ActorRuntime]:
    rt = ActorRuntime()
    client = MagicMock()
    adapter = FeishuAdapter(rt, client)
    return adapter, rt


def feishu_event(
    chat_id: str = "oc_abc123",
    message_id: str = "msg_001",
    text: str = "hello",
    user: str = "Alice",
    user_id: str = "ou_alice",
    root_id: str | None = None,
    msg_type: str = "text",
) -> dict:
    """Build a minimal Feishu message event dict."""
    evt: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "user": user,
        "user_id": user_id,
        "msg_type": msg_type,
    }
    if root_id is not None:
        evt["root_id"] = root_id
    return evt


# ---------------------------------------------------------------------------
# 1. resolve_actor_address — main chat
# ---------------------------------------------------------------------------

def test_resolve_actor_address_main_chat():
    adapter, _ = make_adapter()
    addr = adapter.resolve_actor_address("oc_abc123", None)
    assert addr == "feishu:oc_abc123"


# ---------------------------------------------------------------------------
# 2. resolve_actor_address — thread
# ---------------------------------------------------------------------------

def test_resolve_actor_address_thread():
    adapter, _ = make_adapter()
    addr = adapter.resolve_actor_address("oc_abc123", "om_root456")
    assert addr == "feishu:oc_abc123:om_root456"


# ---------------------------------------------------------------------------
# 3. on_feishu_event auto-spawns actor
# ---------------------------------------------------------------------------

def test_on_feishu_event_auto_spawns_actor():
    adapter, rt = make_adapter()
    evt = feishu_event()
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    actor = rt.lookup(addr)
    assert actor is not None
    assert actor.handler == "feishu_inbound"
    assert actor.transport is not None
    assert actor.transport.type == "feishu_chat"


# ---------------------------------------------------------------------------
# 4. on_feishu_event delivers message to actor mailbox
# ---------------------------------------------------------------------------

def test_on_feishu_event_sends_message():
    adapter, rt = make_adapter()
    evt = feishu_event()
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    mailbox = rt.mailboxes.get(addr)
    assert mailbox is not None
    assert not mailbox.empty()
    msg = mailbox.get_nowait()
    assert isinstance(msg, Message)
    assert msg.type == "text"
    assert msg.payload["text"] == "hello"
    assert msg.metadata["user"] == "Alice"
    assert msg.metadata["user_id"] == "ou_alice"
    assert msg.metadata["message_id"] == "msg_001"


# ---------------------------------------------------------------------------
# 5. on_feishu_event dedup — same message_id only delivered once
# ---------------------------------------------------------------------------

def test_on_feishu_event_dedup():
    adapter, rt = make_adapter()
    evt = feishu_event(message_id="msg_dup")
    adapter.on_feishu_event(evt)
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    mailbox = rt.mailboxes[addr]
    # Only one message should have been delivered
    assert mailbox.qsize() == 1


# ---------------------------------------------------------------------------
# 6. on_feishu_event skips own messages (_recent_sent)
# ---------------------------------------------------------------------------

def test_on_feishu_event_skip_own_messages():
    adapter, rt = make_adapter()
    # Pre-populate _recent_sent with the message_id
    adapter._recent_sent.add("msg_self")
    evt = feishu_event(message_id="msg_self")
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123"
    # Actor may or may not be spawned, but no message should be delivered
    mailbox = rt.mailboxes.get(addr)
    if mailbox is not None:
        assert mailbox.empty()


# ---------------------------------------------------------------------------
# 7. on_feishu_event — thread events spawn with feishu_thread transport
# ---------------------------------------------------------------------------

def test_on_feishu_event_thread_transport():
    adapter, rt = make_adapter()
    evt = feishu_event(root_id="om_root789")
    adapter.on_feishu_event(evt)

    addr = "feishu:oc_abc123:om_root789"
    actor = rt.lookup(addr)
    assert actor is not None
    assert actor.transport is not None
    assert actor.transport.type == "feishu_thread"
    assert actor.transport.config["chat_id"] == "oc_abc123"
    assert actor.transport.config["root_id"] == "om_root789"


# ---------------------------------------------------------------------------
# 8. dedup set is bounded (max 10K entries)
# ---------------------------------------------------------------------------

def test_dedup_set_bounded():
    adapter, _ = make_adapter()
    # Push 10001 events with unique message_ids
    for i in range(10_001):
        evt = feishu_event(message_id=f"msg_{i}")
        adapter.on_feishu_event(evt)

    assert len(adapter._seen) <= 10_000
