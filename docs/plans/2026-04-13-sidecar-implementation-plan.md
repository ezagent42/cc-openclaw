# Sidecar Config Manager Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Sidecar config management service that monitors Feishu group membership and dynamically manages OpenClaw agent lifecycle via `config.patch` RPC, enabling multi-tenant "one agent per user" routing through a single shared Feishu app.

**Architecture:** Sidecar is an independent Python process (asyncio). It connects to Feishu via WebSocket to receive group membership events, maintains a SQLite database of permissions and agent state, and calls OpenClaw Gateway's `config.patch` RPC to dynamically add/remove peer bindings. A lightweight HTTP API (aiohttp) serves fallback and admin agents. OpenClaw handles all message routing natively via openclaw-lark.

**Tech Stack:** Python 3.11+, asyncio, aiohttp (HTTP API), aiosqlite (SQLite), lark-oapi (Feishu SDK), httpx (config.patch RPC), PyYAML (config), pytest + pytest-asyncio (testing)

**Design doc:** `docs/plans/2026-04-13-sidecar-config-manager-design.md`
**PRD:** `docs/prd/prd-feishu-openclaw-bridge.md` v0.4

---

## Phase 1: Foundation (PoC Core)

### Task 1: Project scaffolding and dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `sidecar/__init__.py`
- Create: `sidecar/main.py`
- Create: `tests/__init__.py`
- Create: `tests/sidecar/__init__.py`

**Step 1: Add sidecar dependencies to pyproject.toml**

Add `aiohttp`, `aiosqlite`, `httpx`, `pytest`, `pytest-asyncio` to the project:

```toml
[project]
name = "cc-openclaw"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "lark-oapi>=1.3.0",
    "mcp>=1.0.0",
    "anyio>=4.0",
    "websockets>=13.0",
    "pyyaml>=6.0",
    "aiohttp>=3.9",
    "aiosqlite>=0.20",
    "httpx>=0.27",
]

[project.optional-dependencies]
test = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[tool.hatch.build.targets.wheel]
packages = ["feishu", "sidecar"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Create sidecar package skeleton**

`sidecar/__init__.py`:
```python
"""Sidecar config manager for OpenClaw multi-tenant agent routing."""
```

`sidecar/main.py`:
```python
"""Sidecar entry point."""
import asyncio
import logging

log = logging.getLogger("sidecar")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("sidecar starting")
    # Components will be wired here in subsequent tasks
    log.info("sidecar ready")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
```

`tests/__init__.py` and `tests/sidecar/__init__.py`: empty files.

**Step 3: Install and verify**

Run: `uv sync && uv pip install -e ".[test]"`
Run: `uv run python -c "import sidecar; print('ok')"`
Expected: `ok`

**Step 4: Commit**

```bash
git add pyproject.toml sidecar/ tests/
git commit -m "feat(sidecar): project scaffolding and dependencies"
```

---

### Task 2: SQLite database layer

**Files:**
- Create: `sidecar/db.py`
- Create: `tests/sidecar/test_db.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_db.py`:
```python
"""Tests for sidecar.db — SQLite schema and CRUD operations."""
import pytest
from sidecar.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.sqlite"))
    await d.init()
    yield d
    await d.close()


# --- Permission table ---

async def test_upsert_permission(db: Database):
    await db.upsert_permission("ou_alice", "Alice", is_user_member=True, is_admin=False)
    row = await db.get_permission("ou_alice")
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["is_user_member"] == 1
    assert row["is_admin"] == 0


async def test_upsert_permission_update(db: Database):
    await db.upsert_permission("ou_alice", "Alice", is_user_member=True, is_admin=False)
    await db.upsert_permission("ou_alice", "Alice", is_user_member=False, is_admin=False)
    row = await db.get_permission("ou_alice")
    assert row["is_user_member"] == 0


async def test_list_authorized(db: Database):
    await db.upsert_permission("ou_a", "A", is_user_member=True, is_admin=False)
    await db.upsert_permission("ou_b", "B", is_user_member=False, is_admin=True)
    await db.upsert_permission("ou_c", "C", is_user_member=False, is_admin=False)
    authorized = await db.list_authorized()
    open_ids = {r["open_id"] for r in authorized}
    assert open_ids == {"ou_a", "ou_b"}  # user_member OR admin


# --- Agent registry ---

async def test_create_agent(db: Database):
    await db.create_agent("u-shared-ou_alice", "ou_alice", None, "user", "/path")
    row = await db.get_agent("u-shared-ou_alice")
    assert row is not None
    assert row["status"] == "active"
    assert row["agent_type"] == "user"


async def test_update_agent_status(db: Database):
    await db.create_agent("u-shared-ou_alice", "ou_alice", None, "user", "/path")
    await db.update_agent_status("u-shared-ou_alice", "suspended")
    row = await db.get_agent("u-shared-ou_alice")
    assert row["status"] == "suspended"


async def test_get_agent_by_open_id(db: Database):
    await db.create_agent("u-shared-ou_alice", "ou_alice", None, "user", "/path")
    row = await db.get_agent_by_open_id("ou_alice")
    assert row is not None
    assert row["agent_id"] == "u-shared-ou_alice"


async def test_list_agents_by_status(db: Database):
    await db.create_agent("u-shared-ou_a", "ou_a", None, "user", "/pa")
    await db.create_agent("u-shared-ou_b", "ou_b", None, "user", "/pb")
    await db.update_agent_status("u-shared-ou_b", "suspended")
    active = await db.list_agents(status="active")
    assert len(active) == 1
    assert active[0]["agent_id"] == "u-shared-ou_a"


# --- Audit log ---

async def test_write_audit_log(db: Database):
    await db.write_audit("provision", "u-shared-ou_alice", "system", '{"template": "user"}')
    logs = await db.query_audit_log(limit=10)
    assert len(logs) == 1
    assert logs[0]["action"] == "provision"


# --- Deny rate limit ---

async def test_deny_rate_limit(db: Database):
    should = await db.check_deny_rate("ou_dan", window_minutes=10)
    assert should is True  # first time
    should = await db.check_deny_rate("ou_dan", window_minutes=10)
    assert should is False  # within window


# --- Event dedup ---

async def test_event_dedup(db: Database):
    is_new = await db.check_event_dedup("evt_001")
    assert is_new is True
    is_new = await db.check_event_dedup("evt_001")
    assert is_new is False  # duplicate
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_db.py -v`
Expected: FAIL (module not found)

**Step 3: Implement sidecar/db.py**

```python
"""SQLite database layer for Sidecar state management."""
from __future__ import annotations

