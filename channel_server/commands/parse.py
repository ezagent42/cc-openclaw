"""Command-text parsing: normalize → parse_command → bind_args."""
from __future__ import annotations

import shlex
from dataclasses import dataclass, fields, MISSING
from typing import Any, Type

from channel_server.commands.errors import BadArgs


# ---------- Stage 1: normalize ----------

def normalize_command_text(text: str) -> str | None:
    """Extract the command line from inbound text; return None if not a command.

    Handles Feishu thread-reply quoted prefix ("> ...\n") by taking the last
    non-empty line. Returns None for empty input or text that does not start
    with "/" on the last line.
    """
    if not text:
        return None
    last_line = next(
        (ln for ln in reversed(text.splitlines()) if ln.strip()),
        "",
    ).strip()
    if not last_line.startswith("/"):
        return None
    return last_line


# ---------- Stage 2: parse ----------

@dataclass
class CommandInvocation:
    name: str            # without leading "/"
    raw_args: str
    tokens: list[str]    # shlex.split of raw_args


def parse_command(normalized: str) -> CommandInvocation:
    """Split '/name args...' into name + tokens. Assumes input already normalized."""
    body = normalized[1:]  # strip leading "/"
    head, _, rest = body.partition(" ")
    rest = rest.strip()
    tokens = shlex.split(rest) if rest else []
    return CommandInvocation(name=head.strip(), raw_args=rest, tokens=tokens)


# ---------- Stage 3: bind_args (implemented in Task 4) ----------
