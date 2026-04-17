"""Session lifecycle orchestrator.

Handles init_session from register. All /spawn, /kill, /sessions commands
are now handled by the unified command registry (builtin commands).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
)

if TYPE_CHECKING:
    from channel_server.core.runtime import ActorRuntime


def _reply(app_id: str, chat_id: str, text: str) -> Send:
    """Build a reply action that sends text to the feishu chat actor."""
    return Send(
        to=f"feishu:{app_id}:{chat_id}",
        message=Message(sender="system:session-mgr", payload={"text": text}),
    )


class SessionMgrHandler:
    """Orchestrates session lifecycle: init.

    /spawn, /kill, /sessions are all handled by the unified command registry.
    This handler is retained for init_session from register and will be
    fully removed in Task 14.
    """

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        msg_type = msg.metadata.get("type", "")

        if msg_type == "init_session":
            return self._handle_init(actor, msg, runtime)

        return []

    def on_spawn(self, actor: Actor) -> list[Action]:
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        return []

    def _handle_init(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None") -> list[Action]:
        """Handle init_session from _handle_register for root sessions."""
        return []