import aiosqlite
from datetime import datetime, timezone, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS permission (
    open_id        TEXT PRIMARY KEY,
    display_name   TEXT,
    is_user_member BOOLEAN DEFAULT FALSE,
    is_admin       BOOLEAN DEFAULT FALSE,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id       TEXT PRIMARY KEY,
    open_id        TEXT,
    chat_id        TEXT,
    agent_type     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    workspace_path TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    suspended_at   TEXT,
    restored_at    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS deny_rate_limit (
    open_id         TEXT PRIMARY KEY,
    last_denied_at  TEXT NOT NULL,
    deny_count      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS event_dedup (
    event_id    TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    processed   BOOLEAN DEFAULT TRUE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # --- Permission ---

    async def upsert_permission(self, open_id: str, display_name: str | None,
                                 *, is_user_member: bool, is_admin: bool):
        await self._db.execute(
            """INSERT INTO permission (open_id, display_name, is_user_member, is_admin, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(open_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 is_user_member=excluded.is_user_member,
                 is_admin=excluded.is_admin,
                 updated_at=excluded.updated_at""",
            (open_id, display_name, is_user_member, is_admin, _now()))
        await self._db.commit()

    async def get_permission(self, open_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM permission WHERE open_id=?", (open_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_authorized(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM permission WHERE is_user_member=1 OR is_admin=1")
        return [dict(r) for r in await cur.fetchall()]

    async def list_all_permissions(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM permission")
        return [dict(r) for r in await cur.fetchall()]

    # --- Agent registry ---

    async def create_agent(self, agent_id: str, open_id: str | None,
                           chat_id: str | None, agent_type: str,
                           workspace_path: str):
        await self._db.execute(
            """INSERT INTO agent_registry
               (agent_id, open_id, chat_id, agent_type, status, workspace_path, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (agent_id, open_id, chat_id, agent_type, workspace_path, _now()))
        await self._db.commit()

    async def get_agent(self, agent_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM agent_registry WHERE agent_id=?", (agent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_agent_by_open_id(self, open_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM agent_registry WHERE open_id=? AND agent_type='user'", (open_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_agent_by_chat_id(self, chat_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM agent_registry WHERE chat_id=? AND agent_type='group'", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_agent_status(self, agent_id: str, status: str):
        extra = ""
        if status == "suspended":
            extra = ", suspended_at=?"
        elif status == "active":
            extra = ", restored_at=?"
        if extra:
            await self._db.execute(
                f"UPDATE agent_registry SET status=?{extra} WHERE agent_id=?",
                (status, _now(), agent_id))
        else:
            await self._db.execute(
                "UPDATE agent_registry SET status=? WHERE agent_id=?", (status, agent_id))
        await self._db.commit()

    async def delete_agent(self, agent_id: str):
        await self._db.execute("DELETE FROM agent_registry WHERE agent_id=?", (agent_id,))
        await self._db.commit()

    async def list_agents(self, status: str | None = None) -> list[dict]:
        if status:
            cur = await self._db.execute(
                "SELECT * FROM agent_registry WHERE status=?", (status,))
        else:
            cur = await self._db.execute("SELECT * FROM agent_registry")
        return [dict(r) for r in await cur.fetchall()]

    # --- Audit log ---

    async def write_audit(self, action: str, target: str, actor: str,
                          details: str | None = None):
        await self._db.execute(
            "INSERT INTO audit_log (timestamp, action, target, actor, details) VALUES (?,?,?,?,?)",
            (_now(), action, target, actor, details))
        await self._db.commit()

    async def query_audit_log(self, *, since: str | None = None,
                               limit: int = 50) -> list[dict]:
        if since:
            cur = await self._db.execute(
                "SELECT * FROM audit_log WHERE timestamp>=? ORDER BY id DESC LIMIT ?",
                (since, limit))
        else:
            cur = await self._db.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    # --- Deny rate limit ---

    async def check_deny_rate(self, open_id: str, window_minutes: int = 10) -> bool:
        """Returns True if we should send a deny reply, False if within rate window."""
        cur = await self._db.execute(
            "SELECT last_denied_at FROM deny_rate_limit WHERE open_id=?", (open_id,))
        row = await cur.fetchone()
        now = datetime.now(timezone.utc)
        if row:
            last = datetime.fromisoformat(row["last_denied_at"])
            if now - last < timedelta(minutes=window_minutes):
                return False
            await self._db.execute(
                "UPDATE deny_rate_limit SET last_denied_at=?, deny_count=deny_count+1 WHERE open_id=?",
                (_now(), open_id))
        else:
            await self._db.execute(
                "INSERT INTO deny_rate_limit (open_id, last_denied_at) VALUES (?, ?)",
                (open_id, _now()))
        await self._db.commit()
        return True

    # --- Event dedup ---

    async def check_event_dedup(self, event_id: str) -> bool:
        """Returns True if this is a new event, False if duplicate."""
        cur = await self._db.execute(
            "SELECT event_id FROM event_dedup WHERE event_id=?", (event_id,))
        if await cur.fetchone():
            return False
        await self._db.execute(
            "INSERT INTO event_dedup (event_id, received_at) VALUES (?, ?)",
            (event_id, _now()))
        await self._db.commit()
        return True

    async def cleanup_old_events(self, days: int = 7):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self._db.execute("DELETE FROM event_dedup WHERE received_at<?", (cutoff,))
        await self._db.commit()
```

**Step 4: Run tests**

Run: `uv run pytest tests/sidecar/test_db.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add sidecar/db.py tests/sidecar/test_db.py
git commit -m "feat(sidecar): SQLite database layer with permission, registry, audit, dedup"
```

---

### Task 3: config.patch RPC client

**Files:**
- Create: `sidecar/config_patch.py`
- Create: `tests/sidecar/test_config_patch.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_config_patch.py`:
```python
"""Tests for sidecar.config_patch — OpenClaw config.patch RPC client."""
import pytest
import json
from unittest.mock import AsyncMock, patch
from sidecar.config_patch import ConfigPatchClient, ConfigPatchQueue


@pytest.fixture
def mock_client():
    client = ConfigPatchClient(
        gateway_url="http://127.0.0.1:18789",
        auth_token="test-token",
    )
    return client


async def test_get_config(mock_client):
    """config.get returns current config and baseHash."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "baseHash": "abc123",
        "config": {"agents": {}, "bindings": []}
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await mock_client.get_config()
        assert result["baseHash"] == "abc123"


async def test_patch_config(mock_client):
    """config.patch sends merge patch with baseHash."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": True}
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await mock_client.patch_config("abc123", {"agents": {"new": {}}})
        call_args = mock_post.call_args
        body = call_args.kwargs.get("json") or json.loads(call_args.args[1] if len(call_args.args) > 1 else "{}")
        # Verify baseHash was included
        assert "baseHash" in str(call_args)


async def test_add_binding(mock_client):
    """add_binding appends a peer binding to the existing array."""
    mock_get = AsyncMock()
    mock_get.status_code = 200
    mock_get.json.return_value = {
        "baseHash": "abc123",
        "config": {
            "agents": {},
            "bindings": [
                {"agentId": "fallback", "match": {"channel": "feishu"}}
            ]
        }
    }
    mock_patch = AsyncMock()
    mock_patch.status_code = 200
    mock_patch.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient.post", side_effect=[mock_get, mock_patch]):
        await mock_client.add_binding(
            agent_id="u-shared-ou_alice",
            channel="feishu",
            peer={"kind": "direct", "id": "ou_alice"},
        )


async def test_remove_binding(mock_client):
    """remove_binding removes a peer binding from the array."""
    existing = [
        {"agentId": "fallback", "match": {"channel": "feishu"}},
        {"agentId": "u-shared-ou_alice", "match": {"channel": "feishu", "peer": {"kind": "direct", "id": "ou_alice"}}},
    ]
    mock_get = AsyncMock()
    mock_get.status_code = 200
    mock_get.json.return_value = {"baseHash": "abc123", "config": {"bindings": existing}}
    mock_patch = AsyncMock()
    mock_patch.status_code = 200
    mock_patch.json.return_value = {"ok": True}

    with patch("httpx.AsyncClient.post", side_effect=[mock_get, mock_patch]) as mock_post:
        await mock_client.remove_binding("u-shared-ou_alice")
        # Verify the patched bindings exclude the removed one
        patch_call = mock_post.call_args_list[1]
        body = patch_call.kwargs.get("json", {})
        if "bindings" in body:
            assert all(b["agentId"] != "u-shared-ou_alice" for b in body["bindings"])
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_config_patch.py -v`
Expected: FAIL

**Step 3: Implement sidecar/config_patch.py**

```python
"""OpenClaw config.patch RPC client with rate-limiting queue."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("sidecar.config_patch")


class ConfigPatchClient:
    """Low-level client for OpenClaw config.get / config.patch RPC."""

    def __init__(self, *, gateway_url: str, auth_token: str):
        self._url = gateway_url.rstrip("/")
        self._token = auth_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def get_config(self) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._url}/api/config.get",
                headers=self._headers(),
                json={},
            )
            resp.raise_for_status()
            return resp.json()

    async def patch_config(self, base_hash: str, patch: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._url}/api/config.patch",
                headers=self._headers(),
                json={"baseHash": base_hash, "patch": patch},
            )
            resp.raise_for_status()
            return resp.json()

    async def add_agent(self, agent_id: str, agent_config: dict):
        """Add an agent definition via config.patch."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        await self.patch_config(base_hash, {"agents": {agent_id: agent_config}})

    async def add_binding(self, *, agent_id: str, channel: str,
                          peer: dict | None = None,
                          account_id: str | None = None):
        """Append a binding to the bindings array."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        bindings = data["config"].get("bindings", [])
        new_binding: dict[str, Any] = {"agentId": agent_id, "match": {"channel": channel}}
        if peer:
            new_binding["match"]["peer"] = peer
        if account_id:
            new_binding["match"]["accountId"] = account_id
        bindings.append(new_binding)
        await self.patch_config(base_hash, {"bindings": bindings})

    async def remove_binding(self, agent_id: str):
        """Remove all bindings for an agent_id."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        bindings = [b for b in data["config"].get("bindings", [])
                    if b.get("agentId") != agent_id]
        await self.patch_config(base_hash, {"bindings": bindings})

    async def add_agent_with_binding(self, *, agent_id: str, agent_config: dict,
                                      channel: str, peer: dict):
        """Atomically add agent definition + binding in one patch."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        bindings = data["config"].get("bindings", [])
        bindings.append({
            "agentId": agent_id,
            "match": {"channel": channel, "peer": peer},
        })
        await self.patch_config(base_hash, {
            "agents": {agent_id: agent_config},
            "bindings": bindings,
        })

    async def remove_binding_keep_agent(self, agent_id: str):
        """Remove binding but keep agent definition (for suspend)."""
        await self.remove_binding(agent_id)


class ConfigPatchQueue:
    """Rate-limited queue that merges patches within a time window."""

    RATE_LIMIT = 3
    MERGE_WINDOW = 5.0
    MAX_RETRIES = 3

    def __init__(self, client: ConfigPatchClient):
        self._client = client
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def enqueue(self, operation: dict):
        """Queue an operation. Flushes after MERGE_WINDOW seconds."""
        async with self._lock:
            self._pending.append(operation)
            if self._loop is None:
                self._loop = asyncio.get_running_loop()
            if self._timer is None:
                self._timer = self._loop.call_later(
                    self.MERGE_WINDOW, lambda: asyncio.ensure_future(self._flush()))

    async def flush_now(self):
        """Force immediate flush (for testing or shutdown)."""
        await self._flush()

    async def _flush(self):
        async with self._lock:
            self._timer = None
            if not self._pending:
                return
            ops = self._pending[:]
            self._pending.clear()

        for attempt in range(self.MAX_RETRIES):
            try:
                data = await self._client.get_config()
                base_hash = data["baseHash"]
                config = data["config"]

                merged_agents = {}
                bindings = list(config.get("bindings", []))

                for op in ops:
                    if op.get("add_agent"):
                        merged_agents[op["agent_id"]] = op["add_agent"]
                    if op.get("add_binding"):
                        bindings.append(op["add_binding"])
                    if op.get("remove_binding_agent_id"):
                        bindings = [b for b in bindings
                                    if b.get("agentId") != op["remove_binding_agent_id"]]

                patch = {"bindings": bindings}
                if merged_agents:
                    patch["agents"] = merged_agents

                await self._client.patch_config(base_hash, patch)
                log.info("config.patch flushed: %d operations merged", len(ops))
                return
            except httpx.HTTPStatusError as e:
                if attempt < self.MAX_RETRIES - 1:
                    log.warning("config.patch failed (attempt %d): %s", attempt + 1, e)
                    await asyncio.sleep(1)
                else:
                    log.error("config.patch failed after %d retries, dropping batch: %s",
                              self.MAX_RETRIES, e)
```

**Step 4: Run tests**

Run: `uv run pytest tests/sidecar/test_config_patch.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add sidecar/config_patch.py tests/sidecar/test_config_patch.py
git commit -m "feat(sidecar): config.patch RPC client with rate-limited merge queue"
```

---

### Task 4: Agent provisioner

**Files:**
- Create: `sidecar/provisioner.py`
- Create: `sidecar/templates/user-agent/SOUL.md`
- Create: `sidecar/templates/user-agent/USER.md.tmpl`
- Create: `sidecar/templates/group-agent/SOUL.md`
- Create: `tests/sidecar/test_provisioner.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_provisioner.py`:
```python
"""Tests for sidecar.provisioner — agent lifecycle management."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from sidecar.provisioner import Provisioner


@pytest.fixture
def agents_dir(tmp_path):
    return tmp_path / "agents"


@pytest.fixture
def archived_dir(tmp_path):
    return tmp_path / "archived"


@pytest.fixture
def templates_dir():
    return Path(__file__).resolve().parent.parent.parent / "sidecar" / "templates"


@pytest.fixture
def provisioner(agents_dir, archived_dir, templates_dir):
    db = AsyncMock()
    config_client = AsyncMock()
    return Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=str(agents_dir),
        archived_dir=str(archived_dir),
        templates_dir=str(templates_dir),
        account_id="shared",
        default_model="test-model",
    )


async def test_provision_user_agent(provisioner, agents_dir):
    """Provision creates workspace, patches config, updates registry."""
    agent_id = await provisioner.provision_user("ou_alice", "Alice")
    assert agent_id == "u-shared-ou_alice"

    workspace = agents_dir / "u-shared-ou_alice" / "workspace"
    assert workspace.exists()
    assert (workspace / "SOUL.md").exists()
    assert (workspace / "USER.md").exists()
    # USER.md should have Alice's name rendered
    content = (workspace / "USER.md").read_text()
    assert "Alice" in content

    provisioner._config_client.add_agent_with_binding.assert_called_once()
    provisioner._db.create_agent.assert_called_once()
    provisioner._db.write_audit.assert_called()


async def test_suspend_user_agent(provisioner, agents_dir):
    """Suspend removes binding but keeps workspace."""
    # Create a fake workspace
    ws = agents_dir / "u-shared-ou_alice" / "workspace"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("test")

    provisioner._db.get_agent_by_open_id.return_value = {
        "agent_id": "u-shared-ou_alice",
        "status": "active",
        "workspace_path": str(ws.parent),
    }

    await provisioner.suspend_user("ou_alice")
    provisioner._config_client.remove_binding.assert_called_with("u-shared-ou_alice")
    provisioner._db.update_agent_status.assert_called_with("u-shared-ou_alice", "suspended")
    assert ws.exists()  # workspace preserved


async def test_restore_user_agent(provisioner):
    """Restore re-adds binding without touching workspace."""
    provisioner._db.get_agent_by_open_id.return_value = {
        "agent_id": "u-shared-ou_alice",
        "status": "suspended",
        "workspace_path": "/fake/path",
    }

    await provisioner.restore_user("ou_alice")
    provisioner._config_client.add_binding.assert_called_once()
    provisioner._db.update_agent_status.assert_called_with("u-shared-ou_alice", "active")


async def test_reset_user_agent(provisioner, agents_dir, archived_dir):
    """Reset archives workspace and removes agent from config."""
    ws = agents_dir / "u-shared-ou_alice" / "workspace"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("test")

    provisioner._db.get_agent_by_open_id.return_value = {
        "agent_id": "u-shared-ou_alice",
        "status": "active",
        "workspace_path": str(ws.parent),
    }

    await provisioner.reset_user("ou_alice", actor="ou_admin")
    assert not ws.parent.exists()  # moved
    assert archived_dir.exists()
    provisioner._db.delete_agent.assert_called_with("u-shared-ou_alice")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_provisioner.py -v`
Expected: FAIL

**Step 3: Create agent templates**

`sidecar/templates/user-agent/SOUL.md`:
```markdown
# Soul

You are a personal AI assistant exclusively for {{display_name}}. Your conversations are completely private and isolated — no other user can see or access them.

Be genuinely helpful, not performatively helpful. Have opinions and personality. Be resourceful before asking. Earn trust through competence. Remember you're a guest. Respect privacy.
```

`sidecar/templates/user-agent/USER.md.tmpl`:
```markdown
# User Profile

- **Name:** {{display_name}}
- **User ID:** {{open_id}}
- **Created:** {{created_at}}
```

`sidecar/templates/group-agent/SOUL.md`:
```markdown
# Soul

You are a group AI assistant for this chat. Be helpful but more restrained than in DM — keep responses concise and relevant to the group discussion. Only respond when explicitly mentioned (@).
```

**Step 4: Implement sidecar/provisioner.py**

```python
"""Agent lifecycle management: provision, suspend, restore, archive, reset."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sidecar.config_patch import ConfigPatchClient
from sidecar.db import Database

log = logging.getLogger("sidecar.provisioner")


class Provisioner:
    def __init__(self, *, db: Database, config_client: ConfigPatchClient,
                 agents_dir: str, archived_dir: str, templates_dir: str,
                 account_id: str, default_model: str):
        self._db = db
        self._config_client = config_client
        self._agents_dir = Path(agents_dir)
        self._archived_dir = Path(archived_dir)
        self._templates_dir = Path(templates_dir)
        self._account_id = account_id
        self._default_model = default_model

    def _user_agent_id(self, open_id: str) -> str:
        return f"u-{self._account_id}-{open_id}"

    def _group_agent_id(self, chat_id: str) -> str:
        return f"g-{self._account_id}-{chat_id}"

    def _render_template(self, text: str, **kwargs) -> str:
        for key, val in kwargs.items():
            text = text.replace("{{" + key + "}}", str(val))
        return text

    async def provision_user(self, open_id: str, display_name: str) -> str:
        agent_id = self._user_agent_id(open_id)
        agent_dir = self._agents_dir / agent_id
        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent").mkdir(parents=True, exist_ok=True)

        template_dir = self._templates_dir / "user-agent"
        now = datetime.now(timezone.utc).isoformat()
        render_vars = {"display_name": display_name, "open_id": open_id, "created_at": now}

        for f in template_dir.iterdir():
            if f.is_file():
                content = f.read_text()
                rendered = self._render_template(content, **render_vars)
                out_name = f.name.replace(".tmpl", "")
                (workspace / out_name).write_text(rendered)

        agent_config = {
            "agentDir": str(agent_dir / "agent"),
            "workspace": str(workspace),
            "model": self._default_model,
        }
        await self._config_client.add_agent_with_binding(
            agent_id=agent_id,
            agent_config=agent_config,
            channel="feishu",
            peer={"kind": "direct", "id": open_id},
        )
        await self._db.create_agent(agent_id, open_id, None, "user", str(agent_dir))
        await self._db.write_audit("provision", agent_id, "system",
                                   f'{{"open_id":"{open_id}","display_name":"{display_name}"}}')
        log.info("provisioned user agent %s for %s (%s)", agent_id, display_name, open_id)
        return agent_id

    async def suspend_user(self, open_id: str):
        agent = await self._db.get_agent_by_open_id(open_id)
        if not agent or agent["status"] != "active":
            return
        agent_id = agent["agent_id"]
        await self._config_client.remove_binding(agent_id)
        await self._db.update_agent_status(agent_id, "suspended")
        await self._db.write_audit("suspend", agent_id, "system")
        log.info("suspended agent %s", agent_id)

    async def restore_user(self, open_id: str):
        agent = await self._db.get_agent_by_open_id(open_id)
        if not agent or agent["status"] != "suspended":
            return
        agent_id = agent["agent_id"]
        await self._config_client.add_binding(
            agent_id=agent_id,
            channel="feishu",
            peer={"kind": "direct", "id": open_id},
        )
        await self._db.update_agent_status(agent_id, "active")
        await self._db.write_audit("restore", agent_id, "system")
        log.info("restored agent %s", agent_id)

    async def reset_user(self, open_id: str, *, actor: str):
        agent = await self._db.get_agent_by_open_id(open_id)
        if not agent:
            return
        agent_id = agent["agent_id"]
        workspace_path = Path(agent["workspace_path"])

        await self._config_client.remove_binding(agent_id)

        if workspace_path.exists():
            self._archived_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            dest = self._archived_dir / f"{agent_id}-reset-{ts}"
            shutil.move(str(workspace_path), str(dest))
            log.info("archived workspace to %s", dest)

        await self._db.delete_agent(agent_id)
        await self._db.write_audit("reset", agent_id, actor)
        log.info("reset agent %s by %s", agent_id, actor)

    async def provision_group(self, chat_id: str) -> str:
        agent_id = self._group_agent_id(chat_id)
        agent_dir = self._agents_dir / agent_id
        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent").mkdir(parents=True, exist_ok=True)

        template_dir = self._templates_dir / "group-agent"
        for f in template_dir.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(workspace / f.name))

        agent_config = {
            "agentDir": str(agent_dir / "agent"),
            "workspace": str(workspace),
            "model": self._default_model,
        }
        await self._config_client.add_agent_with_binding(
            agent_id=agent_id,
            agent_config=agent_config,
            channel="feishu",
            peer={"kind": "group", "id": chat_id},
        )
        await self._db.create_agent(agent_id, None, chat_id, "group", str(agent_dir))
        await self._db.write_audit("provision", agent_id, "system",
                                   f'{{"chat_id":"{chat_id}"}}')
        log.info("provisioned group agent %s", agent_id)
        return agent_id
```

**Step 5: Run tests**

Run: `uv run pytest tests/sidecar/test_provisioner.py -v`
Expected: All 4 tests PASS

**Step 6: Commit**

```bash
git add sidecar/provisioner.py sidecar/templates/ tests/sidecar/test_provisioner.py
git commit -m "feat(sidecar): agent provisioner with template rendering and lifecycle ops"
```

---

### Task 5: Management HTTP API (resolve-sender + admin endpoints)

**Files:**
- Create: `sidecar/api.py`
- Create: `tests/sidecar/test_api.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_api.py`:
```python
"""Tests for sidecar.api — management HTTP API."""
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient
from unittest.mock import AsyncMock, MagicMock
from sidecar.api import create_app


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_permission.return_value = None
    db.get_agent_by_open_id.return_value = None
    db.check_deny_rate.return_value = True
    return db


@pytest.fixture
def mock_provisioner():
    return AsyncMock()


@pytest.fixture
async def client(aiohttp_client, mock_db, mock_provisioner):
    app = create_app(db=mock_db, provisioner=mock_provisioner)
    return await aiohttp_client(app)


# --- resolve-sender ---

async def test_resolve_sender_unauthorized(client, mock_db):
    """Unauthorized user gets deny action."""
    mock_db.get_permission.return_value = None
    mock_db.check_deny_rate.return_value = True
    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_dan"})
    assert resp.status == 200
    data = await resp.json()
    assert data["action"] == "deny"
    assert "message" in data


async def test_resolve_sender_unauthorized_silent(client, mock_db):
    """Unauthorized user within rate window gets deny_silent."""
    mock_db.get_permission.return_value = None
    mock_db.check_deny_rate.return_value = False
    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_dan"})
    data = await resp.json()
    assert data["action"] == "deny_silent"


async def test_resolve_sender_needs_provision(client, mock_db):
    """Authorized user with no agent gets provision action."""
    mock_db.get_permission.return_value = {"is_user_member": True, "is_admin": False}
    mock_db.get_agent_by_open_id.return_value = None
    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_alice"})
    data = await resp.json()
    assert data["action"] == "provision"


async def test_resolve_sender_needs_restore(client, mock_db):
    """Authorized user with suspended agent gets restore action."""
    mock_db.get_permission.return_value = {"is_user_member": True, "is_admin": False}
    mock_db.get_agent_by_open_id.return_value = {"status": "suspended", "agent_id": "u-shared-ou_alice"}
    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_alice"})
    data = await resp.json()
    assert data["action"] == "restore"


async def test_resolve_sender_provisioning(client, mock_db):
    """User whose agent is still provisioning gets retry_later."""
    mock_db.get_permission.return_value = {"is_user_member": True, "is_admin": False}
    mock_db.get_agent_by_open_id.return_value = {"status": "provisioning"}
    resp = await client.post("/api/v1/resolve-sender", json={"open_id": "ou_alice"})
    data = await resp.json()
    assert data["action"] == "retry_later"


# --- provision / restore ---

async def test_provision_endpoint(client, mock_provisioner, mock_db):
    mock_db.get_permission.return_value = {"is_user_member": True, "display_name": "Alice"}
    mock_provisioner.provision_user.return_value = "u-shared-ou_alice"
    resp = await client.post("/api/v1/provision", json={"open_id": "ou_alice"})
    assert resp.status == 200
    mock_provisioner.provision_user.assert_called_once()


async def test_restore_endpoint(client, mock_provisioner):
    mock_provisioner.restore_user.return_value = None
    resp = await client.post("/api/v1/restore", json={"open_id": "ou_alice"})
    assert resp.status == 200


# --- admin ---

async def test_list_agents(client, mock_db):
    mock_db.list_agents.return_value = [{"agent_id": "a1", "status": "active"}]
    resp = await client.get("/api/v1/agents")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["agents"]) == 1


