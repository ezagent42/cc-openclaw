"""Integration tests — end-to-end message flow through the actor runtime."""
from __future__ import annotations

import asyncio

import pytest

from channel_server.core.actor import Message, Transport
from channel_server.core.runtime import ActorRuntime


# ---------------------------------------------------------------------------
# 12. Full session lifecycle via session-mgr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mgr_ignores_spawn_command():
    """/spawn is no longer handled by session-mgr (Task 12: unified registry).

    Verify that a /spawn message sent directly to session-mgr produces no
    actor side-effects — the command is silently dropped. In the live system,
    /spawn is intercepted by CommandDispatcher before session-mgr is involved.
    """
    rt = ActorRuntime()
    rt.spawn("system:session-mgr", "session_mgr", tag="session-mgr")
    task = asyncio.create_task(rt.run())

    # Send /spawn dev directly to session-mgr (as would have happened in legacy)
    rt.send(
        "system:session-mgr",
        Message(
            sender="system:admin",
            payload={
                "text": "/spawn dev",
                "user": "alice",
                "chat_id": "oc_chat1",
                "app_id": "test_app",
            },
        ),
    )

    await asyncio.sleep(0.1)

    # Verify: session-mgr no longer spawns actors for /spawn
    thread_actor = rt.lookup("feishu:test_app:oc_chat1:thread:dev")
    assert thread_actor is None, "session-mgr should NOT handle /spawn (now in unified registry)"

    cc_actor = rt.lookup("cc:alice.dev")
    assert cc_actor is None, "session-mgr should NOT handle /spawn (now in unified registry)"

    await rt.shutdown()
    await task


# ---------------------------------------------------------------------------
# 1. feishu actor receives message -> forwards to cc actor -> cc gets transport push
# ---------------------------------------------------------------------------


async def test_end_to_end_feishu_to_cc():
    """feishu actor receives message, forwards to cc actor, cc gets transport push."""
    runtime = ActorRuntime()
    transport_log: list[dict] = []

    def mock_ws_transport(actor, payload):
        transport_log.append(payload)

    runtime.register_transport_handler("websocket", mock_ws_transport)

    runtime.spawn(
        "feishu:test_app:oc_xxx",
        handler="feishu_inbound",
        tag="DM",
        downstream=["cc:linyilun.root"],
    )
    runtime.spawn(
        "cc:linyilun.root",
        handler="cc_session",
        tag="root",
        downstream=["feishu:test_app:oc_xxx"],
    )
    runtime.attach(
        "cc:linyilun.root",
        Transport(type="websocket", config={}),
    )

    task = asyncio.create_task(runtime.run())
    runtime.send(
        "feishu:test_app:oc_xxx",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "hello", "chat_id": "oc_xxx", "message_id": "om_1", "file_path": ""},
            metadata={"chat_id": "oc_xxx", "user": "testuser"},
        ),
    )
    await asyncio.sleep(0.2)

    assert len(transport_log) == 1
    assert transport_log[0]["text"] == "hello"
    assert transport_log[0]["action"] == "message"

    await runtime.shutdown()
    await task


# ---------------------------------------------------------------------------
# 2. cc actor sends reply -> feishu actor receives via transport
# ---------------------------------------------------------------------------


async def test_end_to_end_cc_reply():
    """cc actor sends reply, feishu actor receives via transport."""
    runtime = ActorRuntime()
    feishu_log: list[dict] = []

    def mock_feishu_transport(actor, payload):
        feishu_log.append(payload)

    runtime.register_transport_handler("feishu_chat", mock_feishu_transport)

    runtime.spawn(
        "feishu:test_app:oc_xxx",
        handler="feishu_inbound",
        tag="DM",
        downstream=["cc:linyilun.root"],
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_xxx"}),
    )
    runtime.spawn(
        "cc:linyilun.root",
        handler="cc_session",
        tag="root",
        downstream=["feishu:test_app:oc_xxx"],
    )

    task = asyncio.create_task(runtime.run())
    runtime.send(
        "cc:linyilun.root",
        Message(
            sender="cc:linyilun.root",
            payload={"text": "world"},
        ),
    )
    await asyncio.sleep(0.2)

    assert len(feishu_log) == 1
    assert "world" in feishu_log[0].get("text", "")

    await runtime.shutdown()
    await task


