"""Tests for async runtime additions: TransportSend returns, wire(), dedup."""
from __future__ import annotations

import asyncio

import pytest

from channel_server.core.actor import Actor, Message, Transport, TransportSend
from channel_server.core.runtime import ActorRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_runtime() -> ActorRuntime:
    return ActorRuntime()


# ---------------------------------------------------------------------------
# 1. test_transport_send_return_merges_metadata
#    Async transport handler that returns a dict: verify merged into actor.metadata
# ---------------------------------------------------------------------------

async def test_transport_send_return_merges_metadata():
    """Transport handler returning a dict should have its contents merged into actor.metadata."""
    rt = make_runtime()

    async def async_handler(actor: Actor, payload: dict) -> dict:
        return {"reply_msg_id": "msg-abc-123", "extra": "value"}

    rt.register_transport_handler("test_async", async_handler)

    transport = Transport(type="test_async", config={})
    actor = rt.spawn("actor://ts", "forward_all", transport=transport)

    action = TransportSend(payload={"action": "send_msg", "text": "hello"})
    await rt._execute_transport_send(actor, action)

    assert actor.metadata.get("reply_msg_id") == "msg-abc-123"
    assert actor.metadata.get("extra") == "value"


async def test_transport_send_return_none_no_crash():
    """Transport handler returning None should not crash."""
    rt = make_runtime()

    def sync_handler(actor: Actor, payload: dict) -> None:
        return None

    rt.register_transport_handler("test_none", sync_handler)

    transport = Transport(type="test_none", config={})
    actor = rt.spawn("actor://ts_none", "forward_all", transport=transport)

    action = TransportSend(payload={"action": "send_msg"})
    await rt._execute_transport_send(actor, action)

    # metadata untouched
    assert actor.metadata == {}


async def test_transport_send_return_sent_msg_id_ring_buffer():
    """_sent_msg_id in return dict is appended to sent_msg_ids ring buffer (capped at 100)."""
    rt = make_runtime()
    call_count = [0]

    def sync_handler(actor: Actor, payload: dict) -> dict:
        call_count[0] += 1
        return {"_sent_msg_id": f"msg-{call_count[0]}"}

    rt.register_transport_handler("test_ring", sync_handler)

    transport = Transport(type="test_ring", config={})
    actor = rt.spawn("actor://ring", "forward_all", transport=transport)

    # Send 110 messages — ring buffer should cap at 100
    for _ in range(110):
        action = TransportSend(payload={"text": "x"})
        await rt._execute_transport_send(actor, action)

    sent_ids = actor.metadata.get("sent_msg_ids", [])
    assert len(sent_ids) == 100
    # Most recent should be msg-110
    assert sent_ids[-1] == "msg-110"
    # Oldest retained should be msg-11 (110 - 100 + 1)
    assert sent_ids[0] == "msg-11"


# ---------------------------------------------------------------------------
# 2. test_wire_appends_downstream
#    wire() appends, deduplicates, no-op on missing actor
# ---------------------------------------------------------------------------

def test_wire_appends_downstream():
    """wire() appends to_addr to from_addr's downstream list."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    rt.wire("actor://a", "actor://b")
    assert rt.lookup("actor://a").downstream == ["actor://b"]


def test_wire_deduplicates():
    """wire() does not append to_addr if already present."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all", downstream=["actor://b"])

    rt.wire("actor://a", "actor://b")
    assert rt.lookup("actor://a").downstream == ["actor://b"]


def test_wire_noop_on_missing_actor():
    """wire() is a no-op when from_addr does not exist."""
    rt = make_runtime()
    # Should not raise
    rt.wire("actor://missing", "actor://b")


def test_wire_multiple():
    """wire() can add multiple downstream targets."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    rt.wire("actor://a", "actor://b")
    rt.wire("actor://a", "actor://c")
    rt.wire("actor://a", "actor://b")  # duplicate

    assert rt.lookup("actor://a").downstream == ["actor://b", "actor://c"]


# ---------------------------------------------------------------------------
# 3. test_send_dedup_by_message_id
#    Same message_id is only delivered once
# ---------------------------------------------------------------------------

def test_send_dedup_by_message_id():
    """send() with the same message_id only delivers the message once."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    msg1 = Message(sender="actor://b", payload={"text": "first"})
    msg2 = Message(sender="actor://b", payload={"text": "second"})

    rt.send("actor://a", msg1, message_id="unique-id-1")
    rt.send("actor://a", msg2, message_id="unique-id-1")  # should be deduped

    mailbox = rt.mailboxes["actor://a"]
    assert mailbox.qsize() == 1
    queued = mailbox.get_nowait()
    assert queued is msg1


def test_send_no_dedup_without_message_id():
    """send() without message_id delivers all messages (no dedup)."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    msg1 = Message(sender="actor://b", payload={"text": "first"})
    msg2 = Message(sender="actor://b", payload={"text": "second"})

    rt.send("actor://a", msg1)
    rt.send("actor://a", msg2)

    assert rt.mailboxes["actor://a"].qsize() == 2


def test_send_dedup_different_ids_both_delivered():
    """send() with different message_ids delivers both."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    msg1 = Message(sender="actor://b", payload={"text": "first"})
    msg2 = Message(sender="actor://b", payload={"text": "second"})

    rt.send("actor://a", msg1, message_id="id-1")
    rt.send("actor://a", msg2, message_id="id-2")

    assert rt.mailboxes["actor://a"].qsize() == 2


# ---------------------------------------------------------------------------
# 4. test_send_dedup_bounded
#    Dedup set is bounded at _dedup_max
# ---------------------------------------------------------------------------

def test_send_dedup_bounded():
    """_dedup set is pruned when it exceeds _dedup_max."""
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")

    # Fill dedup beyond max
    limit = rt._dedup_max
    for i in range(limit + 10):
        msg = Message(sender="actor://b", payload={"i": i})
        rt.send("actor://a", msg, message_id=f"msg-{i}")

    # Dedup set should have been pruned — size should be well below limit + 10
    assert len(rt._dedup) <= limit
    # After pruning, size should be approximately _dedup_max // 2 + 10
    assert len(rt._dedup) <= (limit // 2) + 20


def test_send_dedup_bounded_allows_new_ids_after_prune():
    """After dedup pruning, new message_ids are accepted."""
    rt = make_runtime()
    rt._dedup_max = 10  # Use a small limit for testing

    rt.spawn("actor://a", "forward_all")

    # Fill up the dedup set past max
    for i in range(15):
        msg = Message(sender="actor://b", payload={"i": i})
        rt.send("actor://a", msg, message_id=f"msg-{i}")

    # The dedup set should have been pruned
    assert len(rt._dedup) <= rt._dedup_max

    # New IDs should still be accepted (not treated as duplicates)
    msg_new = Message(sender="actor://b", payload={"text": "new"})
    size_before = rt.mailboxes["actor://a"].qsize()
    rt.send("actor://a", msg_new, message_id="brand-new-id-xyz")
    assert rt.mailboxes["actor://a"].qsize() == size_before + 1
