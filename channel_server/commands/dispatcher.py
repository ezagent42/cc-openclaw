"""CommandDispatcher — adapter-layer entrypoint for the command registry."""
from __future__ import annotations

import logging
from typing import Any

from channel_server.commands.errors import UnknownCommand, BadArgs, CommandError
from channel_server.commands.parse import normalize_command_text, parse_command
from channel_server.commands.registry import resolve_scope

log = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(
        self,
        runtime: Any,
        feishu_adapter: Any,
        cc_adapter: Any,
        *,
        fallback_on_unknown: bool = False,
    ):
        self._runtime = runtime
        self._feishu = feishu_adapter
        self._cc = cc_adapter
        # During migration, True lets unregistered commands fall through to the
        # legacy actor pipeline. Flipped to False at Phase 4 cleanup.
        self._fallback_on_unknown = fallback_on_unknown

    @property
    def fallback_on_unknown(self) -> bool:
        return self._fallback_on_unknown

    def set_fallback(self, value: bool) -> None:
        self._fallback_on_unknown = value

    async def dispatch_from_adapter(
        self,
        *,
        adapter: Any,
        raw_text: str,
        source_actor: str | None,
        ctx_partial: dict,
    ) -> bool:
        """Returns True iff the command was handled here (adapter should stop).

        `adapter` is the adapter that received the command (used for reply_error
        on the error paths). The command function itself uses ctx.feishu / ctx.cc
        which are injected here from the dispatcher's stored references.

        Returns False for non-command text OR, when fallback_on_unknown is True,
        for unregistered commands (so adapter can deliver to the legacy pipeline).
        """
        normalized = normalize_command_text(raw_text)
        if normalized is None:
            return False

        invocation = parse_command(normalized)
        ctx_partial = {
            **ctx_partial,
            "feishu": self._feishu,
            "cc": self._cc,
            "runtime": self._runtime,
        }

        try:
            scope = resolve_scope(source_actor, self._runtime)
            await scope.dispatch(invocation.name, invocation.tokens, ctx_partial)
        except UnknownCommand as e:
            if self._fallback_on_unknown:
                return False
            await adapter.reply_error(
                ctx_partial,
                f"未知命令: /{e.name}\n发送 /help 查看可用命令",
            )
        except BadArgs as e:
            await adapter.reply_error(ctx_partial, f"参数错误: {e}")
        except CommandError as e:
            await adapter.reply_error(ctx_partial, f"命令失败: {e}")
        except Exception:
            log.exception("unexpected error in command %s", invocation.name)
            await adapter.reply_error(
                ctx_partial, f"命令内部错误: /{invocation.name}"
            )
        return True
