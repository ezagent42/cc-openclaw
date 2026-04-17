"""Global ROOT_SCOPE singleton + scope resolution from actor tree."""
from __future__ import annotations

from typing import Any

from channel_server.commands.errors import CommandError
from channel_server.commands.scope import CommandScope


ROOT_SCOPE = CommandScope()

_MAX_SCOPE_DEPTH = 16


def resolve_scope(
    source_actor_addr: str | None,
    runtime: Any,
    *,
    _depth: int = 0,
) -> CommandScope:
    """Build a CommandScope chain from an actor's parent chain.

    Scopes are ephemeral — constructed per dispatch. ROOT_SCOPE is returned
    when the actor has no parent or doesn't exist (dangling reference).
    """
    if source_actor_addr is None:
        return ROOT_SCOPE
    if _depth >= _MAX_SCOPE_DEPTH:
        raise CommandError(f"scope recursion exceeded {_MAX_SCOPE_DEPTH} levels")
    actor = runtime.lookup(source_actor_addr)
    if actor is None or actor.parent is None:
        return ROOT_SCOPE
    parent_scope = resolve_scope(actor.parent, runtime, _depth=_depth + 1)
    return CommandScope(
        parent=parent_scope,
        default_ctx={
            "current_actor": source_actor_addr,
            "parent_actor": actor.parent,
        },
    )
