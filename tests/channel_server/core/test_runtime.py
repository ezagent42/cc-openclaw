"""Tests for ActorRuntime — spawn, send, stop, attach/detach, per-actor loops."""
from __future__ import annotations

import asyncio

import pytest

from channel_server.core.actor import (
    Actor,
    Message,
    Send,
    StopActor,
    Transport,
    TransportSend,
    UpdateActor,
)
from channel_server.core.runtime import ActorRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_runtime() -> ActorRuntime:
    return ActorRuntime()


# ---------------------------------------------------------------------------
# 1. spawn creates actor + lookup works
# ---------------------------------------------------------------------------

def test_spawn_creates_actor_and_lookup():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all", tag="test")
    assert isinstance(actor, Actor)
    assert actor.address == "actor://a"
    assert actor.handler == "forward_all"
    assert actor.tag == "test"
    assert actor.state == "active"
    assert rt.lookup("actor://a") is actor


# ---------------------------------------------------------------------------
# 2. spawn duplicate raises ValueError
# ---------------------------------------------------------------------------

def test_spawn_duplicate_raises():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    with pytest.raises(ValueError, match="already exists"):
        rt.spawn("actor://a", "forward_all")


def test_spawn_over_ended_actor_succeeds():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    rt.stop("actor://a")
    # Should succeed — old actor is ended.
    actor = rt.spawn("actor://a", "forward_all", tag="respawned")
    assert actor.tag == "respawned"
    assert actor.state == "active"


# ---------------------------------------------------------------------------
# 3. stop ends actor
# ---------------------------------------------------------------------------

def test_stop_ends_actor():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    rt.stop("actor://a")
    actor = rt.lookup("actor://a")
    assert actor is not None
    assert actor.state == "ended"


def test_stop_nonexistent_is_graceful():
    rt = make_runtime()
    # Should not raise.
    rt.stop("actor://nonexistent")


# ---------------------------------------------------------------------------
# 4. send delivers to mailbox
# ---------------------------------------------------------------------------

def test_send_delivers_to_mailbox():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    msg = Message(sender="actor://b", type="chat", payload={"text": "hi"})
    rt.send("actor://a", msg)
    assert not rt.mailboxes["actor://a"].empty()
    queued = rt.mailboxes["actor://a"].get_nowait()
    assert queued is msg


# ---------------------------------------------------------------------------
# 5. send to nonexistent is graceful (no crash)
# ---------------------------------------------------------------------------

def test_send_to_nonexistent_no_crash():
    rt = make_runtime()
    msg = Message(sender="actor://b", type="chat")
    # Should not raise.
    rt.send("actor://nowhere", msg)


def test_send_to_ended_actor_no_crash():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    rt.stop("actor://a")
    msg = Message(sender="actor://b", type="chat")
    # Should not raise.
    rt.send("actor://a", msg)


# ---------------------------------------------------------------------------
# 6. attach transport + resume suspended
# ---------------------------------------------------------------------------

def test_attach_sets_transport():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    transport = Transport(type="websocket", config={"url": "ws://localhost"})
    rt.attach("actor://a", transport)
    actor = rt.lookup("actor://a")
    assert actor is not None
    assert actor.transport is transport


def test_attach_resumes_suspended_actor():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all")
    actor.state = "suspended"
    transport = Transport(type="websocket", config={})
    rt.attach("actor://a", transport)
    assert actor.state == "active"


# ---------------------------------------------------------------------------
# 7. detach transport + suspend
# ---------------------------------------------------------------------------

def test_detach_removes_transport_and_suspends():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    transport = Transport(type="websocket", config={})
    rt.attach("actor://a", transport)
    assert rt.lookup("actor://a").transport is not None

    rt.detach("actor://a")
    actor = rt.lookup("actor://a")
    assert actor.transport is None
    assert actor.state == "suspended"


def test_detach_nonexistent_is_graceful():
    rt = make_runtime()
    # Should not raise.
    rt.detach("actor://nonexistent")


# ---------------------------------------------------------------------------
# 8. actor loop processes messages (forward_all forwarding)
# ---------------------------------------------------------------------------

