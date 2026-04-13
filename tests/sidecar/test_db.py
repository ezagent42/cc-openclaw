"""Tests for sidecar.db — SQLite database layer."""

import pytest

from sidecar.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.sqlite"))
    await d.init()
    yield d
    await d.close()


# ── Permission ───────────────────────────────────────────────────────


async def test_upsert_and_get_permission(db):
    await db.upsert_permission("u1", "Alice", is_user_member=True, is_admin=False)
    row = await db.get_permission("u1")
    assert row is not None
    assert row["open_id"] == "u1"
    assert row["display_name"] == "Alice"
    assert row["is_user_member"] == 1
    assert row["is_admin"] == 0


async def test_upsert_permission_update(db):
    await db.upsert_permission("u1", "Alice", is_user_member=False, is_admin=False)
    await db.upsert_permission("u1", "Alice", is_user_member=True, is_admin=False)
    row = await db.get_permission("u1")
    assert row["is_user_member"] == 1


async def test_list_authorized(db):
    await db.upsert_permission("u1", "Alice", is_user_member=True, is_admin=False)
    await db.upsert_permission("u2", "Bob", is_user_member=False, is_admin=True)
    await db.upsert_permission("u3", "Eve", is_user_member=False, is_admin=False)
    authorized = await db.list_authorized()
    ids = {r["open_id"] for r in authorized}
    assert ids == {"u1", "u2"}


async def test_list_all_permissions(db):
    await db.upsert_permission("u1", "Alice", is_user_member=True, is_admin=False)
    await db.upsert_permission("u2", "Bob", is_user_member=False, is_admin=False)
    rows = await db.list_all_permissions()
    assert len(rows) == 2


# ── Agent registry ───────────────────────────────────────────────────


async def test_create_and_get_agent(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    agent = await db.get_agent("a1")
    assert agent is not None
    assert agent["agent_id"] == "a1"
    assert agent["status"] == "active"
    assert agent["workspace_path"] == "/ws/a1"


async def test_update_agent_status_suspend(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    await db.update_agent_status("a1", "suspended")
    agent = await db.get_agent("a1")
    assert agent["status"] == "suspended"
    assert agent["suspended_at"] is not None


async def test_update_agent_status_restore(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    await db.update_agent_status("a1", "suspended")
    await db.update_agent_status("a1", "active")
    agent = await db.get_agent("a1")
    assert agent["status"] == "active"
    assert agent["restored_at"] is not None


async def test_get_agent_by_open_id(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    await db.create_agent("a2", "u1", "c2", "group", "/ws/a2")
    agent = await db.get_agent_by_open_id("u1")
    assert agent is not None
    assert agent["agent_type"] == "user"


async def test_get_agent_by_chat_id(db):
    await db.create_agent("a1", "u1", "c1", "group", "/ws/a1")
    agent = await db.get_agent_by_chat_id("c1")
    assert agent is not None
    assert agent["agent_type"] == "group"


async def test_list_agents_by_status(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    await db.create_agent("a2", "u2", "c2", "user", "/ws/a2")
    await db.update_agent_status("a2", "suspended")
    active = await db.list_agents(status="active")
    assert len(active) == 1
    assert active[0]["agent_id"] == "a1"
    all_agents = await db.list_agents()
    assert len(all_agents) == 2


async def test_delete_agent(db):
    await db.create_agent("a1", "u1", "c1", "user", "/ws/a1")
    await db.delete_agent("a1")
    assert await db.get_agent("a1") is None


# ── Audit log ────────────────────────────────────────────────────────


async def test_write_and_query_audit_log(db):
    await db.write_audit("create", "agent/a1", "admin")
    await db.write_audit("suspend", "agent/a1", "admin", details="reason: test")
    logs = await db.query_audit_log()
    assert len(logs) == 2
    # ORDER BY id DESC — most recent first
    assert logs[0]["action"] == "suspend"
    assert logs[1]["action"] == "create"


async def test_query_audit_log_with_limit(db):
    for i in range(5):
        await db.write_audit(f"action{i}", "target", "actor")
    logs = await db.query_audit_log(limit=3)
    assert len(logs) == 3


# ── Deny rate limit ─────────────────────────────────────────────────


async def test_check_deny_rate(db):
    # First call — not in window, should send deny reply
    result = await db.check_deny_rate("u1", window_minutes=10)
    assert result is True
    # Second call — within window, should suppress
    result = await db.check_deny_rate("u1", window_minutes=10)
    assert result is False


# ── Event dedup ──────────────────────────────────────────────────────


async def test_check_event_dedup(db):
    assert await db.check_event_dedup("evt1") is True   # new
    assert await db.check_event_dedup("evt1") is False   # duplicate
    assert await db.check_event_dedup("evt2") is True   # different event