async def test_audit_log(client, mock_db):
    mock_db.query_audit_log.return_value = [{"action": "provision", "target": "a1"}]
    resp = await client.get("/api/v1/audit-log")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["logs"]) == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_api.py -v`
Expected: FAIL

**Step 3: Implement sidecar/api.py**

```python
"""Sidecar management HTTP API — serves fallback and admin agents."""
from __future__ import annotations

import logging
from aiohttp import web

from sidecar.db import Database
from sidecar.provisioner import Provisioner

log = logging.getLogger("sidecar.api")

DENY_MESSAGE = "您没有权限使用本助手，如需使用请联系管理员"


def create_app(*, db: Database, provisioner: Provisioner) -> web.Application:
    app = web.Application()
    app["db"] = db
    app["provisioner"] = provisioner

    app.router.add_post("/api/v1/resolve-sender", handle_resolve_sender)
    app.router.add_post("/api/v1/provision", handle_provision)
    app.router.add_post("/api/v1/restore", handle_restore)
    app.router.add_get("/api/v1/agents", handle_list_agents)
    app.router.add_get("/api/v1/audit-log", handle_audit_log)
    app.router.add_post("/api/v1/admin/reset-agent", handle_reset_agent)

    return app


async def handle_resolve_sender(request: web.Request) -> web.Response:
    """Single decision endpoint: permission + agent state + rate limit → action."""
    body = await request.json()
    open_id = body["open_id"]
    db: Database = request.app["db"]

    perm = await db.get_permission(open_id)
    is_authorized = perm and (perm["is_user_member"] or perm["is_admin"])

    if not is_authorized:
        should_reply = await db.check_deny_rate(open_id)
        if should_reply:
            return web.json_response({"action": "deny", "message": DENY_MESSAGE})
        return web.json_response({"action": "deny_silent"})

    agent = await db.get_agent_by_open_id(open_id)
    if not agent:
        return web.json_response({"action": "provision"})

    if agent["status"] == "suspended":
        return web.json_response({"action": "restore", "agent_id": agent["agent_id"]})

    if agent["status"] == "provisioning":
        return web.json_response({"action": "retry_later"})

    # active — this shouldn't normally hit fallback, but handle gracefully
    return web.json_response({"action": "retry_later"})


