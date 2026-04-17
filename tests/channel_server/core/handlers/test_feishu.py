"""Tests for FeishuInboundHandler — ACK, echo prevention, serial correctness."""
from __future__ import annotations

from channel_server.core.actor import Actor, Message, Send, Transport, TransportSend, UpdateActor
from channel_server.core.handlers.feishu import FeishuInboundHandler


def make_actor(**kwargs) -> Actor:
    defaults = dict(address="feishu:test_app:oc_test", tag="test", handler="feishu_inbound",
                    downstream=["system:admin"], metadata={})
    defaults.update(kwargs)
    return Actor(**defaults)


def test_inbound_emits_ack_and_forward():
    handler = FeishuInboundHandler()
    actor = make_actor(transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}))
    msg = Message(sender="feishu_user:u1", payload={"message_id": "om_1", "text": "hi"})
    actions = handler.handle(actor, msg)
    updates = [a for a in actions if isinstance(a, UpdateActor)]
    acks = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") == "ack_react"]
    sends = [a for a in actions if isinstance(a, Send)]
    assert len(updates) == 1
    assert updates[0].changes["metadata"]["ack_msg_id"] == "om_1"
    assert len(acks) == 1
    assert acks[0].payload["message_id"] == "om_1"
    assert len(sends) >= 1


def test_outbound_removes_ack_and_sends():
    handler = FeishuInboundHandler()
    actor = make_actor(
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}),
        metadata={"ack_msg_id": "om_1", "ack_reaction_id": "r_1"},
    )
    msg = Message(sender="cc:user.root", payload={"text": "reply"})
    actions = handler.handle(actor, msg)
    removes = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") == "remove_ack"]
    sends = [a for a in actions if isinstance(a, TransportSend) and a.payload.get("action") != "remove_ack"]
    assert len(removes) == 1
    assert removes[0].payload["message_id"] == "om_1"
    assert removes[0].payload["reaction_id"] == "r_1"
    assert len(sends) >= 1


def test_echo_prevention():
    handler = FeishuInboundHandler()
    actor = make_actor(
        metadata={"sent_msg_ids": ["om_echo"]},
        transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}),
    )
    msg = Message(sender="feishu_user:u1", payload={"message_id": "om_echo", "text": "echo"})
    actions = handler.handle(actor, msg)
    assert actions == []


def test_feishu_inbound_on_spawn_child_creates_anchor():
    """on_spawn for child mode creates thread anchor only."""
    from channel_server.core.actor import Transport, TransportSend

    actor = make_actor(
        address="feishu:test_app:oc_test:thread:dev",
        tag="dev",
        handler="feishu_inbound",
        metadata={"chat_id": "oc_test", "tag": "dev", "mode": "child"},
    )
    actor.transport = Transport(type="feishu_thread", config={"chat_id": "oc_test"})

    actions = FeishuInboundHandler().on_spawn(actor)
    assert len(actions) == 1  # only anchor, no tool card

    anchor_action = actions[0]
    assert isinstance(anchor_action, TransportSend)
    assert anchor_action.payload["action"] == "create_thread_anchor"
    assert anchor_action.payload["tag"] == "dev"


def test_feishu_inbound_on_spawn_no_mode_noop():
    """on_spawn without mode metadata returns empty."""
    actor = make_actor(handler="feishu_inbound")
    actions = FeishuInboundHandler().on_spawn(actor)
    assert actions == []


def test_serial_ack_no_interference():
    handler = FeishuInboundHandler()
    actor = make_actor(transport=Transport(type="feishu_chat", config={"chat_id": "oc_test"}))
    msg_a = Message(sender="feishu_user:u1", payload={"message_id": "om_A", "text": "A"})
    actions_a = handler.handle(actor, msg_a)
    for a in actions_a:
        if isinstance(a, UpdateActor):
            actor.metadata.update(a.changes.get("metadata", {}))
    assert actor.metadata["ack_msg_id"] == "om_A"
    msg_b = Message(sender="feishu_user:u1", payload={"message_id": "om_B", "text": "B"})
    actions_b = handler.handle(actor, msg_b)
    for a in actions_b:
        if isinstance(a, UpdateActor):
            actor.metadata.update(a.changes.get("metadata", {}))
    assert actor.metadata["ack_msg_id"] == "om_B"
    reply = Message(sender="cc:user.root", payload={"text": "reply"})
    actions_reply = handler.handle(actor, reply)
    removes = [a for a in actions_reply if isinstance(a, TransportSend) and a.payload.get("action") == "remove_ack"]
    assert len(removes) == 1
    assert removes[0].payload["message_id"] == "om_B"
