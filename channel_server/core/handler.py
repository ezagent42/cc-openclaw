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
# Registry
# ---------------------------------------------------------------------------

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "tool_card": ToolCardHandler(),
}


def get_handler(name: str) -> Handler:
    """Look up a handler by name. Raises ValueError if not found."""
    handler = HANDLER_REGISTRY.get(name)
    if handler is None:
        raise ValueError(f"Unknown handler: {name}")
    return handler
