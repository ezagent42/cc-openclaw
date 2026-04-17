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


async def test_spawn_over_ended_actor_succeeds():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    await rt.stop("actor://a")
    # Should succeed — old actor is ended.
    actor = rt.spawn("actor://a", "forward_all", tag="respawned")
    assert actor.tag == "respawned"
    assert actor.state == "active"


# ---------------------------------------------------------------------------
# 3. stop ends actor
# ---------------------------------------------------------------------------

async def test_stop_ends_actor():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    await rt.stop("actor://a")
    actor = rt.lookup("actor://a")
    assert actor is not None
    assert actor.state == "ended"


async def test_stop_nonexistent_is_graceful():
    rt = make_runtime()
    # Should not raise.
    await rt.stop("actor://nonexistent")


# ---------------------------------------------------------------------------
# 4. send delivers to mailbox
# ---------------------------------------------------------------------------

def test_send_delivers_to_mailbox():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    msg = Message(sender="actor://b", payload={"text": "hi"})
    rt.send("actor://a", msg)
    assert not rt.mailboxes["actor://a"].empty()
    queued = rt.mailboxes["actor://a"].get_nowait()
    assert queued is msg


# ---------------------------------------------------------------------------
# 5. send to nonexistent is graceful (no crash)
# ---------------------------------------------------------------------------

def test_send_to_nonexistent_no_crash():
    rt = make_runtime()
    msg = Message(sender="actor://b")
    # Should not raise.
    rt.send("actor://nowhere", msg)


async def test_send_to_ended_actor_no_crash():
    rt = make_runtime()
    rt.spawn("actor://a", "forward_all")
    await rt.stop("actor://a")
    msg = Message(sender="actor://b")
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
    # src forwards to dst; dst uses a relay handler that emits TransportSend.
    from channel_server.core.handler import HANDLER_REGISTRY

    class RelayHandler:
        def handle(self, actor, msg, runtime=None):
            return [TransportSend(payload={"action": "relay", "text": msg.payload.get("text", "")})]

    HANDLER_REGISTRY["_test_relay"] = RelayHandler()
    try:
        src = rt.spawn("actor://src", "forward_all", downstream=["actor://dst"])
        dst = rt.spawn("actor://dst", "_test_relay", transport=transport)

        msg = Message(sender="actor://ext", payload={"text": "hello"})
        rt.send("actor://src", msg)

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.15)
        await rt.shutdown()
        await run_task

        # RelayHandler emits TransportSend, which our capture handler receives.
        assert len(received) == 1
        assert received[0]["action"] == "relay"
        assert "hello" in received[0]["text"]
    finally:
        del HANDLER_REGISTRY["_test_relay"]


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
        def handle(self, actor, msg, runtime=None):
            raise RuntimeError("boom")

    received: list[dict] = []

    # Parent uses forward_all with a transport that captures messages.
    # But forward_all doesn't emit TransportSend. Instead, let's use a
    # collecting handler.
    class CollectorHandler:
        """Stores received messages in a shared list."""
        def __init__(self, sink: list):
            self._sink = sink

        def handle(self, actor, msg, runtime=None):
            self._sink.append(msg)
            return []

    HANDLER_REGISTRY["broken"] = BrokenHandler()
    HANDLER_REGISTRY["collector"] = CollectorHandler(received)
    try:
        parent = rt.spawn("actor://parent", "collector")
        child = rt.spawn("actor://child", "broken", parent="actor://parent")

        rt.send("actor://child", Message(sender="actor://ext"))

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.15)
        await rt.shutdown()
        await run_task

        # Parent's collector handler should have received the error message.
        assert len(received) == 1
        assert received[0].payload.get("msg_type") == "error"
        assert "boom" in received[0].payload["error"]
    finally:
        del HANDLER_REGISTRY["broken"]
        del HANDLER_REGISTRY["collector"]


async def test_actor_loop_ends_after_max_errors():
    """After max_errors consecutive failures the actor state becomes ended."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class BrokenHandler:
        def handle(self, actor, msg, runtime=None):
            raise RuntimeError("boom")

    HANDLER_REGISTRY["broken2"] = BrokenHandler()
    try:
        actor = rt.spawn("actor://bad", "broken2")

        # Send more than max_errors messages.
        for _ in range(12):
            rt.send("actor://bad", Message(sender="actor://ext"))

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
    from channel_server.core.handler import HANDLER_REGISTRY

    class PingHandler:
        def handle(self, actor, msg, runtime=None):
            return [TransportSend(payload={"action": "pong", "text": msg.payload.get("text", "")})]

    HANDLER_REGISTRY["_test_ping"] = PingHandler()
    try:
        actor = rt.spawn("actor://tc", "_test_ping", transport=transport)

        msg = Message(sender="actor://ext", payload={"text": "ping"})
        rt.send("actor://tc", msg)

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.15)
        await rt.shutdown()
        await run_task

        assert len(sent_payloads) == 1
        assert sent_payloads[0]["action"] == "pong"
    finally:
        del HANDLER_REGISTRY["_test_ping"]


# ---------------------------------------------------------------------------
# 11. UpdateActor merges metadata
# ---------------------------------------------------------------------------

async def test_execute_update_actor_merges_metadata():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all", metadata={"key1": "val1"})
    action = UpdateActor(changes={"metadata": {"key2": "val2"}})
    await rt._execute(actor, action)
    assert actor.metadata == {"key1": "val1", "key2": "val2"}


async def test_execute_update_actor_sets_fields():
    rt = make_runtime()
    actor = rt.spawn("actor://a", "forward_all", tag="old")
    action = UpdateActor(changes={"tag": "new"})
    await rt._execute(actor, action)
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


# ---------------------------------------------------------------------------
# 13. handler error notifies parent
# ---------------------------------------------------------------------------

async def test_handler_error_notifies_parent():
    """A broken child handler should send an error message to its parent."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class BrokenHandler:
        def handle(self, actor, msg, runtime=None):
            raise RuntimeError("test-error")

    received: list = []

    class CollectorHandler:
        def __init__(self, sink):
            self._sink = sink

        def handle(self, actor, msg, runtime=None):
            self._sink.append(msg)
            return []

    HANDLER_REGISTRY["broken_notify"] = BrokenHandler()
    HANDLER_REGISTRY["collector_notify"] = CollectorHandler(received)
    try:
        rt.spawn("actor://parent", "collector_notify")
        rt.spawn("actor://child", "broken_notify", parent="actor://parent")

        rt.send("actor://child", Message(sender="actor://ext"))

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.15)
        await rt.shutdown()
        await run_task

        assert len(received) == 1
        assert received[0].payload.get("msg_type") == "error"
        assert "test-error" in received[0].payload["error"]
    finally:
        del HANDLER_REGISTRY["broken_notify"]
        del HANDLER_REGISTRY["collector_notify"]


