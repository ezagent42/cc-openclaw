"""CommandScope — registry node supporting parent-chain dispatch and ctx merging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Type

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import UnknownCommand
from channel_server.commands.parse import bind_args


@dataclass
class CommandEntry:
    fn: Callable
    args_schema: Type[Any] | None
    help: str


class CommandScope:
    def __init__(
        self,
        parent: "CommandScope | None" = None,
        default_ctx: dict | None = None,
    ):
        self._commands: dict[str, CommandEntry] = {}
        self._parent = parent
        self._default_ctx = default_ctx or {}

    # ---- registration ----
    def register(self, name: str, args: Type[Any] | None = None, help: str = ""):
        def deco(fn: Callable) -> Callable:
            self._commands[name] = CommandEntry(fn=fn, args_schema=args, help=help)
            return fn
        return deco

    # ---- dispatch ----
    async def dispatch(
        self, name: str, tokens: list[str], ctx_dict: dict
    ) -> Any:
        # Scope's default_ctx (actor-context keys injected by resolve_scope)
        # takes precedence over None values in ctx_dict so that current_actor
        # and parent_actor are not silently erased by a caller passing None.
        merged = {**ctx_dict}
        for k, v in self._default_ctx.items():
            if merged.get(k) is None and v is not None:
                merged[k] = v
        if name in self._commands:
            entry = self._commands[name]
            bound = bind_args(entry.args_schema, tokens)
            return await entry.fn(bound, CommandContext(**merged))
        if self._parent is not None:
            return await self._parent.dispatch(name, tokens, merged)
        raise UnknownCommand(name)

    # ---- help generation ----
    def list_commands_with_help(self) -> list[tuple[str, str]]:
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        scope: "CommandScope | None" = self
        while scope is not None:
            for name, entry in scope._commands.items():
                if name not in seen:
                    seen.add(name)
                    out.append((name, entry.help))
            scope = scope._parent
        return sorted(out)
