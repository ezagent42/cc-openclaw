"""Unit tests for commands.parse."""
from __future__ import annotations

import pytest
from dataclasses import dataclass

from channel_server.commands.parse import (
    normalize_command_text,
    parse_command,
    CommandInvocation,
    bind_args,
)
from channel_server.commands.errors import BadArgs


# ---------- normalize_command_text ----------

def test_normalize_empty_returns_none():
    assert normalize_command_text("") is None
    assert normalize_command_text("   \n\n  ") is None

def test_normalize_non_command_returns_none():
    assert normalize_command_text("hello world") is None

def test_normalize_simple_command():
    assert normalize_command_text("/spawn") == "/spawn"

def test_normalize_command_with_args():
    assert normalize_command_text("/spawn foo") == "/spawn foo"

def test_normalize_strips_quoted_prefix():
    # Feishu thread-reply quote: "> @林懿伦 said ...\n/spawn foo"
    text = "> @user said blah\n/spawn foo"
    assert normalize_command_text(text) == "/spawn foo"

def test_normalize_ignores_trailing_blank_lines():
    assert normalize_command_text("/spawn\n\n") == "/spawn"

def test_normalize_only_quote_no_command():
    assert normalize_command_text("> @user said blah") is None


# ---------- parse_command ----------

def test_parse_simple_command():
    inv = parse_command("/spawn")
    assert inv == CommandInvocation(name="spawn", raw_args="", tokens=[])

def test_parse_command_with_positional():
    inv = parse_command("/spawn foo")
    assert inv.name == "spawn"
    assert inv.tokens == ["foo"]

def test_parse_command_with_quoted_arg():
    inv = parse_command('/spawn "foo bar"')
    assert inv.tokens == ["foo bar"]

def test_parse_command_with_named_arg():
    inv = parse_command("/spawn foo --tag beta")
    assert inv.tokens == ["foo", "--tag", "beta"]


# ---------- bind_args dataclasses ----------

@dataclass
class _SpawnArgs:
    tag: str = ""


@dataclass
class _KillArgs:
    name: str   # required (no default)


# ---------- bind_args ----------

def test_bind_none_schema_returns_tokens():
    assert bind_args(None, ["a", "b"]) == ["a", "b"]

def test_bind_positional():
    assert bind_args(_SpawnArgs, ["foo"]) == _SpawnArgs(tag="foo")

def test_bind_no_args_uses_defaults():
    assert bind_args(_SpawnArgs, []) == _SpawnArgs(tag="")

def test_bind_named_flag():
    assert bind_args(_SpawnArgs, ["--tag", "foo"]) == _SpawnArgs(tag="foo")

def test_bind_named_equals():
    assert bind_args(_SpawnArgs, ["--tag=foo"]) == _SpawnArgs(tag="foo")

def test_bind_missing_required_raises():
    with pytest.raises(BadArgs):
        bind_args(_KillArgs, [])

def test_bind_extra_positional_raises():
    with pytest.raises(BadArgs):
        bind_args(_SpawnArgs, ["foo", "bar"])

def test_bind_unknown_named_raises():
    with pytest.raises(BadArgs):
        bind_args(_SpawnArgs, ["--wat", "x"])
