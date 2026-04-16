"""Handler protocol and built-in handler implementations for the actor model."""
from __future__ import annotations

from typing import Protocol

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    StopActor,
    TransportSend,
    UpdateActor,
)


class Handler(Protocol):
    """Protocol that all actor message handlers must satisfy."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]: ...

    def on_stop(self, actor: Actor) -> list[Action]:
        """Lifecycle callback invoked when an actor is stopped.

        Returns actions to execute as cleanup (e.g. unpin messages,
        update cards, notify parents). Default: no-op.
        """
        return []


# ---------------------------------------------------------------------------
# FeishuInboundHandler
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CCSessionHandler
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ForwardAllHandler
# ---------------------------------------------------------------------------

class ForwardAllHandler:
    """Broadcast every message to all downstream actors (same as FeishuInbound)."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_stop(self, actor: Actor) -> list[Action]:
        return []


# ---------------------------------------------------------------------------
# ToolCardHandler
# ---------------------------------------------------------------------------

class ToolCardHandler:
    """Accumulate a rolling history window (max 5) and emit a tool-card update."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "")
        history: list[str] = list(actor.metadata.get("history", []))
        history.append(text)
        if len(history) > 5:
            history = history[-5:]
        display = "\n".join(history)
        return [
            UpdateActor(changes={"metadata": {"history": history}}),
            TransportSend(payload={
                "action": "tool_notify",
                "text": display,
                "card_msg_id": actor.metadata.get("card_msg_id", ""),
            }),
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        """Clear the tool card on session end."""
        card_msg_id = actor.metadata.get("card_msg_id", "")
        if card_msg_id and actor.transport is not None:
            return [TransportSend(payload={
                "action": "tool_notify",
                "text": f"\u2b1b Session ended",
                "card_msg_id": card_msg_id,
            })]
        return []


# ---------------------------------------------------------------------------
# AdminHandler
# ---------------------------------------------------------------------------

class AdminHandler:
    """Handles admin commands and notifications.

    - System notifications are forwarded to downstream actors.
    - Session commands (/spawn, /kill, /sessions) pass through to downstream.
    - Non-slash messages pass through to downstream.
    - /help shows available commands.
    - Unknown slash commands get an error message.
    """

    SESSION_COMMANDS = ("/spawn", "/kill", "/sessions")

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        text = msg.payload.get("text", "").strip()

        # System notifications -> forward to downstream
        if msg.payload.get("msg_type") == "system":
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Session commands -> pass through to downstream CC actor
        if text.startswith(self.SESSION_COMMANDS):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Non-slash messages -> forward to downstream
        if not text.startswith("/"):
            return [Send(to=addr, message=msg) for addr in actor.downstream]

        # Admin commands
        if text == "/help":
            return [
                Send(
                    to=addr,
                    message=Message(
                        sender=actor.address,
                        payload={"text": self._help_text()},
                    ),
                )
                for addr in actor.downstream
            ]

        # Unknown slash command
        cmd = text.split()[0]
        return [
            Send(
                to=addr,
                message=Message(
                    sender=actor.address,
                    payload={"text": f"未知命令: {cmd}\n发送 /help 查看可用命令"},
                ),
            )
            for addr in actor.downstream
        ]

    def on_stop(self, actor: Actor) -> list[Action]:
        return []

    @staticmethod
    def _help_text() -> str:
        return (
            "可用命令:\n"
            "/help — 显示帮助\n"
            "/spawn <name> — 创建子 session\n"
            "/kill <name> — 结束子 session\n"
            "/sessions — 列出活跃 sessions"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
    "admin": AdminHandler(),
}


def get_handler(name: str) -> Handler:
    """Look up a handler by name. Raises ValueError if not found."""
    handler = HANDLER_REGISTRY.get(name)
    if handler is None:
        raise ValueError(f"Unknown handler: {name}")
    return handler
