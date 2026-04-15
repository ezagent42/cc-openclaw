"""Handler protocol and built-in handler implementations for the actor model."""
from __future__ import annotations

from typing import Protocol

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    TransportSend,
    UpdateActor,
)


class Handler(Protocol):
    """Protocol that all actor message handlers must satisfy."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]: ...


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


# ---------------------------------------------------------------------------
# CCSessionHandler
# ---------------------------------------------------------------------------

class CCSessionHandler:
    """Bridge between actor messages and a Claude Code session.

    - External message (sender != actor.address): push to CC via TransportSend.
    - CC command (sender == actor.address): dispatch by command type.
    """

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        if msg.sender != actor.address:
            # External message — forward to the CC session over its transport.
            return [TransportSend(payload=msg.payload)]

        # Message originated from CC itself — treat as a command.
        command = msg.payload.get("command")

        if command == "reply":
            text = msg.payload.get("text", "")
            if actor.tag != "root":
                text = f"[{actor.tag}] {text}"
            reply_msg = Message(
                sender=actor.address,
                type=msg.type,
                payload={**msg.payload, "text": text},
            )
            return [Send(to=addr, message=reply_msg) for addr in actor.downstream]

        if command == "forward":
            target = msg.payload.get("target", "")
            return [Send(to=target, message=msg)]

        if command == "send_summary":
            parent_feishu = msg.payload.get("parent_feishu", "")
            return [Send(to=parent_feishu, message=msg)]

        if command == "send_file":
            return [TransportSend(payload={
                "type": "send_file",
                "chat_id": msg.payload.get("chat_id", ""),
                "file_path": msg.payload.get("file_path", ""),
            })]

        if command == "react":
            return [TransportSend(payload={
                "type": "react",
                "message_id": msg.payload.get("message_id", ""),
                "emoji_type": msg.payload.get("emoji_type", "THUMBSUP"),
            })]

        if command == "update_title":
            update_msg = Message(
                sender=actor.address,
                type="update_title",
                payload=msg.payload,
            )
            return [Send(to=addr, message=update_msg) for addr in actor.downstream]

        # Unknown command.
        return []


# ---------------------------------------------------------------------------
# ForwardAllHandler
# ---------------------------------------------------------------------------

class ForwardAllHandler:
    """Broadcast every message to all downstream actors (same as FeishuInbound)."""

    def handle(self, actor: Actor, msg: Message) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]


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
            TransportSend(payload={"type": "tool_card_update", "text": display}),
        ]


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
        if msg.type == "system":
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
                        type="text",
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
                    type="text",
                    payload={"text": f"\u672a\u77e5\u547d\u4ee4: {cmd}\n\u53d1\u9001 /help \u67e5\u770b\u53ef\u7528\u547d\u4ee4"},
                ),
            )
            for addr in actor.downstream
        ]

    @staticmethod
    def _help_text() -> str:
        return (
            "\u53ef\u7528\u547d\u4ee4:\n"
            "/help \u2014 \u663e\u793a\u5e2e\u52a9\n"
            "/spawn <name> \u2014 \u521b\u5efa\u5b50 session\n"
            "/kill <name> \u2014 \u7ed3\u675f\u5b50 session\n"
            "/sessions \u2014 \u5217\u51fa\u6d3b\u8dc3 sessions"
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