async def test_actor_loop_forwards_messages():
    """forward_all on src forwards to dst; dst has a transport handler that captures the payload."""
    rt = make_runtime()
    received: list[dict] = []

    async def capture(actor: Actor, payload: dict):
        received.append(payload)

    rt.register_transport_handler("test", capture)

    transport = Transport(type="test", config={})
    # src forwards to dst; dst uses tool_card which emits TransportSend.
    src = rt.spawn("actor://src", "forward_all", downstream=["actor://dst"])
    dst = rt.spawn("actor://dst", "tool_card", transport=transport)

    msg = Message(sender="actor://ext", type="chat", payload={"text": "hello"})
    rt.send("actor://src", msg)

    run_task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.15)
    await rt.shutdown()
    await run_task

    # tool_card handler emits TransportSend, which our capture handler receives.
    assert len(received) == 1
    assert received[0]["type"] == "tool_card_update"
    assert "hello" in received[0]["text"]


# ---------------------------------------------------------------------------
# 9. actor loop handles errors gracefully (no crash on handler error)
# ---------------------------------------------------------------------------

async def test_actor_loop_handles_handler_error():
    """A handler that raises should not crash the actor loop (up to max_errors).

    The parent uses a transport handler to capture the error notification.
    """
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class BrokenHandler:
        def handle(self, actor, msg):
            raise RuntimeError("boom")

    received: list[dict] = []

    # Parent uses forward_all with a transport that captures messages.
    # But forward_all doesn't emit TransportSend. Instead, let's use a
    # collecting handler.
    class CollectorHandler:
        """Stores received messages in a shared list."""
        def __init__(self, sink: list):
            self._sink = sink

        def handle(self, actor, msg):
            self._sink.append(msg)
            return []

    HANDLER_REGISTRY["broken"] = BrokenHandler()
    HANDLER_REGISTRY["collector"] = CollectorHandler(received)
    try:
        parent = rt.spawn("actor://parent", "collector")
        child = rt.spawn("actor://child", "broken", parent="actor://parent")

        rt.send("actor://child", Message(sender="actor://ext", type="chat"))

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.15)
        await rt.shutdown()
        await run_task

        # Parent's collector handler should have received the error message.
        assert len(received) == 1
        assert received[0].type == "error"
        assert "boom" in received[0].payload["error"]
    finally:
        del HANDLER_REGISTRY["broken"]
        del HANDLER_REGISTRY["collector"]


async def test_actor_loop_ends_after_max_errors():
    """After max_errors consecutive failures the actor state becomes ended."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class BrokenHandler:
        def handle(self, actor, msg):
            raise RuntimeError("boom")

    HANDLER_REGISTRY["broken2"] = BrokenHandler()
    try:
        actor = rt.spawn("actor://bad", "broken2")

        # Send more than max_errors messages.
        for _ in range(12):
            rt.send("actor://bad", Message(sender="actor://ext", type="chat"))

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.5)
        await rt.shutdown()
        await run_task

        assert actor.state == "ended"
    finally:
        del HANDLER_REGISTRY["broken2"]


# ---------------------------------------------------------------------------
# 10. register_transport_handler + TransportSend dispatches correctly
# ---------------------------------------------------------------------------

async def test_transport_send_dispatches():
    rt = make_runtime()
    sent_payloads: list[dict] = []

    async def ws_sender(actor: Actor, payload: dict):
        sent_payloads.append(payload)

    rt.register_transport_handler("websocket", ws_sender)

    transport = Transport(type="websocket", config={"url": "ws://localhost"})
    actor = rt.spawn("actor://tc", "tool_card", transport=transport)

    msg = Message(sender="actor://ext", type="chat", payload={"text": "ping"})
    rt.send("actor://tc", msg)

    run_task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.15)
    await rt.shutdown()
    await run_task

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["type"] == "tool_card_update"


# ---------------------------------------------------------------------------
# 11. UpdateActor merges metadata
# ---------------------------------------------------------------------------

def test_execute_update_actor_merges_metadata():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all", metadata={"key1": "val1"})
    action = UpdateActor(changes={"metadata": {"key2": "val2"}})
    rt._execute(actor, action)
    assert actor.metadata == {"key1": "val1", "key2": "val2"}


def test_execute_update_actor_sets_fields():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all", tag="old")
    action = UpdateActor(changes={"tag": "new"})
    rt._execute(actor, action)
    assert actor.tag == "new"


# ---------------------------------------------------------------------------
# 12. shutdown cancels all tasks
# ---------------------------------------------------------------------------

async def test_shutdown_cancels_all_tasks():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    rt.spawn("actor://b", "forward_all")

    run_task = asyncio.create_task(rt.run())
    await asyncio.sleep(0.05)
    await rt.shutdown()
    await run_task

    # After shutdown, all actors should have loops cancelled / runtime stopped.
    assert rt._stop_event.is_set()