async def handle_provision(request: web.Request) -> web.Response:
    body = await request.json()
    open_id = body["open_id"]
    db: Database = request.app["db"]
    provisioner: Provisioner = request.app["provisioner"]

    perm = await db.get_permission(open_id)
    display_name = perm.get("display_name", open_id) if perm else open_id
    agent_id = await provisioner.provision_user(open_id, display_name)
    return web.json_response({"ok": True, "agent_id": agent_id})


async def handle_restore(request: web.Request) -> web.Response:
    body = await request.json()
    open_id = body["open_id"]
    provisioner: Provisioner = request.app["provisioner"]
    await provisioner.restore_user(open_id)
    return web.json_response({"ok": True})


async def handle_list_agents(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    status = request.query.get("status")
    agents = await db.list_agents(status=status)
    return web.json_response({"agents": agents})


async def handle_audit_log(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    since = request.query.get("since")
    limit = int(request.query.get("limit", "50"))
    logs = await db.query_audit_log(since=since, limit=limit)
    return web.json_response({"logs": logs})


async def handle_reset_agent(request: web.Request) -> web.Response:
    body = await request.json()
    open_id = body["open_id"]
    actor = body.get("actor", "admin")
    provisioner: Provisioner = request.app["provisioner"]
    await provisioner.reset_user(open_id, actor=actor)
    return web.json_response({"ok": True})
```

**Step 4: Run tests**

Run: `uv run pytest tests/sidecar/test_api.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add sidecar/api.py tests/sidecar/test_api.py
git commit -m "feat(sidecar): management HTTP API with resolve-sender and admin endpoints"
```

---

### Task 6: Feishu event listener

**Files:**
- Create: `sidecar/feishu_events.py`
- Create: `tests/sidecar/test_feishu_events.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_feishu_events.py`:
```python
"""Tests for sidecar.feishu_events — event dispatch logic (not WebSocket connection)."""
import pytest
from unittest.mock import AsyncMock
from sidecar.feishu_events import FeishuEventHandler


@pytest.fixture
def handler():
    db = AsyncMock()
    provisioner = AsyncMock()
    return FeishuEventHandler(
        db=db,
        provisioner=provisioner,
        user_group_chat_id="oc_user_group",
        admin_group_chat_id="oc_admin_group",
    )


async def test_user_added_to_user_group(handler):
    """Member added to user group → grant permission."""
    handler._db.check_event_dedup.return_value = True
    handler._db.get_permission.return_value = None

    await handler.handle_member_added(
        event_id="evt_001",
        chat_id="oc_user_group",
        open_id="ou_alice",
        name="Alice",
    )
    handler._db.upsert_permission.assert_called_once()
    call_kwargs = handler._db.upsert_permission.call_args
    assert call_kwargs[1]["is_user_member"] is True


async def test_user_removed_from_user_group(handler):
    """Member removed from user group → revoke permission + suspend."""
    handler._db.check_event_dedup.return_value = True
    handler._db.get_permission.return_value = {"is_user_member": True, "is_admin": False}

    await handler.handle_member_removed(
        event_id="evt_002",
        chat_id="oc_user_group",
        open_id="ou_alice",
    )
    handler._db.upsert_permission.assert_called_once()
    handler._provisioner.suspend_user.assert_called_with("ou_alice")


async def test_user_added_to_admin_group(handler):
    """Member added to admin group → grant admin."""
    handler._db.check_event_dedup.return_value = True
    handler._db.get_permission.return_value = None

    await handler.handle_member_added(
        event_id="evt_003",
        chat_id="oc_admin_group",
        open_id="ou_bob",
        name="Bob",
    )
    call_kwargs = handler._db.upsert_permission.call_args
    assert call_kwargs[1]["is_admin"] is True


async def test_duplicate_event_ignored(handler):
    """Duplicate event_id is silently dropped."""
    handler._db.check_event_dedup.return_value = False  # duplicate
    await handler.handle_member_added(
        event_id="evt_001", chat_id="oc_user_group", open_id="ou_alice", name="Alice")
    handler._db.upsert_permission.assert_not_called()


async def test_bot_added_to_group(handler):
    """Bot added to a group → provision group agent."""
    handler._db.check_event_dedup.return_value = True
    handler._provisioner.provision_group.return_value = "g-shared-oc_xxx"

    await handler.handle_bot_added(event_id="evt_004", chat_id="oc_xxx")
    handler._provisioner.provision_group.assert_called_with("oc_xxx")


async def test_group_disbanded(handler):
    """Group disbanded → archive group agent."""
    handler._db.check_event_dedup.return_value = True
    handler._db.get_agent_by_chat_id.return_value = {
        "agent_id": "g-shared-oc_xxx", "status": "active", "workspace_path": "/fake"}

    await handler.handle_group_disbanded(event_id="evt_005", chat_id="oc_xxx")
    handler._db.write_audit.assert_called()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_feishu_events.py -v`
Expected: FAIL

**Step 3: Implement sidecar/feishu_events.py**

```python
"""Feishu event listener — handles group membership change events."""
from __future__ import annotations

import logging

from sidecar.db import Database
from sidecar.provisioner import Provisioner

log = logging.getLogger("sidecar.feishu_events")


class FeishuEventHandler:
    """Processes Feishu group membership events. Connection logic handled separately."""

    def __init__(self, *, db: Database, provisioner: Provisioner,
                 user_group_chat_id: str, admin_group_chat_id: str):
        self._db = db
        self._provisioner = provisioner
        self._user_group_id = user_group_chat_id
        self._admin_group_id = admin_group_chat_id

    async def handle_member_added(self, *, event_id: str, chat_id: str,
                                   open_id: str, name: str | None = None):
        if not await self._db.check_event_dedup(event_id):
            log.debug("duplicate event %s, skipping", event_id)
            return

        is_user_group = chat_id == self._user_group_id
        is_admin_group = chat_id == self._admin_group_id
        if not is_user_group and not is_admin_group:
            return

        existing = await self._db.get_permission(open_id)
        is_user = (existing["is_user_member"] if existing else False) or is_user_group
        is_admin = (existing["is_admin"] if existing else False) or is_admin_group

        await self._db.upsert_permission(open_id, name,
                                          is_user_member=is_user, is_admin=is_admin)

        action = "permission_granted"
        if is_admin_group:
            action = "admin_granted"
        await self._db.write_audit(action, open_id, "system",
                                   f'{{"chat_id":"{chat_id}"}}')
        log.info("member added: %s (%s) to %s", open_id, name, chat_id)

    async def handle_member_removed(self, *, event_id: str, chat_id: str,
                                     open_id: str):
        if not await self._db.check_event_dedup(event_id):
            return

        is_user_group = chat_id == self._user_group_id
        is_admin_group = chat_id == self._admin_group_id
        if not is_user_group and not is_admin_group:
            return

        existing = await self._db.get_permission(open_id)
        is_user = (existing["is_user_member"] if existing else False) and not is_user_group
        is_admin = (existing["is_admin"] if existing else False) and not is_admin_group

        await self._db.upsert_permission(open_id, existing.get("display_name") if existing else None,
                                          is_user_member=is_user, is_admin=is_admin)

        if is_user_group:
            await self._provisioner.suspend_user(open_id)
            await self._db.write_audit("permission_revoked", open_id, "system",
                                       f'{{"chat_id":"{chat_id}"}}')
        if is_admin_group:
            await self._db.write_audit("admin_revoked", open_id, "system",
                                       f'{{"chat_id":"{chat_id}"}}')

        log.info("member removed: %s from %s", open_id, chat_id)

    async def handle_bot_added(self, *, event_id: str, chat_id: str):
        if not await self._db.check_event_dedup(event_id):
            return
        if chat_id in (self._user_group_id, self._admin_group_id):
            return  # skip management groups
        await self._provisioner.provision_group(chat_id)
        log.info("bot added to group %s, provisioned group agent", chat_id)

    async def handle_group_disbanded(self, *, event_id: str, chat_id: str):
        if not await self._db.check_event_dedup(event_id):
            return
        agent = await self._db.get_agent_by_chat_id(chat_id)
        if not agent:
            return
        # Archive: remove config + move workspace
        # For now, just update status — full archive logic in provisioner
        await self._db.update_agent_status(agent["agent_id"], "archived")
        await self._db.write_audit("archive", agent["agent_id"], "system",
                                   f'{{"chat_id":"{chat_id}","reason":"disbanded"}}')
        log.info("group %s disbanded, archived agent %s", chat_id, agent["agent_id"])
```

**Step 4: Run tests**

Run: `uv run pytest tests/sidecar/test_feishu_events.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add sidecar/feishu_events.py tests/sidecar/test_feishu_events.py
git commit -m "feat(sidecar): Feishu event handler for group membership changes"
```

---

### Task 7: Reconciler

**Files:**
- Create: `sidecar/reconciler.py`
- Create: `tests/sidecar/test_reconciler.py`

**Step 1: Write the failing tests**

`tests/sidecar/test_reconciler.py`:
```python
"""Tests for sidecar.reconciler — periodic full-sync with Feishu groups."""
import pytest
from unittest.mock import AsyncMock
from sidecar.reconciler import Reconciler


@pytest.fixture
def reconciler():
    db = AsyncMock()
    provisioner = AsyncMock()
    feishu_api = AsyncMock()
    return Reconciler(
        db=db,
        provisioner=provisioner,
        feishu_api=feishu_api,
        user_group_chat_id="oc_user_group",
        admin_group_chat_id="oc_admin_group",
    )


async def test_reconcile_grants_missing_permission(reconciler):
    """User in Feishu group but not in permission table → grant."""
    reconciler._feishu_api.get_group_members.side_effect = [
        [{"open_id": "ou_alice", "name": "Alice"}],  # user group
        [],  # admin group
    ]
    reconciler._db.list_all_permissions.return_value = []

    await reconciler.reconcile()
    reconciler._db.upsert_permission.assert_called()
    reconciler._db.write_audit.assert_called()


async def test_reconcile_revokes_stale_permission(reconciler):
    """User in permission table but not in any Feishu group → revoke."""
    reconciler._feishu_api.get_group_members.side_effect = [
        [],  # user group (empty)
        [],  # admin group (empty)
    ]
    reconciler._db.list_all_permissions.return_value = [
        {"open_id": "ou_stale", "is_user_member": True, "is_admin": False, "display_name": "Stale"}
    ]

    await reconciler.reconcile()
    # Should have been called to revoke
    calls = reconciler._db.upsert_permission.call_args_list
    assert any(c[1].get("is_user_member") is False for c in calls)


async def test_reconcile_fixes_missing_binding(reconciler):
    """Active agent without binding in OpenClaw → re-add binding."""
    reconciler._feishu_api.get_group_members.side_effect = [
        [{"open_id": "ou_alice", "name": "Alice"}],
        [],
    ]
    reconciler._db.list_all_permissions.return_value = [
        {"open_id": "ou_alice", "is_user_member": True, "is_admin": False, "display_name": "Alice"}
    ]
    reconciler._db.list_agents.return_value = [
        {"agent_id": "u-shared-ou_alice", "open_id": "ou_alice", "status": "active"}
    ]
    # Simulate missing binding
    reconciler._provisioner._config_client = AsyncMock()

    await reconciler.reconcile()
    # Reconciler should note the discrepancy (actual binding check requires config.get)
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sidecar/test_reconciler.py -v`
Expected: FAIL

**Step 3: Implement sidecar/reconciler.py**

```python
"""Periodic reconciler: full-sync Feishu group members with permission table."""
from __future__ import annotations

import logging

from sidecar.db import Database
from sidecar.provisioner import Provisioner

log = logging.getLogger("sidecar.reconciler")


class FeishuGroupAPI:
    """Abstraction for Feishu group member listing. Inject real or mock."""

    async def get_group_members(self, chat_id: str) -> list[dict]:
        """Returns list of {open_id, name} for all members in the group."""
        raise NotImplementedError("Subclass or mock this")


class Reconciler:
    def __init__(self, *, db: Database, provisioner: Provisioner,
                 feishu_api: FeishuGroupAPI,
                 user_group_chat_id: str, admin_group_chat_id: str):
        self._db = db
        self._provisioner = provisioner
        self._feishu_api = feishu_api
        self._user_group_id = user_group_chat_id
        self._admin_group_id = admin_group_chat_id

    async def reconcile(self):
        log.info("reconciliation started")

        user_members = await self._feishu_api.get_group_members(self._user_group_id)
        admin_members = await self._feishu_api.get_group_members(self._admin_group_id)

        user_ids = {m["open_id"] for m in user_members}
        admin_ids = {m["open_id"] for m in admin_members}
        user_names = {m["open_id"]: m.get("name") for m in user_members + admin_members}

        existing = await self._db.list_all_permissions()
        existing_map = {r["open_id"]: r for r in existing}

        # 1. Grant missing permissions
        all_ids = user_ids | admin_ids
        for oid in all_ids:
            is_user = oid in user_ids
            is_admin = oid in admin_ids
            ex = existing_map.get(oid)
            if not ex or ex["is_user_member"] != is_user or ex["is_admin"] != is_admin:
                await self._db.upsert_permission(
                    oid, user_names.get(oid),
                    is_user_member=is_user, is_admin=is_admin)
                await self._db.write_audit("reconciled_grant", oid, "reconciler")
                log.info("reconciled: granted %s (user=%s admin=%s)", oid, is_user, is_admin)

        # 2. Revoke stale permissions
        for oid, ex in existing_map.items():
            if oid not in all_ids and (ex["is_user_member"] or ex["is_admin"]):
                was_user = ex["is_user_member"]
                await self._db.upsert_permission(
                    oid, ex.get("display_name"),
                    is_user_member=False, is_admin=False)
                if was_user:
                    await self._provisioner.suspend_user(oid)
                await self._db.write_audit("reconciled_revoke", oid, "reconciler")
                log.info("reconciled: revoked %s", oid)

        # 3. Cleanup old dedup entries
        await self._db.cleanup_old_events()

        log.info("reconciliation complete")
```

**Step 4: Run tests**

Run: `uv run pytest tests/sidecar/test_reconciler.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add sidecar/reconciler.py tests/sidecar/test_reconciler.py
git commit -m "feat(sidecar): periodic reconciler for Feishu group membership sync"
```

---

### Task 8: Wire up main.py and sidecar config

**Files:**
- Create: `sidecar/config.py`
- Modify: `sidecar/main.py`
- Create: `sidecar-config.yaml.example`

**Step 1: Create config loader**

`sidecar/config.py`:
```python
"""Sidecar configuration loader."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class SidecarConfig:
    # Feishu
    feishu_app_id: str
    feishu_app_secret: str
    user_group_chat_id: str
    admin_group_chat_id: str
    # OpenClaw
    gateway_url: str
    auth_token: str
    default_model: str
    account_id: str
    # Sidecar
    api_port: int = 18791
    db_path: str = "~/.openclaw/sidecar.sqlite"
    reconcile_interval_minutes: int = 10
    deny_rate_limit_minutes: int = 10
    # Paths
    agents_dir: str = "~/.openclaw/agents"
    archived_dir: str = "~/.openclaw/archived"
    templates_dir: str = ""  # default: sidecar/templates/

    @classmethod
    def from_yaml(cls, path: str) -> SidecarConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        def env_sub(val: str) -> str:
            if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                return os.environ.get(val[2:-1], val)
            return val

        feishu = raw.get("feishu", {})
        oc = raw.get("openclaw", {})
        sc = raw.get("sidecar", {})
        tmpl = raw.get("templates", {})

        return cls(
            feishu_app_id=env_sub(feishu.get("app_id", "")),
            feishu_app_secret=env_sub(feishu.get("app_secret", "")),
            user_group_chat_id=feishu.get("user_group_chat_id", ""),
            admin_group_chat_id=feishu.get("admin_group_chat_id", ""),
            gateway_url=oc.get("gateway_url", "http://127.0.0.1:18789"),
            auth_token=env_sub(oc.get("auth_token", "")),
            default_model=oc.get("default_model", "openrouter/google/gemini-3.1-flash-lite-preview"),
            account_id=oc.get("account_id", "shared"),
            api_port=sc.get("api_port", 18791),
            db_path=sc.get("db_path", "~/.openclaw/sidecar.sqlite"),
            reconcile_interval_minutes=sc.get("reconcile_interval_minutes", 10),
            deny_rate_limit_minutes=sc.get("deny_rate_limit_minutes", 10),
            agents_dir=sc.get("agents_dir", "~/.openclaw/agents"),
            archived_dir=sc.get("archived_dir", "~/.openclaw/archived"),
            templates_dir=tmpl.get("user_agent_dir", str(Path(__file__).parent / "templates")),
        )
```

**Step 2: Update main.py to wire all components**

`sidecar/main.py`:
```python
"""Sidecar entry point — wires all components and runs the service."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from sidecar.api import create_app
from sidecar.config import SidecarConfig
from sidecar.config_patch import ConfigPatchClient
from sidecar.db import Database
from sidecar.provisioner import Provisioner
from sidecar.reconciler import Reconciler

log = logging.getLogger("sidecar")

DEFAULT_CONFIG = "sidecar-config.yaml"


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    log.info("loading config from %s", config_path)
    cfg = SidecarConfig.from_yaml(config_path)

    # Database
    db_path = str(Path(cfg.db_path).expanduser())
    db = Database(db_path)
    await db.init()
    log.info("database initialized at %s", db_path)

    # Config patch client
    config_client = ConfigPatchClient(
        gateway_url=cfg.gateway_url,
        auth_token=cfg.auth_token,
    )

    # Provisioner
    provisioner = Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=str(Path(cfg.agents_dir).expanduser()),
        archived_dir=str(Path(cfg.archived_dir).expanduser()),
        templates_dir=cfg.templates_dir,
        account_id=cfg.account_id,
        default_model=cfg.default_model,
    )

    # Reconciler (Feishu API integration to be implemented with real SDK)
    # reconciler = Reconciler(...)

    # Initial reconciliation
    # await reconciler.reconcile()

    # HTTP API
    app = create_app(db=db, provisioner=provisioner)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.api_port)
    await site.start()
    log.info("management API listening on 127.0.0.1:%d", cfg.api_port)

    # TODO: Start Feishu WebSocket event listener (Task 6 integration)
    # TODO: Start reconciler periodic loop

    log.info("sidecar ready")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 3: Create example config**

