"""Tests for channel_server.core.persistence."""
import json
from pathlib import Path

import pytest

from channel_server.core.actor import Actor, Transport
from channel_server.core.persistence import load_actors, save_actors


def _make_actor(address: str, tag: str, handler: str, transport: Transport | None = None) -> Actor:
    return Actor(address=address, tag=tag, handler=handler, transport=transport)


def test_save_and_load_actors(tmp_path: Path) -> None:
    """Round-trip: save two actors (one with transport, one without) and reload."""
    transport = Transport(type="feishu_chat", config={"chat_id": "oc_abc123"})
    actor_a = _make_actor("actor://a", "session", "session_handler", transport=transport)
    actor_b = _make_actor("actor://b", "gateway", "gateway_handler")

    actors = {"actor://a": actor_a, "actor://b": actor_b}
    filepath = tmp_path / "actors.json"

    save_actors(actors, filepath)
    assert filepath.exists()

    loaded = load_actors(filepath)
    assert set(loaded.keys()) == {"actor://a", "actor://b"}

    la = loaded["actor://a"]
    assert la.address == "actor://a"
    assert la.tag == "session"
    assert la.handler == "session_handler"
    assert la.transport is not None
    assert la.transport.type == "feishu_chat"
    assert la.transport.config == {"chat_id": "oc_abc123"}

    lb = loaded["actor://b"]
    assert lb.address == "actor://b"
    assert lb.transport is None


def test_load_missing_file(tmp_path: Path) -> None:
    """load_actors returns {} when the file does not exist."""
    filepath = tmp_path / "nonexistent.json"
    result = load_actors(filepath)
    assert result == {}


def test_save_filters_ended_actors(tmp_path: Path) -> None:
    """Actors in 'ended' state must not be written to disk."""
    actor_active = _make_actor("actor://active", "session", "session_handler")
    actor_ended = _make_actor("actor://ended", "session", "session_handler")
    actor_ended.state = "ended"

    actors = {"actor://active": actor_active, "actor://ended": actor_ended}
    filepath = tmp_path / "actors.json"

    save_actors(actors, filepath)

    raw = json.loads(filepath.read_text())
    assert "actor://active" in raw
    assert "actor://ended" not in raw

    loaded = load_actors(filepath)
    assert "actor://ended" not in loaded


def test_load_corrupt_file(tmp_path: Path) -> None:
    """load_actors returns {} when the file contains invalid JSON."""
    filepath = tmp_path / "actors.json"
    filepath.write_text("{ this is not valid JSON !!!")

    result = load_actors(filepath)
    assert result == {}
