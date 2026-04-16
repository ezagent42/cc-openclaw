"""ForwardAllHandler — broadcasts every message to all downstream actors."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send


class ForwardAllHandler:
    """Broadcast every message to all downstream actors (same as FeishuInbound)."""

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        return [Send(to=addr, message=msg) for addr in actor.downstream]

    def on_stop(self, actor: Actor) -> list[Action]:
        return []
