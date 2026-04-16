"""Tests for CCAdapter — registration, message routing, disconnect, push, auto-spawn."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime
from channel_server.adapters.cc.adapter import CCAdapter, _read_tmux_session_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_adapter() -> tuple[CCAdapter, ActorRuntime]:
    rt = ActorRuntime()
    adapter = CCAdapter(rt, host="127.0.0.1", port=0)
    return adapter, rt


def make_ws(instance_id: str = "alice.root") -> AsyncMock:
    """Create a mock WebSocket with send/recv capabilities."""
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# 1. _handle_register attaches transport to a pre-spawned suspended actor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_register_attaches_transport():
    adapter, rt = make_adapter()
    ws = make_ws()

    # Pre-spawn CC actor as suspended (simulating a child that was spawned by _handle_spawn)
    rt.spawn(
        "cc:alice.root",
        "cc_session",
        tag="root",
        state="suspended",
    )

    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
        "tag_name": "root",
    })

    actor = rt.lookup("cc:alice.root")
    assert actor is not None
    assert actor.state == "active"
    assert actor.transport is not None
    assert actor.transport.type == "websocket"
    assert actor.transport.config["instance_id"] == "alice.root"

    # Verify registered ack was sent
    ws.send.assert_called_once()
    ack = json.loads(ws.send.call_args[0][0])
    assert ack["method"] == "registered"
    assert ack["address"] == "cc:alice.root"


# ---------------------------------------------------------------------------
# 2. handle_message routes reply to actor mailbox
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_reply_sends_to_actor():
    adapter, rt = make_adapter()
    ws = make_ws()

    # Register a CC actor
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })

    # Send a reply message
    await adapter.handle_message(ws, {
        "method": "reply",
        "chat_id": "oc_abc",
        "text": "hello world",
    })

    mailbox = rt.mailboxes.get("cc:alice.root")
    assert mailbox is not None
    assert not mailbox.empty()
    msg = mailbox.get_nowait()
    assert isinstance(msg, Message)
    assert msg.payload.get("text") == "hello world"
    assert msg.payload.get("action") is None  # reply -> no action (default send text)


# ---------------------------------------------------------------------------
# 3. handle_disconnect detaches transport and suspends actor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_disconnect_detaches():
    adapter, rt = make_adapter()
    ws = make_ws()

    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })

    actor = rt.lookup("cc:alice.root")
    assert actor is not None
    assert actor.state == "active"

    # Disconnect
    adapter.handle_disconnect(ws)

    assert actor.state == "suspended"
    assert actor.transport is None
    # Verify ws mapping is cleaned up
    assert id(ws) not in adapter._ws_to_address
    assert "cc:alice.root" not in adapter._address_to_ws


# ---------------------------------------------------------------------------
# 4. push_to_cc sends payload via WebSocket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_to_cc_sends_via_ws():
    adapter, rt = make_adapter()
    ws = make_ws()

    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })

    actor = rt.lookup("cc:alice.root")
    assert actor is not None

    # Reset send mock after registration ack
    ws.send.reset_mock()

    payload = {"method": "message", "text": "incoming from feishu", "chat_id": "oc_abc"}
    adapter.push_to_cc(actor, payload)

    # push_to_cc uses asyncio.ensure_future(ws.send(...))
    # Give the event loop a tick to execute
    await asyncio.sleep(0)

    ws.send.assert_called_once()
    sent = json.loads(ws.send.call_args[0][0])
    assert sent["method"] == "message"
    assert sent["text"] == "incoming from feishu"


# ---------------------------------------------------------------------------
# 5. _handle_register auto-spawns when actor doesn't exist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_register_auto_spawns():
    adapter, rt = make_adapter()
    ws = make_ws()

    # Don't pre-spawn — register should auto-spawn
    assert rt.lookup("cc:bob.root") is None

    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "bob.root",
        "tag_name": "root",
    })

    actor = rt.lookup("cc:bob.root")
    assert actor is not None
    assert actor.state == "active"
    assert actor.handler == "cc_session"
    assert actor.tag == "root"
    assert actor.transport is not None
    assert actor.transport.type == "websocket"


# ---------------------------------------------------------------------------
# 6. _handle_register rejects missing instance_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_register_rejects_missing_instance_id():
    adapter, rt = make_adapter()
    ws = make_ws()

    await adapter._handle_register(ws, {"method": "register"})

    ws.send.assert_called_once()
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "error"
    assert "instance_id" in resp["message"].lower()


# ---------------------------------------------------------------------------
# 7. _handle_list returns sessions for the user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_list_returns_sessions():
    adapter, rt = make_adapter()
    ws = make_ws()

    # Register root
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
    })

    # Manually spawn a child
    rt.spawn("cc:alice.dev", "cc_session", tag="dev", state="suspended")

    ws.send.reset_mock()
    await adapter._handle_list(ws, {"method": "list_sessions"})

    ws.send.assert_called_once()
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "sessions_list"
    names = [s["name"] for s in resp["sessions"]]
    assert "root" in names
    assert "dev" in names


# ---------------------------------------------------------------------------
# 8. handle_message ignores pong
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_ignores_pong():
    adapter, rt = make_adapter()
    ws = make_ws()

    # This should not raise or send anything
    await adapter.handle_message(ws, {"method": "pong"})
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# 9. _read_tmux_session_name reads from cc-openclaw.sh
# ---------------------------------------------------------------------------

def test_read_tmux_session_name():
    name = _read_tmux_session_name()
    assert name == "cc-openclaw"


# ---------------------------------------------------------------------------
# 10. spawn_cc_process returns False when tmux fails
# ---------------------------------------------------------------------------

def test_spawn_cc_process_returns_false_on_failure():
    adapter, _ = make_adapter()
    with patch("channel_server.adapters.cc.adapter.subprocess") as mock_sub:
        mock_sub.run.side_effect = Exception("tmux not found")
        result = adapter.spawn_cc_process("alice", "test-session")
        assert result is False


def test_spawn_cc_process_returns_true_on_success():
    adapter, _ = make_adapter()
    with patch("channel_server.adapters.cc.adapter.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0)
        result = adapter.spawn_cc_process("alice", "test-session")
        assert result is True


# ---------------------------------------------------------------------------
# 11. _handle_spawn rolls back actors on tmux failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_spawn_rollback_on_failure():
    adapter, rt = make_adapter()
    ws = make_ws()

    # Register root session
    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
        "tag_name": "root",
    })

    ws.send.reset_mock()

    # Mock spawn_cc_process to fail
    with patch.object(adapter, "spawn_cc_process", return_value=False):
        await adapter._handle_spawn(ws, {
            "method": "spawn",
            "session_name": "broken-child",
        })

    # spawn_result should report failure
    ws.send.assert_called_once()
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "spawn_result"
    assert resp["ok"] is False
    assert "failed" in resp["text"].lower()

    # CC actor should be cleaned up (state == ended)
    child = rt.lookup("cc:alice.broken-child")
    assert child is None or child.state == "ended"


@pytest.mark.asyncio
async def test_handle_spawn_success():
    adapter, rt = make_adapter()
    ws = make_ws()

    await adapter._handle_register(ws, {
        "method": "register",
        "instance_id": "alice.root",
        "tag_name": "root",
    })

    ws.send.reset_mock()

    with patch.object(adapter, "spawn_cc_process", return_value=True):
        await adapter._handle_spawn(ws, {
            "method": "spawn",
            "session_name": "good-child",
        })

    ws.send.assert_called_once()
    resp = json.loads(ws.send.call_args[0][0])
    assert resp["method"] == "spawn_result"
    assert resp["ok"] is True

    child = rt.lookup("cc:alice.good-child")
    assert child is not None
    assert child.state == "suspended"