# ---------------------------------------------------------------------------
# 3. Round-trip: feishu -> cc -> reply back to feishu
# ---------------------------------------------------------------------------


async def test_round_trip():
    """Full round-trip: feishu message -> cc actor -> cc replies -> feishu transport."""
    runtime = ActorRuntime()
    ws_log: list[dict] = []
    feishu_log: list[dict] = []

    async def mock_ws(actor, payload):
        ws_log.append(payload)
        # Simulate CC replying immediately after receiving a message
        runtime.send(
            actor.address,
            Message(
                sender=actor.address,
                payload={"text": f"echo: {payload.get('text', '')}"},
            ),
        )

    async def mock_feishu(actor, payload):
        feishu_log.append(payload)

    runtime.register_transport_handler("websocket", mock_ws)
    runtime.register_transport_handler("feishu_chat", mock_feishu)

    runtime.spawn(
        "feishu:test_app:oc_round",
        handler="feishu_inbound",
        tag="DM",
        downstream=["cc:user.root"],
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_round"}),
    )
    runtime.spawn(
        "cc:user.root",
        handler="cc_session",
        tag="root",
        downstream=["feishu:test_app:oc_round"],
        transport=Transport(type="websocket", config={}),
    )

    task = asyncio.create_task(runtime.run())
    runtime.send(
        "feishu:test_app:oc_round",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "ping", "chat_id": "oc_round", "message_id": "om_1", "file_path": ""},
            metadata={"chat_id": "oc_round"},
        ),
    )
    await asyncio.sleep(0.3)

    # CC should have received the message
    assert len(ws_log) == 1
    assert ws_log[0]["text"] == "ping"

    # Feishu transport receives: ack_react (inbound ACK) + remove_ack + reply text (outbound)
    non_ack_sends = [p for p in feishu_log if p.get("action") != "ack_react"]
    reply_sends = [p for p in non_ack_sends if p.get("action") != "remove_ack"]
    assert len(reply_sends) >= 1
    assert "echo: ping" in reply_sends[0].get("text", "")

    await runtime.shutdown()
    await task


# ---------------------------------------------------------------------------
# 4. Suspended actor does not process messages until resumed
# ---------------------------------------------------------------------------


async def test_suspended_actor_no_processing():
    """Suspended actor queues messages; they are delivered after attach."""
    runtime = ActorRuntime()
    transport_log: list[dict] = []

    def mock_ws(actor, payload):
        transport_log.append(payload)

    runtime.register_transport_handler("websocket", mock_ws)

    # feishu actor active, cc actor starts suspended
    runtime.spawn(
        "feishu:test_app:oc_sus",
        handler="feishu_inbound",
        tag="DM",
        downstream=["cc:user.suspended"],
    )
    runtime.spawn(
        "cc:user.suspended",
        handler="cc_session",
        tag="test",
        state="suspended",
        downstream=["feishu:test_app:oc_sus"],
    )

    task = asyncio.create_task(runtime.run())

    # Send a message — cc actor is suspended, so no transport push yet
    runtime.send(
        "feishu:test_app:oc_sus",
        Message(
            sender="feishu_user:testuser",
            payload={"msg_type": "text", "text": "queued", "chat_id": "oc_sus", "message_id": "om_1", "file_path": ""},
        ),
    )
    await asyncio.sleep(0.15)
    assert len(transport_log) == 0

    # Attach transport -> resumes actor
    runtime.attach(
        "cc:user.suspended",
        Transport(type="websocket", config={}),
    )
    await asyncio.sleep(0.2)

    # Now the queued message should have been processed
    assert len(transport_log) == 1
    assert transport_log[0]["text"] == "queued"

    await runtime.shutdown()
    await task


# ---------------------------------------------------------------------------
# 5. ChannelServerApp start/stop lifecycle
# ---------------------------------------------------------------------------


async def test_app_start_stop(tmp_path):
    """ChannelServerApp starts, writes pidfile, and stops cleanly."""
    from channel_server.app import ChannelServerApp

    app = ChannelServerApp(
        feishu_enabled=False,
        port=0,
    )
    # Override paths to use tmp_path
    app.actors_file = tmp_path / "actors.json"
    app.pidfile = tmp_path / "channel-server.pid"

    await app.start()
    assert app.pidfile.exists()
    assert app.cc_adapter.port > 0

    await app.stop()
    assert not app.pidfile.exists()
