"""Tests for Handler protocol and built-in handlers."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from channel_server.core.actor import Actor, Message, Send, TransportSend, UpdateActor
from channel_server.core.handler import (
    AdminHandler,
    CCSessionHandler,
    FeishuInboundHandler,
    ForwardAllHandler,
    ToolCardHandler,
    get_handler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_actor(
    address: str = "actor://test",
    tag: str = "test",
    handler: str = "feishu_inbound",
    downstream: list[str] | None = None,
    metadata: dict | None = None,
) -> Actor:
    return Actor(
        address=address,
        tag=tag,
        handler=handler,
        downstream=downstream or [],
        metadata=metadata or {},
    )


def make_msg(
    sender: str = "actor://sender",
    payload: dict | None = None,
) -> Message:
    return Message(sender=sender, payload=payload or {})


# ---------------------------------------------------------------------------
# 1. FeishuInboundHandler — single downstream
# ---------------------------------------------------------------------------

def test_feishu_inbound_single_downstream():
    actor = make_actor(downstream=["actor://ds1"])
    msg = make_msg(sender="feishu_user:u123")
    actions = FeishuInboundHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://ds1"
    assert actions[0].message is msg


# ---------------------------------------------------------------------------
# 2. FeishuInboundHandler — multiple downstream
# ---------------------------------------------------------------------------

def test_feishu_inbound_multiple_downstream():
    actor = make_actor(downstream=["actor://ds1", "actor://ds2", "actor://ds3"])
    msg = make_msg(sender="feishu_user:u123")
    actions = FeishuInboundHandler().handle(actor, msg)
    assert len(actions) == 3
    targets = {a.to for a in actions}
    assert targets == {"actor://ds1", "actor://ds2", "actor://ds3"}
    for action in actions:
        assert isinstance(action, Send)
        assert action.message is msg


# ---------------------------------------------------------------------------
# 3. CCSessionHandler — external message → TransportSend with method
# ---------------------------------------------------------------------------

def test_cc_session_external_message():
    actor = make_actor(address="actor://cc", tag="session")
    msg = make_msg(sender="actor://feishu", payload={"text": "hello"})
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], TransportSend)
    assert actions[0].payload["action"] == "message"
    assert actions[0].payload["text"] == "hello"


# ---------------------------------------------------------------------------
# 4. CCSessionHandler — reply with tag prefix (no action = default reply)
# ---------------------------------------------------------------------------

def test_cc_session_reply_with_tag():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"text": "Hello world"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "actor://feishu1"
    assert action.message.payload["text"] == "[session-1] Hello world"


def test_cc_session_reply_skips_tag_for_root():
    actor = make_actor(
        address="actor://cc",
        tag="root",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"text": "Hello world"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.message.payload["text"] == "Hello world"


# ---------------------------------------------------------------------------
# 5. CCSessionHandler — forward → Send to target
# ---------------------------------------------------------------------------

def test_cc_session_forward():
    actor = make_actor(address="actor://cc", tag="session-1")
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "forward", "target": "actor://other", "text": "forwarded"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "actor://other"
    assert action.message is msg


# ---------------------------------------------------------------------------
# 6. CCSessionHandler — send_summary → Send to parent_feishu via runtime lookup
# ---------------------------------------------------------------------------

def test_cc_session_send_summary():
    # Build a mock runtime with parent actor that has a feishu downstream
    parent_actor = make_actor(
        address="actor://cc-parent",
        downstream=["feishu:oc_parent_chat", "other://something"],
    )
    runtime = MagicMock()
    runtime.lookup.return_value = parent_actor

    actor = make_actor(address="actor://cc", tag="session-1")
    actor.parent = "actor://cc-parent"

    msg = make_msg(
        sender="actor://cc",
        payload={
            "action": "send_summary",
            "text": "Summary text",
        },
    )
    handler = CCSessionHandler(runtime=runtime)
    actions = handler.handle(actor, msg)
    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, Send)
    assert action.to == "feishu:oc_parent_chat"
    assert action.message is msg


def test_cc_session_send_summary_no_parent():
    """send_summary with no parent actor returns empty (no-op)."""
    actor = make_actor(address="actor://cc", tag="session-1")
    # actor.parent is None by default

    msg = make_msg(
        sender="actor://cc",
        payload={"action": "send_summary", "text": "Summary text"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert actions == []


def test_cc_session_send_summary_no_runtime():
    """send_summary with parent set but no runtime returns empty (no-op)."""
    actor = make_actor(address="actor://cc", tag="session-1")
    actor.parent = "actor://cc-parent"

    msg = make_msg(
        sender="actor://cc",
        payload={"action": "send_summary", "text": "Summary text"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert actions == []


# ---------------------------------------------------------------------------
# 7. CCSessionHandler — send_file → Send to downstream (catch-all)
# ---------------------------------------------------------------------------

def test_cc_session_send_file():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "send_file", "chat_id": "c1", "file_path": "/tmp/test.pdf"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"


# ---------------------------------------------------------------------------
# 8. CCSessionHandler — react → Send to downstream (catch-all)
# ---------------------------------------------------------------------------

def test_cc_session_react():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "react", "message_id": "m1", "emoji_type": "HEART"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"


# ---------------------------------------------------------------------------
# 9. CCSessionHandler — update_title → Send to downstream (catch-all)
# ---------------------------------------------------------------------------

def test_cc_session_update_title():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1", "actor://feishu2"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "update_title", "title": "New Title"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 2
    for action in actions:
        assert isinstance(action, Send)
    targets = {a.to for a in actions}
    assert targets == {"actor://feishu1", "actor://feishu2"}


# ---------------------------------------------------------------------------
# 10. CCSessionHandler — tool_notify → Send to tool_card:*
# ---------------------------------------------------------------------------

def test_cc_session_tool_notify():
    actor = make_actor(address="cc:user.dev", tag="root")
    msg = make_msg(
        sender="cc:user.dev",
        payload={"action": "tool_notify", "text": "Running tests..."},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "tool_card:user.dev"
    assert actions[0].message is msg


# ---------------------------------------------------------------------------
# 11. CCSessionHandler — unknown action sends to downstream
# ---------------------------------------------------------------------------

def test_cc_session_unknown_action_sends_downstream():
    actor = make_actor(
        address="actor://cc",
        tag="session-1",
        downstream=["actor://feishu1"],
    )
    msg = make_msg(
        sender="actor://cc",
        payload={"action": "nonexistent"},
    )
    actions = CCSessionHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "actor://feishu1"


# ---------------------------------------------------------------------------
# 12. ForwardAllHandler — broadcasts to all downstream
# ---------------------------------------------------------------------------

def test_forward_all_broadcasts():
    actor = make_actor(downstream=["actor://a", "actor://b"])
    msg = make_msg()
    actions = ForwardAllHandler().handle(actor, msg)
    assert len(actions) == 2
    targets = {a.to for a in actions}
    assert targets == {"actor://a", "actor://b"}
    for action in actions:
        assert isinstance(action, Send)
        assert action.message is msg


# ---------------------------------------------------------------------------
# 13. ToolCardHandler — accumulates history (max 5), emits tool_notify
# ---------------------------------------------------------------------------

def test_tool_card_accumulates_history():
    actor = make_actor(metadata={"history": ["a", "b", "c", "d", "e"]})
    msg = make_msg(payload={"text": "f"})
    actions = ToolCardHandler().handle(actor, msg)

    update = next(a for a in actions if isinstance(a, UpdateActor))
    transport = next(a for a in actions if isinstance(a, TransportSend))

    # history trimmed to last 5
    assert update.changes["metadata"]["history"] == ["b", "c", "d", "e", "f"]
    assert transport.payload["action"] == "tool_notify"
    assert "f" in transport.payload["text"]
    # oldest entry dropped
    assert "a" not in transport.payload["text"]


def test_tool_card_starts_empty():
    actor = make_actor()
    msg = make_msg(payload={"text": "first"})
    actions = ToolCardHandler().handle(actor, msg)

    update = next(a for a in actions if isinstance(a, UpdateActor))
    assert update.changes["metadata"]["history"] == ["first"]


def test_tool_card_includes_card_msg_id():
    actor = make_actor(metadata={"card_msg_id": "om_card_123"})
    msg = make_msg(payload={"text": "Running tests"})
    actions = ToolCardHandler().handle(actor, msg)

    transport = next(a for a in actions if isinstance(a, TransportSend))
    assert transport.payload["card_msg_id"] == "om_card_123"


def test_tool_card_empty_card_msg_id():
    actor = make_actor(metadata={})
    msg = make_msg(payload={"text": "Running tests"})
    actions = ToolCardHandler().handle(actor, msg)

    transport = next(a for a in actions if isinstance(a, TransportSend))
    assert transport.payload["card_msg_id"] == ""


# ---------------------------------------------------------------------------
# 14 & 15. get_handler — returns correct types / raises on unknown
# ---------------------------------------------------------------------------

def test_get_handler_returns_correct_types():
    assert isinstance(get_handler("feishu_inbound"), FeishuInboundHandler)
    assert isinstance(get_handler("cc_session"), CCSessionHandler)
    assert isinstance(get_handler("forward_all"), ForwardAllHandler)
    assert isinstance(get_handler("tool_card"), ToolCardHandler)


def test_get_handler_raises_on_unknown():
    with pytest.raises(ValueError, match="Unknown handler"):
        get_handler("does_not_exist")


# ---------------------------------------------------------------------------
# 16. AdminHandler — /help
# ---------------------------------------------------------------------------

def test_admin_help_command():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    msg = make_msg(sender="feishu_user:u1", payload={"text": "/help"})
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].to == "cc:user.root"
    assert "/help" in actions[0].message.payload["text"]
    assert "/spawn" in actions[0].message.payload["text"]


# ---------------------------------------------------------------------------
# 17. AdminHandler — unknown command
# ---------------------------------------------------------------------------

def test_admin_unknown_command():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    msg = make_msg(sender="feishu_user:u1", payload={"text": "/foobar"})
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert "/foobar" in actions[0].message.payload["text"]


# ---------------------------------------------------------------------------
# 18. AdminHandler — session command passthrough
# ---------------------------------------------------------------------------

def test_admin_session_command_passthrough():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    for cmd in ("/spawn research", "/kill research", "/sessions"):
        msg = make_msg(sender="feishu_user:u1", payload={"text": cmd})
        actions = AdminHandler().handle(actor, msg)
        assert len(actions) == 1
        assert isinstance(actions[0], Send)
        assert actions[0].to == "cc:user.root"
        assert actions[0].message is msg


# ---------------------------------------------------------------------------
# 19. AdminHandler — system notification forward (msg_type in payload)
# ---------------------------------------------------------------------------

def test_admin_system_notification_forward():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root", "feishu:chat1"],
    )
    msg = make_msg(
        sender="system:runtime",
        payload={"msg_type": "system", "text": "server online"},
    )
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 2
    targets = {a.to for a in actions}
    assert targets == {"cc:user.root", "feishu:chat1"}
    for action in actions:
        assert isinstance(action, Send)
        assert action.message is msg


# ---------------------------------------------------------------------------
# 20. AdminHandler — non-slash message passthrough
# ---------------------------------------------------------------------------

def test_admin_non_slash_passthrough():
    actor = make_actor(
        address="system:admin",
        handler="admin",
        downstream=["cc:user.root"],
    )
    msg = make_msg(sender="feishu_user:u1", payload={"text": "hello there"})
    actions = AdminHandler().handle(actor, msg)
    assert len(actions) == 1
    assert isinstance(actions[0], Send)
    assert actions[0].message is msg


# ---------------------------------------------------------------------------
# 21. get_handler returns admin handler
# ---------------------------------------------------------------------------

def test_get_handler_returns_admin():
    assert isinstance(get_handler("admin"), AdminHandler)


# ---------------------------------------------------------------------------
# 22. FeishuInboundHandler.on_stop — thread actor emits unpin + update_anchor
# ---------------------------------------------------------------------------

def test_feishu_inbound_on_stop_thread_actor():
    from channel_server.core.actor import Transport

    actor = make_actor(
        address="feishu:oc_test:om_anchor123",
        tag="dev-session",
        handler="feishu_inbound",
    )
    actor.transport = Transport(
        type="feishu_thread",
        config={"chat_id": "oc_test", "root_id": "om_anchor123"},
    )

    actions = FeishuInboundHandler().on_stop(actor)
    assert len(actions) == 2

    unpin = actions[0]
    assert isinstance(unpin, TransportSend)
    assert unpin.payload["action"] == "unpin"
    assert unpin.payload["message_id"] == "om_anchor123"

    update = actions[1]
    assert isinstance(update, TransportSend)
    assert update.payload["action"] == "update_anchor"
    assert "ended" in update.payload["title"]
    assert update.payload["template"] == "red"


def test_feishu_inbound_on_stop_chat_actor_noop():
    """Chat actors (not thread) should not emit cleanup actions."""
    from channel_server.core.actor import Transport

    actor = make_actor(handler="feishu_inbound")
    actor.transport = Transport(type="feishu_chat", config={"chat_id": "oc_test"})

    actions = FeishuInboundHandler().on_stop(actor)
    assert actions == []


# ---------------------------------------------------------------------------
# 23. CCSessionHandler.on_stop — stops child actors
# ---------------------------------------------------------------------------

def test_cc_session_on_stop_stops_children():
    from channel_server.core.actor import StopActor

    actor = make_actor(
        address="cc:user.dev",
        tag="dev",
        handler="cc_session",
        downstream=["feishu:oc_test:om_anchor"],
    )

    actions = CCSessionHandler().on_stop(actor)
    stop_addrs = {a.address for a in actions if isinstance(a, StopActor)}
    assert "tool_card:user.dev" in stop_addrs
    assert "feishu:oc_test:om_anchor" in stop_addrs


# ---------------------------------------------------------------------------
# 24. ToolCardHandler.on_stop — emits final card update
# ---------------------------------------------------------------------------

def test_tool_card_on_stop_updates_card():
    from channel_server.core.actor import Transport

    actor = make_actor(
        handler="tool_card",
        metadata={"card_msg_id": "om_card_999"},
    )
    actor.transport = Transport(type="feishu_chat", config={"chat_id": "oc_test"})

    actions = ToolCardHandler().on_stop(actor)
    assert len(actions) == 1
    assert isinstance(actions[0], TransportSend)
    assert actions[0].payload["card_msg_id"] == "om_card_999"
    assert "ended" in actions[0].payload["text"].lower()


def test_tool_card_on_stop_no_transport_noop():
    actor = make_actor(handler="tool_card", metadata={"card_msg_id": "om_card_999"})
    actions = ToolCardHandler().on_stop(actor)
    assert actions == []
