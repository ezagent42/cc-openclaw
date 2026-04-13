"""Tests for sidecar.api — Management HTTP API."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from sidecar.api import create_app, DENY_MESSAGE


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_permission.return_value = None
    db.get_agent_by_open_id.return_value = None
    db.check_deny_rate.return_value = True
    db.list_agents.return_value = []
    db.query_audit_log.return_value = []
    return db


@pytest.fixture
def mock_provisioner():
    return AsyncMock()


@pytest.fixture
async def client(mock_db, mock_provisioner):
    app = create_app(db=mock_db, provisioner=mock_provisioner)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── resolve-sender ──────────────────────────────────────────────────


async def test_resolve_sender_unauthorized_deny(client, mock_db):
    """Unauthorized user within rate window gets deny with message."""
    mock_db.get_permission.return_value = None
    mock_db.check_deny_rate.return_value = True

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "deny"
    assert body["message"] == DENY_MESSAGE


async def test_resolve_sender_unauthorized_deny_silent(client, mock_db):
    """Unauthorized user past rate window gets deny_silent."""
    mock_db.get_permission.return_value = None
    mock_db.check_deny_rate.return_value = False

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "deny_silent"


async def test_resolve_sender_unauthorized_with_permission_row(client, mock_db):
    """User exists in permission table but not authorized."""
    mock_db.get_permission.return_value = {
        "open_id": "ou_xxx",
        "display_name": "Test",
        "is_user_member": False,
        "is_admin": False,
    }
    mock_db.check_deny_rate.return_value = True

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "deny"


async def test_resolve_sender_authorized_no_agent(client, mock_db):
    """Authorized user with no agent gets provision action."""
    mock_db.get_permission.return_value = {
        "open_id": "ou_xxx",
        "display_name": "Alice",
        "is_user_member": True,
        "is_admin": False,
    }
    mock_db.get_agent_by_open_id.return_value = None

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "provision"


async def test_resolve_sender_authorized_suspended(client, mock_db):
    """Authorized user with suspended agent gets restore action."""
    mock_db.get_permission.return_value = {
        "open_id": "ou_xxx",
        "display_name": "Alice",
        "is_user_member": True,
        "is_admin": False,
    }
    mock_db.get_agent_by_open_id.return_value = {
        "agent_id": "u-acct-ou_xxx",
        "status": "suspended",
    }

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "restore"
    assert body["agent_id"] == "u-acct-ou_xxx"


async def test_resolve_sender_authorized_provisioning(client, mock_db):
    """Authorized user with agent in provisioning state gets retry_later."""
    mock_db.get_permission.return_value = {
        "open_id": "ou_xxx",
        "display_name": "Alice",
        "is_user_member": True,
        "is_admin": False,
    }
    mock_db.get_agent_by_open_id.return_value = {
        "agent_id": "u-acct-ou_xxx",
        "status": "provisioning",
    }

    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["action"] == "retry_later"


# ── provision ───────────────────────────────────────────────────────


async def test_provision_calls_provisioner(client, mock_db, mock_provisioner):
    """POST /provision calls provisioner.provision_user and returns agent_id."""
    mock_db.get_permission.return_value = {
        "open_id": "ou_xxx",
        "display_name": "Alice",
        "is_user_member": True,
        "is_admin": False,
    }
    mock_provisioner.provision_user.return_value = "u-acct-ou_xxx"

    resp = await client.post("/api/v1/provision", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["agent_id"] == "u-acct-ou_xxx"
    mock_provisioner.provision_user.assert_awaited_once_with("ou_xxx", "Alice")


# ── restore ─────────────────────────────────────────────────────────


async def test_restore_calls_provisioner(client, mock_provisioner):
    """POST /restore calls provisioner.restore_user."""
    resp = await client.post("/api/v1/restore", json={"open_id": "ou_xxx"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    mock_provisioner.restore_user.assert_awaited_once_with("ou_xxx")


# ── list agents ─────────────────────────────────────────────────────


async def test_list_agents(client, mock_db):
    """GET /agents returns agents from db."""
    mock_db.list_agents.return_value = [
        {"agent_id": "u-1", "status": "active"},
        {"agent_id": "u-2", "status": "suspended"},
    ]

    resp = await client.get("/api/v1/agents")
    assert resp.status == 200
    body = await resp.json()
    assert len(body["agents"]) == 2
    mock_db.list_agents.assert_awaited_once_with(status=None)


async def test_list_agents_with_status_filter(client, mock_db):
    """GET /agents?status=active filters by status."""
    mock_db.list_agents.return_value = [{"agent_id": "u-1", "status": "active"}]

    resp = await client.get("/api/v1/agents?status=active")
    assert resp.status == 200
    body = await resp.json()
    assert len(body["agents"]) == 1
    mock_db.list_agents.assert_awaited_once_with(status="active")


# ── audit log ───────────────────────────────────────────────────────


async def test_audit_log(client, mock_db):
    """GET /audit-log returns logs from db."""
    mock_db.query_audit_log.return_value = [
        {"id": 1, "action": "provision", "target": "agent/u-1", "actor": "system"},
    ]

    resp = await client.get("/api/v1/audit-log")
    assert resp.status == 200
    body = await resp.json()
    assert len(body["logs"]) == 1
    mock_db.query_audit_log.assert_awaited_once_with(since=None, limit=50)


async def test_audit_log_with_params(client, mock_db):
    """GET /audit-log with since and limit params."""
    mock_db.query_audit_log.return_value = []

    resp = await client.get("/api/v1/audit-log?since=2025-01-01T00:00:00&limit=10")
    assert resp.status == 200
    mock_db.query_audit_log.assert_awaited_once_with(
        since="2025-01-01T00:00:00", limit=10
    )


# ── admin reset ─────────────────────────────────────────────────────


async def test_admin_reset(client, mock_provisioner):
    """POST /admin/reset-agent calls provisioner.reset_user."""
    resp = await client.post(
        "/api/v1/admin/reset-agent",
        json={"open_id": "ou_xxx", "actor": "ou_admin"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    mock_provisioner.reset_user.assert_awaited_once_with("ou_xxx", actor="ou_admin")
