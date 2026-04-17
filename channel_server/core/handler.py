"""Handler protocol and built-in handler implementations for the actor model."""
from __future__ import annotations

from typing import Protocol

from channel_server.core.actor import Action, Actor, Message
from channel_server.core.handlers import (
    CCSessionHandler,
    FeishuInboundHandler,
    ForwardAllHandler,
)


class Handler(Protocol):
    """Protocol that all actor message handlers must satisfy."""

    def handle(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None" = None) -> list[Action]: ...

    def on_spawn(self, actor: Actor) -> list[Action]:
        """Lifecycle callback invoked when an actor is spawned."""
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        """Lifecycle callback invoked when an actor is stopped.

        Returns actions to execute as cleanup (e.g. unpin messages,
        update cards, notify parents). Default: no-op.
        """
        return []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

HANDLER_REGISTRY: dict[str, Handler] = {
    "feishu_inbound": FeishuInboundHandler(),
    "cc_session": CCSessionHandler(),
    "forward_all": ForwardAllHandler(),
    "admin": ForwardAllHandler(),
}


def get_handler(name: str) -> Handler:
    """Look up a handler by name. Raises ValueError if not found."""
    handler = HANDLER_REGISTRY.get(name)
    if handler is None:
        raise ValueError(f"Unknown handler: {name}")
    return handler
