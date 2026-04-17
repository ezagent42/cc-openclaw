"""/kill <name> — stop a CC session: kill tmux, stop actors. on_stop hooks
emit unpin + anchor-update TransportSend automatically."""
from __future__ import annotations

from dataclasses import dataclass

from channel_server.commands.context import CommandContext
from channel_server.commands.errors import CommandError
from channel_server.commands.registry import ROOT_SCOPE


@dataclass
class KillArgs:
    name: str   # required (no default → bind_args raises BadArgs if omitted)


@ROOT_SCOPE.register("kill", args=KillArgs, help="/kill <name> — end a session")
async def kill_cmd(args: KillArgs, ctx: CommandContext):
    user = ctx.user.removeprefix("feishu_user:")
    cc_addr = f"cc:{user}.{args.name}"

    actor = ctx.runtime.lookup(cc_addr)
    if actor is None:
        raise CommandError(f"session '{args.name}' not found")

    # Find downstream feishu thread actor (if any) — stop it too
    thread_addr = next(
        (d for d in actor.downstream if d.startswith("feishu:")),
        None,
    )

    # Kill the tmux process first (SYNC — do NOT await)
    ctx.cc.kill_cc_process(user, args.name)

    # Stop actors via runtime.stop (ASYNC). on_stop hooks will emit
    # unpin + anchor-update TransportSend actions automatically.
    await ctx.runtime.stop(cc_addr)
    if thread_addr:
        await ctx.runtime.stop(thread_addr)

    await ctx.feishu.reply(ctx, f"Session {args.name} ended")