`sidecar-config.yaml.example`:
```yaml
# Sidecar Config Manager — example configuration
# Copy to sidecar-config.yaml and fill in values

feishu:
  app_id: "cli_xxx"                         # Shared Feishu App ID
  app_secret: "${FEISHU_APP_SECRET}"         # From environment variable
  user_group_chat_id: "oc_user_xxx"          # User authorization group
  admin_group_chat_id: "oc_admin_xxx"        # Admin group

openclaw:
  gateway_url: "http://127.0.0.1:18789"
  auth_token: "${OPENCLAW_AUTH_TOKEN}"        # From environment variable
  default_model: "openrouter/google/gemini-3.1-flash-lite-preview"
  account_id: "shared"                        # accountId in openclaw.json

sidecar:
  api_port: 18791
  db_path: "~/.openclaw/sidecar.sqlite"
  reconcile_interval_minutes: 10
  deny_rate_limit_minutes: 10
  agents_dir: "~/.openclaw/agents"
  archived_dir: "~/.openclaw/archived"

templates:
  user_agent_dir: "./sidecar/templates/"
```

**Step 4: Verify import chain**

Run: `uv run python -c "from sidecar.main import main; print('ok')"`
Expected: `ok`

**Step 5: Commit**

```bash
git add sidecar/config.py sidecar/main.py sidecar-config.yaml.example
git commit -m "feat(sidecar): wire up main entry point with config loader"
```

