"""/spawn <name> — create a CC session (main or child).

Merges the I/O sequence from CCAdapter._handle_spawn (anchor + pin + tmux)
and the actor-registration logic from SessionMgrHandler._handle_spawn into
a single implementation. This closes Goal #4 of the unified-command-registry
spec — Feishu-originated spawns now actually start tmux.
"""
from __future__ import annotations

from dataclasses import dataclass

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE
from channel_server.core.actor import Transport


_MAX_CHILDREN = 5


@dataclass
class SpawnArgs:
    # Required: no default → bind_args raises BadArgs if the user omits it,
    # and the dispatcher replies "参数错误" automatically.
    name: str


@ROOT_SCOPE.register("spawn", args=SpawnArgs,
                     help="/spawn <name> — create a session")
async def spawn_cmd(args: SpawnArgs, ctx: CommandContext):
    user = ctx.user.removeprefix("feishu_user:")
    session_name = args.name
    tag = session_name   # current convention: tag == session name

    # Enforce child limit under the invoker's actor (or under user's root)
    if ctx.current_actor:
        children = sum(
            1 for a in ctx.runtime.actors.values()
            if a.parent == ctx.current_actor and a.state not in ("ended",)
        )
    else:
        # Top-level spawn: count user's CC sessions
        prefix = f"cc:{user}."
        children = sum(
            1 for a in ctx.runtime.actors.values()
            if a.address.startswith(prefix) and a.state not in ("ended",)
        )
    if children >= _MAX_CHILDREN:
        raise CommandError(f"Max sessions ({_MAX_CHILDREN}) reached")

    cc_addr = f"cc:{user}.{session_name}"
    existing = ctx.runtime.lookup(cc_addr)
    if existing is not None and existing.state not in ("ended",):
        raise CommandError(f"session '{session_name}' already exists")

    # ---- I/O phase (mirrors CCAdapter._handle_spawn:411-417) ----
    anchor = await ctx.feishu.create_thread_anchor(ctx.chat_id, tag)
    if anchor:
        await ctx.feishu.pin_message(anchor)

    # ---- Actor registration (legacy address scheme preserved) ----
    app_id = ctx.app_id or ctx.feishu.app_id
    thread_addr = f"feishu:{app_id}:{ctx.chat_id}:thread:{session_name}"

    if anchor and ctx.chat_id:
        ctx.runtime.spawn(
            address=thread_addr,
            handler="feishu_inbound",
            tag=tag,
            parent=ctx.current_actor,
            downstream=[cc_addr],
            metadata={"chat_id": ctx.chat_id, "tag": tag, "mode": "child"},
            transport=Transport(
                type="feishu_thread",
                config={"chat_id": ctx.chat_id, "root_id": anchor},
            ),
        )

    ctx.runtime.spawn(
        address=cc_addr,
        handler="cc_session",
        tag=tag,
        parent=ctx.current_actor or f"cc:{user}.root",
        downstream=[thread_addr] if anchor else [],
        state="suspended",   # active when WS connects
        metadata={
            "chat_id": ctx.chat_id,
            "tag": tag,
            "anchor_msg_id": anchor or "",
        },
    )

    # ---- Start tmux (SYNC; returns bool) ----
    ok = ctx.cc.spawn_cc_process(user, session_name, tag=tag, chat_id=ctx.chat_id)
    if not ok:
        # Rollback on tmux failure — mirrors CCAdapter._handle_spawn:440-444
        await ctx.runtime.stop(cc_addr)
        if anchor:
            await ctx.runtime.stop(thread_addr)
        raise CommandError(f"tmux failed to start for '{session_name}'")

    await ctx.feishu.reply(ctx, f"Session {session_name} started")
