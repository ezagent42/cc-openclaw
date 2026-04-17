"""CommandContext — data passed from dispatcher to command function."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from channel_server.core.actor import Message


@dataclass
class CommandContext:
    """Everything a command function needs.

    `feishu`, `cc`, and `runtime` are typed `Any` to avoid import cycles. In
    practice: `feishu` is FeishuAdapter, `cc` is CCAdapter, `runtime` is
    ActorRuntime. Both adapters are populated regardless of which one received
    the command, because commands like /spawn need both.
    """
    source: str                         # "feishu" | "cc_mcp"
    user: str                           # feishu_user:xxx
    chat_id: str | None                 # top-level Feishu chat id
    app_id: str                         # FeishuAdapter.app_id (for actor address)
    current_actor: str | None           # actor this command originated from
    parent_actor: str | None            # current_actor's parent
    thread_root_id: str | None          # root_id if the command came from a thread
    raw_msg: Message | None             # original inbound message (for reply)
    feishu: Any                         # FeishuAdapter — always populated
    cc: Any                             # CCAdapter — always populated
    runtime: Any                        # ActorRuntime
