"""Session lifecycle orchestrator.

Handles /spawn, /kill, /sessions commands and init_session from register.
All session lifecycle flows (from both Feishu text and CC MCP tools)
converge here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    SpawnActor,
    Transport,
)

if TYPE_CHECKING:
    from channel_server.core.runtime import ActorRuntime

_MAX_CHILDREN = 5


def _parse_spawn_args(text: str) -> tuple[str, str]:
    """Parse '/spawn name [--tag tag]' → (name, tag)."""
    parts = text.split()
    name = parts[1] if len(parts) > 1 else ""
    tag = ""
    if "--tag" in parts:
        idx = parts.index("--tag")
        if idx + 1 < len(parts):
            tag = parts[idx + 1]
    return name, tag



def _reply(app_id: str, chat_id: str, text: str) -> Send:
    """Build a reply action that sends text to the feishu chat actor."""
    return Send(
        to=f"feishu:{app_id}:{chat_id}",
        message=Message(sender="system:session-mgr", payload={"text": text}),
    )


class SessionMgrHandler:
    """Orchestrates session lifecycle: spawn, kill, list, init."""

    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        text = msg.payload.get("text", "").strip()
        # Strip quoted content prefix ("> ...") for command matching
        command_text = text.split("\n")[-1].strip() if "\n" in text else text
        msg_type = msg.metadata.get("type", "")

        if msg_type == "init_session":
            return self._handle_init(actor, msg, runtime)

        if command_text.startswith("/spawn"):
            # Use command_text for parsing (has the actual /spawn command)
            msg = Message(sender=msg.sender, payload={**msg.payload, "text": command_text}, metadata=msg.metadata)
            return self._handle_spawn(actor, msg, runtime)

        return []

    def on_spawn(self, actor: Actor) -> list[Action]:
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        return []

    def _handle_spawn(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None") -> list[Action]:
        user = msg.payload.get("user", "")
        chat_id = msg.payload.get("chat_id", "")
        app_id = msg.payload.get("app_id", "")
        text = msg.payload.get("text", "")
        session_name, tag = _parse_spawn_args(text)
        tag = tag or session_name

        if not session_name:
            return [_reply(app_id, chat_id, "Usage: /spawn <name> [--tag <tag>]")]

        if not runtime:
            return [_reply(app_id, chat_id, "Internal error: no runtime")]

        cc_addr = f"cc:{user}.{session_name}"
        existing = runtime.lookup(cc_addr)

        # Already active
        if existing and existing.state == "active":
            return [_reply(app_id, chat_id, f"Session '{session_name}' is already active")]

        # Resume suspended
        if existing and existing.state == "suspended":
            return [
                Send(
                    to=cc_addr,
                    message=Message(
                        sender="system:session-mgr",
                        payload={"action": "resume", "tag": tag, "chat_id": chat_id},
                    ),
                ),
                _reply(app_id, chat_id, f"Session '{session_name}' resumed"),
            ]

        # Check child limit
        prefix = f"cc:{user}."
        active = sum(
            1 for a in runtime.actors.values()
            if a.address.startswith(prefix) and a.state not in ("ended",)
        )
        if active >= _MAX_CHILDREN:
            return [_reply(app_id, chat_id, f"Max sessions ({_MAX_CHILDREN}) reached")]

        # New session: spawn feishu thread actor + cc actor
        thread_addr = f"feishu:{app_id}:{chat_id}:thread:{session_name}"

        actions: list[Action] = [
            SpawnActor(
                address=thread_addr,
                handler="feishu_inbound",
                kwargs={
                    "tag": tag,
                    "downstream": [cc_addr],  # wire thread → cc
                    "metadata": {"chat_id": chat_id, "tag": tag, "mode": "child"},
                    "transport": Transport(type="feishu_thread", config={"chat_id": chat_id}),
                },
            ),
            SpawnActor(
                address=cc_addr,
                handler="cc_session",
                kwargs={
                    "tag": tag,
                    "state": "suspended",
                    "parent": f"cc:{user}.root",
                    "downstream": [thread_addr],
                    "metadata": {"chat_id": chat_id, "tag": tag},
                },
            ),
            _reply(app_id, chat_id, f"Session '{session_name}' spawned"),
        ]

        return actions

    def _handle_init(self, actor: Actor, msg: Message, runtime: "ActorRuntime | None") -> list[Action]:
        """Handle init_session from _handle_register for root sessions."""
        return []
