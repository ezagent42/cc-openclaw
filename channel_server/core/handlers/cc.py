"""CCSessionHandler — bridges actor messages and a Claude Code session."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, StopActor, TransportSend


class CCSessionHandler:
    """Bridge between actor messages and a Claude Code session.

    - External message (sender != actor.address): push to CC via TransportSend.
    - CC-originated (sender == actor.address): dispatch by payload action.
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender != actor.address:
            # External message — forward to the CC session over its transport.
            # Merge metadata (user, user_id, etc.) into payload so channel.py
            # can inject them into the MCP notification.
            return [TransportSend(payload={**msg.metadata, **msg.payload, "action": "message"})]

        # Message originated from CC itself — dispatch on action.
        action = msg.payload.get("action")

        if action is None:
            # Default reply behaviour — tag prefix if not root.
            text = msg.payload.get("text", "")
            if actor.tag != "root":
                text = f"[{actor.tag}] {text}"
            reply_msg = Message(
                sender=actor.address,
                payload={**msg.payload, "text": text},
            )
            return [Send(to=addr, message=reply_msg) for addr in actor.downstream]

        if action == "forward":
            target = msg.payload.get("target", "")
            return [Send(to=target, message=msg)]

        if action == "send_summary":
            parent_feishu = msg.payload.get("parent_feishu", "")
            return [Send(to=parent_feishu, message=msg)]

        if action == "tool_notify":
            # Route to the tool_card actor for this user session.
            # Actor address is "cc:<user_session>", target is "tool_card:<user_session>".
            user_session = actor.address.removeprefix("cc:")
            return [Send(to=f"tool_card:{user_session}", message=msg)]

        # Catch-all (react, send_file, update_title, etc.) → send to downstream.
        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_stop(self, actor: Actor) -> list[Action]:
        """Stop child actors (feishu_thread, tool_card) when CC session ends."""
        actions: list[Action] = []
        user_session = actor.address.removeprefix("cc:")
        # Stop tool card
        actions.append(StopActor(address=f"tool_card:{user_session}"))
        # Stop downstream feishu thread actors
        for addr in actor.downstream:
            actions.append(StopActor(address=addr))
        return actions