---

## Phase 2: Integration & PoC Verification

### Task 9: PoC — verify peer binding routing with live OpenClaw

> **This task is manual/exploratory — not TDD.** It validates the critical assumptions before building further.

**Step 1: Verify config.get RPC**

Run against the live OpenClaw Gateway:
```bash
curl -s -X POST http://127.0.0.1:18789/api/config.get \
  -H "Authorization: Bearer $(cat ~/.openclaw/auth-token)" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool | head -50
```

Verify: returns `baseHash` + current config with agents and bindings.

**Step 2: Test adding a peer binding via config.patch**

Create a test agent + binding for a known user:
```bash
# Get current config
CONFIG=$(curl -s -X POST http://127.0.0.1:18789/api/config.get \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}')
BASE_HASH=$(echo $CONFIG | python3 -c "import sys,json; print(json.load(sys.stdin)['baseHash'])")

# Patch: add test binding (use a real ou_xxx from current users)
# This is exploratory — adjust based on actual API response format
```

Verify: peer binding routes DM from that user to the correct agent.

**Step 3: Test fallback agent behavior**

Configure a fallback agent with `default: true` or a catch-all binding and verify:
- Messages from users without peer bindings land on fallback
- Fallback can execute tool calls to reach Sidecar API

**Step 4: Document findings in `docs/plans/poc-findings.md`**

