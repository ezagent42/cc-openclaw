"""Feishu inbound message handler."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, TransportSend, UpdateActor


class FeishuInboundHandler:
    """Route messages for a Feishu chat/thread actor.

    Inbound (from feishu_user:*):
      - Check echo prevention (skip if msg_id in sent_msg_ids)
      - ACK react via TransportSend
      - Update ack_msg_id in metadata
      - Forward to downstream

    Outbound (from cc:* or others):
      - Remove ACK react for current ack_msg_id
      - Push to Feishu transport
    """

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        if msg.sender.startswith("feishu_user:"):
            return self._handle_inbound(actor, msg)
        return self._handle_outbound(actor, msg)

    def _handle_inbound(self, actor: Actor, msg: Message) -> list[Action]:
        message_id = msg.payload.get("message_id", "")
        sent_ids = actor.metadata.get("sent_msg_ids", [])
        if message_id and message_id in sent_ids:
            return []
        actions: list[Action] = []

        # Record chat_id mapping for DMs
        chat_type = msg.metadata.get("chat_type", "")
        user_id = msg.metadata.get("user_id", "")
        chat_id = msg.payload.get("chat_id", "")
        if chat_type == "p2p" and user_id and chat_id:
            chat_map = dict(actor.metadata.get("chat_id_map", {}))
            if user_id not in chat_map:
                chat_map[user_id] = chat_id
                actions.append(UpdateActor(changes={"metadata": {"chat_id_map": chat_map}}))

        if message_id:
            actions.append(UpdateActor(changes={"metadata": {"ack_msg_id": message_id}}))
            actions.append(TransportSend(payload={"action": "ack_react", "message_id": message_id}))
        for addr in actor.downstream:
            actions.append(Send(to=addr, message=msg))
        return actions

    def _handle_outbound(self, actor: Actor, msg: Message) -> list[Action]:
        actions: list[Action] = []
        ack_msg_id = actor.metadata.get("ack_msg_id", "")
        ack_reaction_id = actor.metadata.get("ack_reaction_id", "")
        if ack_msg_id:
            actions.append(TransportSend(payload={
                "action": "remove_ack",
                "message_id": ack_msg_id,
                "reaction_id": ack_reaction_id,
            }))
            actions.append(UpdateActor(changes={"metadata": {"ack_msg_id": "", "ack_reaction_id": ""}}))
        if actor.transport is not None:
            actions.append(TransportSend(payload=msg.payload))
        return actions

    def on_spawn(self, actor: Actor) -> list[Action]:
        """Create thread anchor + tool card for child sessions."""
        mode = actor.metadata.get("mode", "")
        if mode != "child":
            return []

        chat_id = actor.metadata.get("chat_id", "")
        tag = actor.metadata.get("tag", "")

        return [
            TransportSend(payload={
                "action": "create_thread_anchor",
                "chat_id": chat_id,
                "tag": tag,
            }),
            TransportSend(payload={
                "action": "create_tool_card",
                "chat_id": chat_id,
                "tag": tag,
            }),
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        actions: list[Action] = []
        if actor.transport is None or actor.transport.type != "feishu_thread":
            return actions
        anchor_msg_id = actor.transport.config.get("root_id", "")
        if anchor_msg_id:
            actions.append(TransportSend(payload={
                "action": "unpin", "message_id": anchor_msg_id,
            }))
            actions.append(TransportSend(payload={
                "action": "update_anchor", "msg_id": anchor_msg_id,
                "title": f"\U0001f534 [{actor.tag}] ended",
                "body_text": f"Session [{actor.tag}] has been terminated",
                "template": "red",
            }))
        return actions
