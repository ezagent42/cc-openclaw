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


# ---------- Stage 3: bind_args ----------

def bind_args(schema: Type[Any] | None, tokens: list[str]) -> Any:
    """Bind tokens to a dataclass schema. Raises BadArgs on mismatch.

    Supports:
      • positional: tokens consumed in field order
      • named: "--key value" or "--key=value"
      • named args are matched first; remaining tokens fill positional
    """
    if schema is None:
        return list(tokens)

    schema_fields = fields(schema)
    known_names = {f.name for f in schema_fields}
    values: dict[str, Any] = {}
    positional_leftover: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                key, _, val = body.partition("=")
                i += 1
            else:
                key = body
                if i + 1 >= len(tokens):
                    raise BadArgs(f"missing value for --{key}")
                val = tokens[i + 1]
                i += 2
            if key not in known_names:
                raise BadArgs(f"unknown argument: --{key}")
            values[key] = val
        else:
            positional_leftover.append(tok)
            i += 1

    # Fill positional from leftovers in field order (skipping ones already set)
    for f in schema_fields:
        if f.name in values:
            continue
        if positional_leftover:
            values[f.name] = positional_leftover.pop(0)

    if positional_leftover:
        raise BadArgs(f"too many arguments: {positional_leftover}")

    # Instantiate — dataclass raises TypeError on missing required fields
    try:
        return schema(**values)
    except TypeError as e:
        raise BadArgs(str(e)) from e
