"""Tests for sidecar.provisioner — agent lifecycle management."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidecar.provisioner import Provisioner

TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent.parent / "sidecar" / "templates")


def _make_provisioner(tmp_path, *, db=None, config_client=None):
    agents_dir = str(tmp_path / "agents")
    archived_dir = str(tmp_path / "archived")
    os.makedirs(agents_dir, exist_ok=True)
    os.makedirs(archived_dir, exist_ok=True)

    if db is None:
        db = AsyncMock()
    if config_client is None:
        config_client = AsyncMock()

    p = Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=agents_dir,
        archived_dir=archived_dir,
        templates_dir=TEMPLATES_DIR,
        account_id="acct1",
        default_model="claude-sonnet-4-20250514",
    )
    return p, db, config_client


# ── provision_user ──────────────────────────────────────────────────


async def test_provision_user_agent(tmp_path):
    p, db, cc = _make_provisioner(tmp_path)

    agent_id = await p.provision_user("ou_abc123", "Alice")

    assert agent_id == "u-acct1-ou_abc123"

    # Workspace dirs created
    workspace = Path(p.agents_dir) / agent_id / "workspace"
    agent_dir = Path(p.agents_dir) / agent_id / "agent"
    assert workspace.is_dir()
    assert agent_dir.is_dir()

    # Templates copied and rendered
    soul = (workspace / "SOUL.md").read_text()
    assert "Alice" in soul
    assert "{{display_name}}" not in soul

    user_md = (workspace / "USER.md").read_text()
    assert "Alice" in user_md
    assert "ou_abc123" in user_md
    assert "{{open_id}}" not in user_md
    # .tmpl extension stripped
    assert not (workspace / "USER.md.tmpl").exists()

    # config_client called
    cc.add_agent_with_binding.assert_awaited_once()
    call_kwargs = cc.add_agent_with_binding.call_args.kwargs
    assert call_kwargs["agent_id"] == agent_id
    assert call_kwargs["channel"] == "feishu"
    assert call_kwargs["peer"] == {"kind": "direct", "id": "ou_abc123"}

    # db calls
    db.create_agent.assert_awaited_once()
    db.write_audit.assert_awaited_once()
    audit_args = db.write_audit.call_args
    assert audit_args[0][0] == "provision"


# ── suspend_user ────────────────────────────────────────────────────


async def test_suspend_user_agent(tmp_path):
    p, db, cc = _make_provisioner(tmp_path)

    # Set up fake agent record
    agent_id = "u-acct1-ou_abc123"
    workspace = Path(p.agents_dir) / agent_id / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("test")

    db.get_agent_by_open_id.return_value = {
        "agent_id": agent_id,
        "open_id": "ou_abc123",
        "status": "active",
        "workspace_path": str(workspace),
    }

    await p.suspend_user("ou_abc123")

    cc.remove_binding.assert_awaited_once_with(agent_id)
    db.update_agent_status.assert_awaited_once_with(agent_id, "suspended")
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "suspend"

    # Workspace still exists
    assert workspace.is_dir()


# ── restore_user ────────────────────────────────────────────────────


async def test_restore_user_agent(tmp_path):
    p, db, cc = _make_provisioner(tmp_path)

    agent_id = "u-acct1-ou_abc123"
    db.get_agent_by_open_id.return_value = {
        "agent_id": agent_id,
        "open_id": "ou_abc123",
        "status": "suspended",
        "workspace_path": str(tmp_path / "agents" / agent_id / "workspace"),
    }

    await p.restore_user("ou_abc123")

    cc.add_binding.assert_awaited_once()
    bind_kwargs = cc.add_binding.call_args.kwargs
    assert bind_kwargs["agent_id"] == agent_id
    assert bind_kwargs["channel"] == "feishu"
    assert bind_kwargs["peer"] == {"kind": "direct", "id": "ou_abc123"}

    db.update_agent_status.assert_awaited_once_with(agent_id, "active")
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "restore"


# ── reset_user ──────────────────────────────────────────────────────


async def test_reset_user_agent(tmp_path):
    p, db, cc = _make_provisioner(tmp_path)

    agent_id = "u-acct1-ou_abc123"
    workspace = Path(p.agents_dir) / agent_id / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("test")

    db.get_agent_by_open_id.return_value = {
        "agent_id": agent_id,
        "open_id": "ou_abc123",
        "status": "active",
        "workspace_path": str(workspace),
    }

    await p.reset_user("ou_abc123", actor="admin")

    # Binding removed
    cc.remove_binding.assert_awaited_once_with(agent_id)

    # Workspace moved to archived
    assert not workspace.exists()
    archived_items = list(Path(p.archived_dir).iterdir())
    assert len(archived_items) == 1
    assert agent_id in archived_items[0].name

    # Agent deleted from registry
    db.delete_agent.assert_awaited_once_with(agent_id)
    db.write_audit.assert_awaited_once()
    assert db.write_audit.call_args[0][0] == "reset"