# ---------------------------------------------------------------------------
# 14. max errors stops actor
# ---------------------------------------------------------------------------

async def test_max_errors_stops_actor():
    """After 10 consecutive handler errors the actor state becomes ended."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class BrokenHandler:
        def handle(self, actor, msg, runtime=None):
            raise RuntimeError("always-broken")

    HANDLER_REGISTRY["broken_max"] = BrokenHandler()
    try:
        actor = rt.spawn("actor://fail", "broken_max")

        # Send 15 messages (more than max_errors=10)
        for _ in range(15):
            rt.send("actor://fail", Message(sender="actor://ext"))

        run_task = asyncio.create_task(rt.run())
        await asyncio.sleep(0.5)
        await rt.shutdown()
        await run_task

        assert actor.state == "ended"
    finally:
        del HANDLER_REGISTRY["broken_max"]


# ---------------------------------------------------------------------------
# 15. runtime.stop calls on_stop lifecycle callback
# ---------------------------------------------------------------------------

async def test_stop_calls_on_stop():
    """runtime.stop() should invoke the handler's on_stop before ending the actor."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    on_stop_called = []

    class LifecycleHandler:
        def handle(self, actor, msg, runtime=None):
            return []

        def on_stop(self, actor):
            on_stop_called.append(actor.address)
            return []

    HANDLER_REGISTRY["lifecycle_test"] = LifecycleHandler()
    try:
        rt.spawn("actor://lc", "lifecycle_test")
        assert rt.lookup("actor://lc").state == "active"

        await rt.stop("actor://lc")

        assert rt.lookup("actor://lc").state == "ended"
        assert on_stop_called == ["actor://lc"]
    finally:
        del HANDLER_REGISTRY["lifecycle_test"]


async def test_stop_on_stop_cascades():
    """on_stop can emit StopActor to cascade cleanup."""
    rt = make_runtime()
    from channel_server.core.handler import HANDLER_REGISTRY

    class ParentHandler:
        def handle(self, actor, msg, runtime=None):
            return []

        def on_stop(self, actor):
            return [StopActor(address="actor://child")]

    class ChildHandler:
        def handle(self, actor, msg, runtime=None):
            return []

        def on_stop(self, actor):
            return []

    HANDLER_REGISTRY["cascade_parent"] = ParentHandler()
    HANDLER_REGISTRY["cascade_child"] = ChildHandler()
    try:
        rt.spawn("actor://parent", "cascade_parent")
        rt.spawn("actor://child", "cascade_child")

        await rt.stop("actor://parent")

        assert rt.lookup("actor://parent").state == "ended"
        assert rt.lookup("actor://child").state == "ended"
    finally:
        del HANDLER_REGISTRY["cascade_parent"]
        del HANDLER_REGISTRY["cascade_child"]


async def test_stop_idempotent():
    """Stopping an already-ended actor should be a no-op."""
    rt = make_runtime()
    rt.spawn("actor://x", "forward_all")
    await rt.stop("actor://x")
    await rt.stop("actor://x")  # should not raise
    assert rt.lookup("actor://x").state == "ended"


# ---------------------------------------------------------------------------
# 16. on_spawn lifecycle hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_calls_on_spawn_hook():
    """on_spawn hook actions are executed when actor is spawned."""
    from channel_server.core.actor import TransportSend
    from channel_server.core.handler import HANDLER_REGISTRY

    class SpawnHookHandler:
        def handle(self, actor, msg, runtime=None):
            return []
        def on_spawn(self, actor):
            return [TransportSend(payload={"action": "init", "tag": actor.tag})]
        def on_stop(self, actor):
            return []

    rt = ActorRuntime()
    HANDLER_REGISTRY["test_spawn_hook"] = SpawnHookHandler()
    transport_calls = []

    async def mock_transport(actor, payload):
        transport_calls.append(payload)
        return None

    rt.register_transport_handler("test", mock_transport)

    try:
        rt.spawn("test:actor", "test_spawn_hook", tag="mytag",
                 transport=Transport(type="test", config={}))
        await asyncio.sleep(0.2)
        assert len(transport_calls) == 1
        assert transport_calls[0]["action"] == "init"
        assert transport_calls[0]["tag"] == "mytag"
    finally:
        del HANDLER_REGISTRY["test_spawn_hook"]