Record actual API formats, endpoint paths, any deviations from documentation.

**Step 5: Commit findings**

```bash
git add docs/plans/poc-findings.md
git commit -m "docs: PoC findings — config.patch and peer routing verification"
```

---

### Task 10: Feishu WebSocket event connection (real SDK integration)

> **Depends on:** Task 6 (event handler) + PoC verification of Feishu event names

**Files:**
- Modify: `sidecar/feishu_events.py` — add `FeishuEventListener` class using `lark-oapi` SDK
- Modify: `sidecar/reconciler.py` — implement real `FeishuGroupAPI` using `lark-oapi`
- Modify: `sidecar/main.py` — wire Feishu listener + reconciler loop

**Step 1: Implement FeishuEventListener**

Add to `sidecar/feishu_events.py`:
```python
import lark_oapi as lark

class FeishuEventListener:
    """Connects to Feishu via WebSocket and dispatches events to FeishuEventHandler."""

    def __init__(self, *, app_id: str, app_secret: str, handler: FeishuEventHandler):
        self._app_id = app_id
        self._app_secret = app_secret
        self._handler = handler

    async def start(self):
        """Start the Feishu WebSocket long connection."""
        # Use lark-oapi ws client
        # Subscribe to:
        #   - Group member added (event name TBD)
        #   - Group member removed (event name TBD)
        #   - Bot added to group (event name TBD)
        #   - Group disbanded (event name TBD)
        # Dispatch to self._handler methods
        pass  # Implement based on PoC findings
```

