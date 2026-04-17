"""Tests for SessionMgrHandler — /spawn, /kill, /sessions, init_session."""
from __future__ import annotations

from unittest.mock import MagicMock

from channel_server.core.actor import Actor, Message, Send, SpawnActor, StopActor
from channel_server.core.handlers.session_mgr import SessionMgrHandler
from channel_server.core.handler import get_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_actor(address="system:session-mgr", tag="session-mgr", handler="session_mgr",
               downstream=None, metadata=None):
    return Actor(address=address, tag=tag, handler=handler,
                 downstream=downstream or [], metadata=metadata or {})


def make_msg(text, user="testuser", chat_id="oc_test123", app_id="test_app"):
    return Message(sender="feishu_user:u1",
                   payload={"text": text, "user": user, "chat_id": chat_id, "app_id": app_id})


def make_runtime(actors=None):
    rt = MagicMock()
    _actors = actors or {}
    rt.lookup.side_effect = lambda addr: _actors.get(addr)
    rt.actors = _actors
    return rt


# ---------------------------------------------------------------------------
# /spawn tests
# ---------------------------------------------------------------------------

def test_spawn_new_session():
    actor = make_actor()
    msg = make_msg("/spawn dev")
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    spawns = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawns) == 2

    addrs = {s.address for s in spawns}
    assert "cc:testuser.dev" in addrs
    assert any("feishu" in a and ":thread:" in a for a in addrs)

    # Verify thread actor has cc in downstream
    thread_spawn = next(s for s in spawns if ":thread:" in s.address)
    assert "cc:testuser.dev" in thread_spawn.kwargs.get("downstream", [])

    # Verify cc actor has thread in downstream
    cc_spawn = next(s for s in spawns if s.address == "cc:testuser.dev")
    assert thread_spawn.address in cc_spawn.kwargs.get("downstream", [])


def test_spawn_with_tag():
    actor = make_actor()
    msg = make_msg("/spawn voice-widget --tag Voice")
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    cc_spawn = next(a for a in actions if isinstance(a, SpawnActor) and a.address.startswith("cc:"))
    assert cc_spawn.kwargs["tag"] == "Voice"


def test_spawn_already_active():
    actor = make_actor()
    msg = make_msg("/spawn dev")
    existing = Actor(address="cc:testuser.dev", tag="dev", handler="cc_session", state="active")
    rt = make_runtime(actors={"cc:testuser.dev": existing})

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    spawns = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawns) == 0

    replies = [a for a in actions if isinstance(a, Send)]
    assert len(replies) == 1
    assert "already active" in replies[0].message.payload["text"]


def test_spawn_resume_suspended():
    actor = make_actor()
    msg = make_msg("/spawn dev")
    existing = Actor(address="cc:testuser.dev", tag="dev", handler="cc_session", state="suspended")
    rt = make_runtime(actors={"cc:testuser.dev": existing})

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    spawns = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawns) == 0

    sends = [a for a in actions if isinstance(a, Send)]
    # One Send to the cc actor (resume), one reply to feishu
    assert any(a.to == "cc:testuser.dev" for a in sends)
    resume_send = next(a for a in sends if a.to == "cc:testuser.dev")
    assert resume_send.message.payload["action"] == "resume"


def test_spawn_missing_name():
    actor = make_actor()
    msg = make_msg("/spawn")
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    spawns = [a for a in actions if isinstance(a, SpawnActor)]
    assert len(spawns) == 0

    replies = [a for a in actions if isinstance(a, Send)]
    assert len(replies) == 1
    assert "Usage" in replies[0].message.payload["text"]


# ---------------------------------------------------------------------------
# /kill tests
# ---------------------------------------------------------------------------

def test_kill_active_session():
    actor = make_actor()
    msg = make_msg("/kill dev")
    thread_addr = "feishu:test_app:oc_test123:thread:dev"
    existing = Actor(address="cc:testuser.dev", tag="dev", handler="cc_session",
                     state="active", downstream=[thread_addr])
    rt = make_runtime(actors={"cc:testuser.dev": existing})

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    stops = [a for a in actions if isinstance(a, StopActor)]
    stop_addrs = {s.address for s in stops}
    assert "cc:testuser.dev" in stop_addrs


def test_kill_nonexistent():
    actor = make_actor()
    msg = make_msg("/kill ghost")
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    stops = [a for a in actions if isinstance(a, StopActor)]
    assert len(stops) == 0

    replies = [a for a in actions if isinstance(a, Send)]
    assert len(replies) == 1
    assert "not found" in replies[0].message.payload["text"]


def test_kill_cannot_kill_root():
    actor = make_actor()
    msg = make_msg("/kill root")
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    stops = [a for a in actions if isinstance(a, StopActor)]
    assert len(stops) == 0

    replies = [a for a in actions if isinstance(a, Send)]
    assert len(replies) == 1
    assert "Cannot kill root" in replies[0].message.payload["text"]


# ---------------------------------------------------------------------------
# init_session tests
# ---------------------------------------------------------------------------

def test_init_session_root():
    actor = make_actor()
    msg = Message(
        sender="system:register",
        payload={"user": "testuser", "chat_id": "oc_test123", "mode": "root"},
        metadata={"type": "init_session"},
    )
    rt = make_runtime()

    actions = SessionMgrHandler().handle(actor, msg, runtime=rt)

    assert actions == []


# ---------------------------------------------------------------------------
# Registry test
# ---------------------------------------------------------------------------

def test_session_mgr_in_registry():
    handler = get_handler("session_mgr")
    assert isinstance(handler, SessionMgrHandler)
