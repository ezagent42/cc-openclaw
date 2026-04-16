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
async def test_session_lifecycle_via_session_mgr():
    """Full lifecycle: spawn via session-mgr, verify actors, kill."""
    rt = ActorRuntime()

    # Register transport handlers (mock — don't actually call feishu/tmux)
    transport_log = []

    async def mock_feishu_thread(actor, payload):
        transport_log.append(("feishu_thread", payload))
        if payload.get("action") == "create_thread_anchor":
            return {"anchor_msg_id": "om_anchor_test"}
        if payload.get("action") == "create_tool_card":
            return {"card_msg_id": "om_card_test"}
        return None

    async def mock_ws(actor, payload):
        transport_log.append(("ws", payload))
        if payload.get("action") == "spawn_tmux":
            return {"tmux_started": True}
        return None

    async def mock_feishu_chat(actor, payload):
        transport_log.append(("feishu_chat", payload))
        return None

    rt.register_transport_handler("feishu_thread", mock_feishu_thread)
    rt.register_transport_handler("websocket", mock_ws)
    rt.register_transport_handler("feishu_chat", mock_feishu_chat)

    # Spawn session-mgr and start the runtime loop
    rt.spawn("system:session-mgr", "session_mgr", tag="session-mgr")
    task = asyncio.create_task(rt.run())

    # Simulate /spawn dev command
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

    # Let the runtime process
    await asyncio.sleep(0.5)

    # Verify: feishu thread actor created
    thread_actor = rt.lookup("feishu:test_app:oc_chat1:thread:dev")
    assert thread_actor is not None

    # Verify: cc actor created (suspended, waiting for WS)
    cc_actor = rt.lookup("cc:alice.dev")
    assert cc_actor is not None
    assert cc_actor.state == "suspended"

    # Verify: feishu thread on_spawn hooks ran (create_thread_anchor via feishu_thread transport)
    anchor_calls = [c for c in transport_log if c[1].get("action") == "create_thread_anchor"]
    assert len(anchor_calls) >= 1

    # Verify: cc on_spawn fires spawn_tmux — only captured if cc actor has a websocket transport.
    # The cc actor is spawned suspended with no transport, so spawn_tmux goes via the transport
    # path which is a no-op (no transport attached yet).  After attaching, spawn_tmux is NOT
    # re-triggered, so we confirm the cc actor *would* have sent it by checking the action
    # exists in on_spawn output directly.
    from channel_server.core.handler import get_handler
    cc_handler = get_handler("cc_session")
    on_spawn_actions = cc_handler.on_spawn(cc_actor)
    assert len(on_spawn_actions) == 1
    assert on_spawn_actions[0].payload["action"] == "spawn_tmux"

    # Kill the session
    rt.send(
        "system:session-mgr",
        Message(
            sender="system:admin",
            payload={
                "text": "/kill dev",
                "user": "alice",
                "chat_id": "oc_chat1",
                "app_id": "test_app",
            },
        ),
    )

    await asyncio.sleep(0.5)

    cc_actor_after = rt.lookup("cc:alice.dev")
    assert cc_actor_after is None or cc_actor_after.state == "ended"

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
