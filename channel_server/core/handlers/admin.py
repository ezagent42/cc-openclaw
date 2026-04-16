"""AdminHandler — handles admin commands and notifications."""
from __future__ import annotations

from channel_server.core.actor import Action, Actor, Message, Send


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