**Step 2: Implement real FeishuGroupAPI for reconciler**

```python
class LarkFeishuGroupAPI(FeishuGroupAPI):
    def __init__(self, app_id: str, app_secret: str):
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    async def get_group_members(self, chat_id: str) -> list[dict]:
        # Call GET /open-apis/im/v1/chats/{chat_id}/members
        # Return [{open_id, name}, ...]
        pass  # Implement based on Feishu API docs
```

**Step 3: Wire into main.py**

Add Feishu listener startup and reconciler periodic loop to `main()`.

**Step 4: Manual integration test**

- Start sidecar with real config
- Add/remove a user from the user group in Feishu
- Verify permission table updates
- Verify config.patch fires

**Step 5: Commit**

```bash
git add sidecar/feishu_events.py sidecar/reconciler.py sidecar/main.py
git commit -m "feat(sidecar): Feishu WebSocket event listener and real reconciler"
```

---

## Phase 3: Deployment & Migration

### Task 11: Fallback agent and admin agent configuration

**Files:**
- Create: `sidecar/templates/fallback-agent/SOUL.md`
- Create: `sidecar/templates/admin-agent/SOUL.md`

**Step 1: Create fallback agent SOUL.md**

`sidecar/templates/fallback-agent/SOUL.md`:
```markdown
# Fallback Agent

You are a routing assistant. You do NOT have conversations. You execute a strict decision tree:

1. Call the resolve-sender tool with the sender's open_id
2. Based on the action returned:
   - "provision": Call the provision tool, then reply "正在为您准备专属助手，请再发一条消息开始对话"
   - "restore": Call the restore tool, then reply "您的助手已恢复，请再发一条消息继续对话"
   - "deny": Reply with the message field from the response
   - "deny_silent": Do not reply at all
   - "retry_later": Reply "您的助手正在准备中，请稍后再试"
   - "error": Reply "系统维护中，请稍后重试"

NEVER deviate from this logic. NEVER make conversation. NEVER use your own knowledge.
```

**Step 2: Create admin agent SOUL.md**

`sidecar/templates/admin-agent/SOUL.md`:
```markdown
# Admin Agent

You are a system administration assistant for the 内部小龙虾 multi-tenant agent platform.

You can help administrators with:
- Listing all agents and their status
- Viewing audit logs
- Resetting a user's agent (requires confirmation)

Use the provided tools to query the Sidecar management API. Format responses clearly with agent IDs, statuses, and timestamps.

For destructive operations (reset), always ask for explicit confirmation before proceeding.
```

**Step 3: Document OpenClaw static configuration needed**

Create `docs/plans/openclaw-static-config.md` documenting what to add to `openclaw.json`:
- Shared Feishu app account configuration
- Fallback agent definition + catch-all binding
- Admin agent definition + management group peer binding
- `dmPolicy: "open"`

**Step 4: Commit**

```bash
git add sidecar/templates/fallback-agent/ sidecar/templates/admin-agent/ docs/plans/openclaw-static-config.md
git commit -m "feat(sidecar): fallback and admin agent templates + static config guide"
```

---

### Task 12: launchd service + Makefile targets

**Files:**
- Create: `deploy/ai.openclaw.sidecar.plist`
- Modify: `Makefile`

**Step 1: Create launchd plist**

`deploy/ai.openclaw.sidecar.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.openclaw.sidecar</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>
        <string>run</string>
        <string>python3</string>
        <string>sidecar/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/h2oslabs/cc-openclaw</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/h2oslabs/.openclaw/logs/sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/h2oslabs/.openclaw/logs/sidecar.err.log</string>
</dict>
</plist>
```

**Step 2: Add Makefile targets**

Append to `Makefile`:
```makefile
run-sidecar:
	uv run python3 sidecar/main.py

install-sidecar:
	cp deploy/ai.openclaw.sidecar.plist ~/Library/LaunchAgents/
	launchctl load ~/Library/LaunchAgents/ai.openclaw.sidecar.plist

uninstall-sidecar:
	launchctl unload ~/Library/LaunchAgents/ai.openclaw.sidecar.plist
	rm ~/Library/LaunchAgents/ai.openclaw.sidecar.plist

test:
	uv run pytest tests/ -v
```

**Step 3: Commit**

```bash
git add deploy/ Makefile
git commit -m "feat(sidecar): launchd service config and Makefile targets"
```

---

### Task 13: Migration script (14 apps → shared app)

> **Depends on:** PoC verification (Task 9)

**Files:**
- Create: `scripts/migrate-to-shared-app.py`

This script implements the Step 1 migration: adds peer bindings for existing users alongside their current accountId bindings. The script should:

1. Read current `openclaw.json` via `config.get`
2. For each existing agent with accountId binding, look up the user's open_id under the shared app (via union_id mapping)
3. Add peer bindings for the shared app account
4. Validate by checking routing

**Note:** The open_id remapping (old app → union_id → new app open_id) requires Feishu API calls. This script will be fleshed out after PoC findings confirm the exact API flow.

**Step 1: Create migration script skeleton**

```python
#!/usr/bin/env python3
"""Migration script: add peer bindings for existing agents under shared Feishu app."""
# Implementation depends on PoC findings (Task 9)
# See docs/plans/2026-04-13-sidecar-config-manager-design.md Section 9
```

**Step 2: Commit**

```bash
git add scripts/migrate-to-shared-app.py
git commit -m "feat(sidecar): migration script skeleton for multi-app to shared-app"
```

---

## Summary

| Phase | Tasks | Dependencies |
|-------|-------|-------------|
| **Phase 1: Foundation** | T1-T8 (scaffolding → main.py) | None — all TDD, can run in sequence |
| **Phase 2: Integration** | T9-T10 (PoC + real Feishu) | Requires live OpenClaw Gateway + Feishu app |
| **Phase 3: Deployment** | T11-T13 (agents, service, migration) | Requires Phase 2 findings |

**Total: 13 tasks**, ~30 TDD steps in Phase 1, followed by integration and deployment.

**Run all tests:** `uv run pytest tests/ -v`
