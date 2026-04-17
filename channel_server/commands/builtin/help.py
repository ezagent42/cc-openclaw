"""/help — list available commands with help text."""
from __future__ import annotations

from channel_server.commands.context import CommandContext
from channel_server.commands.registry import ROOT_SCOPE, resolve_scope


@ROOT_SCOPE.register("help", help="显示可用命令")
async def help_cmd(args, ctx: CommandContext):
    scope = resolve_scope(ctx.current_actor, ctx.runtime)
    lines = ["可用命令:"]
    for name, help_text in scope.list_commands_with_help():
        if help_text:
            lines.append(f"/{name} — {help_text}")
        else:
            lines.append(f"/{name}")
    text = "\n".join(lines)
    # reply goes via FeishuAdapter.reply — added in Task 8 to the real adapter;
    # FakeFeishuAdapter already has it for tests.
    await ctx.feishu.reply(ctx, text)
