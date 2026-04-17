"""/sessions — list active CC sessions owned by the invoker."""
from __future__ import annotations

from channel_server.commands.context import CommandContext
from channel_server.commands.registry import ROOT_SCOPE


@ROOT_SCOPE.register("sessions", help="列出活跃 sessions")
async def sessions_cmd(args, ctx: CommandContext):
    user_prefix = f"cc:{ctx.user.removeprefix('feishu_user:')}."
    rows = []
    for addr, actor in ctx.runtime.actors.items():
        if actor.address.startswith(user_prefix) and actor.state != "ended":
            rows.append(f"• {actor.tag} ({actor.address}) — {actor.state}")
    if not rows:
        await ctx.feishu.reply(ctx, "当前没有活跃 sessions")
        return
    await ctx.feishu.reply(ctx, "活跃 sessions:\n" + "\n".join(rows))
