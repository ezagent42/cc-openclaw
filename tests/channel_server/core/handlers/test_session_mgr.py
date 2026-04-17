"""Tests for SessionMgrHandler — init_session.

Note: /spawn, /kill, /sessions tests removed in Task 12 — these commands
are now handled by the unified command registry (builtin commands).
SessionMgrHandler now only handles init_session.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from channel_server.core.actor import Actor, Message
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
