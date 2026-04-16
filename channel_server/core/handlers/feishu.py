"""FeishuInboundHandler — routes messages for a Feishu chat/thread actor."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, TransportSend, Send


class FeishuInboundHandler:
    """Route messages for a Feishu chat/thread actor.

    - Messages from external users (feishu_user:*) -> forward to downstream CC actors.
    - Messages from CC actors (cc:* or others) -> push to Feishu transport (outbound reply).
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender.startswith("feishu_user:"):
            # Inbound from Feishu user -> forward to downstream (CC actors)
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Outbound reply from CC -> push via transport to Feishu chat/thread
        actions: list[Action] = []
        if actor.transport is not None:
            actions.append(TransportSend(payload=msg.payload))
        return actions

    def on_stop(self, actor: Actor) -> list[Action]:
        """Cleanup: unpin anchor message and update card to 'ended'.

        Only applies to thread actors (feishu_thread transport) that have
        an anchor message (root_id in transport config).
        """
        actions: list[Action] = []
        if actor.transport is None or actor.transport.type != "feishu_thread":
            return actions
        anchor_msg_id = actor.transport.config.get("root_id", "")
        if anchor_msg_id:
            actions.append(TransportSend(payload={
                "action": "unpin",
                "message_id": anchor_msg_id,
            }))
            actions.append(TransportSend(payload={
                "action": "update_anchor",
                "msg_id": anchor_msg_id,
                "title": f"\U0001f534 [{actor.tag}] ended",
                "body_text": f"Session [{actor.tag}] has been terminated",
                "template": "red",
            }))
        return actions
