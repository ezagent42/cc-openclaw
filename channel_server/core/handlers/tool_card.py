"""ToolCardHandler — accumulates a rolling history window and emits tool-card updates."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, TransportSend, UpdateActor


class ToolCardHandler:
    """Accumulate a rolling history window (max 5) and emit a tool-card update."""

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
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
