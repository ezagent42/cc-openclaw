"""SQLite database layer for the Sidecar service."""

from __future__ import annotations

import aiosqlite
from datetime import datetime, timezone, timedelta

_SCHEMA = """
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
    """Async SQLite database for sidecar state."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Permission ───────────────────────────────────────────────────

    async def upsert_permission(
        self,
        open_id: str,
        display_name: str,
        *,
        is_user_member: bool,
        is_admin: bool,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO permission (open_id, display_name, is_user_member, is_admin, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(open_id) DO UPDATE SET
                   display_name   = excluded.display_name,
                   is_user_member = excluded.is_user_member,
                   is_admin       = excluded.is_admin,
                   updated_at     = excluded.updated_at""",
            (open_id, display_name, is_user_member, is_admin, _now()),
        )
        await self._conn.commit()

    async def get_permission(self, open_id: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM permission WHERE open_id = ?", (open_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_authorized(self) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM permission WHERE is_user_member = 1 OR is_admin = 1"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_all_permissions(self) -> list[dict]:
        cur = await self._conn.execute("SELECT * FROM permission")
        return [dict(r) for r in await cur.fetchall()]

    # ── Agent registry ───────────────────────────────────────────────

    async def create_agent(
        self,
        agent_id: str,
        open_id: str,
        chat_id: str,
        agent_type: str,
        workspace_path: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO agent_registry
               (agent_id, open_id, chat_id, agent_type, status, workspace_path, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (agent_id, open_id, chat_id, agent_type, workspace_path, _now()),
        )
        await self._conn.commit()

    async def get_agent(self, agent_id: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_agent_by_open_id(self, open_id: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM agent_registry WHERE open_id = ? AND agent_type = 'user'",
            (open_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_agent_by_chat_id(self, chat_id: str) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM agent_registry WHERE chat_id = ? AND agent_type = 'group'",
            (chat_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_agent_status(self, agent_id: str, status: str) -> None:
        now = _now()
        if status == "suspended":
            await self._conn.execute(
                "UPDATE agent_registry SET status = ?, suspended_at = ? WHERE agent_id = ?",
                (status, now, agent_id),
            )
        elif status == "active":
            await self._conn.execute(
                "UPDATE agent_registry SET status = ?, restored_at = ? WHERE agent_id = ?",
                (status, now, agent_id),
            )
        else:
            await self._conn.execute(
                "UPDATE agent_registry SET status = ? WHERE agent_id = ?",
                (status, agent_id),
            )
        await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM agent_registry WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()

    async def list_agents(self, status: str | None = None) -> list[dict]:
        if status:
            cur = await self._conn.execute(
                "SELECT * FROM agent_registry WHERE status = ?", (status,)
            )
        else:
            cur = await self._conn.execute("SELECT * FROM agent_registry")
        return [dict(r) for r in await cur.fetchall()]

    # ── Audit log ────────────────────────────────────────────────────

    async def write_audit(
        self, action: str, target: str, actor: str, details: str | None = None
    ) -> None:
        await self._conn.execute(
            "INSERT INTO audit_log (timestamp, action, target, actor, details) VALUES (?, ?, ?, ?, ?)",
            (_now(), action, target, actor, details),
        )
        await self._conn.commit()

    async def query_audit_log(
        self, *, since: str | None = None, limit: int = 50
    ) -> list[dict]:
        if since:
            cur = await self._conn.execute(
                "SELECT * FROM audit_log WHERE timestamp >= ? ORDER BY id DESC LIMIT ?",
                (since, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cur.fetchall()]

    # ── Deny rate limit ──────────────────────────────────────────────

    async def check_deny_rate(
        self, open_id: str, window_minutes: int = 10
    ) -> bool:
        cur = await self._conn.execute(
            "SELECT last_denied_at, deny_count FROM deny_rate_limit WHERE open_id = ?",
            (open_id,),
        )
        row = await cur.fetchone()
        now = datetime.now(timezone.utc)

        if row is None:
            await self._conn.execute(
                "INSERT INTO deny_rate_limit (open_id, last_denied_at, deny_count) VALUES (?, ?, 1)",
                (open_id, now.isoformat()),
            )
            await self._conn.commit()
            return True

        last = datetime.fromisoformat(row["last_denied_at"])
        if now - last > timedelta(minutes=window_minutes):
            await self._conn.execute(
                "UPDATE deny_rate_limit SET last_denied_at = ?, deny_count = deny_count + 1 WHERE open_id = ?",
                (now.isoformat(), open_id),
            )
            await self._conn.commit()
            return True

        return False

    # ── Event dedup ──────────────────────────────────────────────────

    async def check_event_dedup(self, event_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT event_id FROM event_dedup WHERE event_id = ?", (event_id,)
        )
        if await cur.fetchone():
            return False
        await self._conn.execute(
            "INSERT INTO event_dedup (event_id, received_at) VALUES (?, ?)",
            (event_id, _now()),
        )
        await self._conn.commit()
        return True

    async def cleanup_old_events(self, days: int = 7) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self._conn.execute(
            "DELETE FROM event_dedup WHERE received_at < ?", (cutoff,)
        )
        await self._conn.commit()
