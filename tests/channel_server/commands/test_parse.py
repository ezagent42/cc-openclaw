"""Unit tests for commands.parse."""
from __future__ import annotations

import pytest

from channel_server.commands.parse import (
    normalize_command_text,
    parse_command,
    CommandInvocation,
)


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
