"""CCSessionHandler — bridges actor messages and a Claude Code session."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send, StopActor, TransportSend


class CCSessionHandler:
    """Bridge between actor messages and a Claude Code session.

    - External message (sender != actor.address): push to CC via TransportSend.
    - CC-originated (sender == actor.address): dispatch by payload action.
    """

    def __init__(self, runtime=None):
        self._runtime = runtime

    def set_runtime(self, runtime) -> None:
        """Inject the actor runtime after construction."""
        self._runtime = runtime

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
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
            parent_feishu = ""
            if actor.parent and self._runtime:
                parent = self._runtime.lookup(actor.parent)
                if parent:
                    parent_feishu = next(
                        (d for d in parent.downstream if d.startswith("feishu:")), ""
                    )
            if parent_feishu:
                return [Send(to=parent_feishu, message=msg)]
            return []

        if action == "tool_notify":
            text_msg = Message(sender=msg.sender, payload={"text": msg.payload.get("text", "")})
            return [Send(to=addr, message=text_msg) for addr in actor.downstream]

        # Catch-all (react, send_file, update_title, etc.) → send to downstream.
        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_spawn(self, actor: Actor) -> list[Action]:
        """Spawn tmux process for child sessions."""
        parts = actor.address.replace("cc:", "").split(".")
        if len(parts) < 2 or parts[1] == "root":
            return []

        user = parts[0]
        session_name = parts[1]
        tag = actor.metadata.get("tag", session_name)
        chat_id = actor.metadata.get("chat_id", "")

        return [
            TransportSend(payload={
                "action": "spawn_tmux",
                "user": user,
                "session_name": session_name,
                "tag": tag,
                "chat_id": chat_id,
            }),
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        """Stop child actors (feishu_thread) when CC session ends."""
        actions: list[Action] = []
        for addr in actor.downstream:
            actions.append(StopActor(address=addr))
        return actions
