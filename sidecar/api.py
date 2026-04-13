"""Management HTTP API for the Sidecar service."""

from __future__ import annotations

from aiohttp import web

from sidecar.db import Database
from sidecar.provisioner import Provisioner

DENY_MESSAGE = "您没有权限使用本助手，如需使用请联系管理员"

_db_key = web.AppKey("db", Database)
_provisioner_key = web.AppKey("provisioner", Provisioner)


def _is_authorized(perm: dict | None) -> bool:
    if perm is None:
        return False
    return bool(perm.get("is_user_member") or perm.get("is_admin"))


async def _resolve_sender(request: web.Request) -> web.Response:
    db = request.app[_db_key]
    body = await request.json()
    open_id: str = body["open_id"]

    perm = await db.get_permission(open_id)

    if not _is_authorized(perm):
        should_send = await db.check_deny_rate(open_id)
        if should_send:
            return web.json_response({"action": "deny", "message": DENY_MESSAGE})
        return web.json_response({"action": "deny_silent"})

    agent = await db.get_agent_by_open_id(open_id)

    if agent is None:
        return web.json_response({"action": "provision"})

    status = agent["status"]
    if status == "suspended":
        return web.json_response({"action": "restore", "agent_id": agent["agent_id"]})

    # provisioning or active (shouldn't normally hit fallback)
    return web.json_response({"action": "retry_later"})


async def _provision(request: web.Request) -> web.Response:
    db = request.app[_db_key]
    provisioner = request.app[_provisioner_key]
    body = await request.json()
    open_id: str = body["open_id"]

    perm = await db.get_permission(open_id)
    display_name = perm["display_name"] if perm else open_id

    agent_id = await provisioner.provision_user(open_id, display_name)
    return web.json_response({"ok": True, "agent_id": agent_id})


async def _restore(request: web.Request) -> web.Response:
    provisioner = request.app[_provisioner_key]
    body = await request.json()
    open_id: str = body["open_id"]

    await provisioner.restore_user(open_id)
    return web.json_response({"ok": True})


async def _list_agents(request: web.Request) -> web.Response:
    db = request.app[_db_key]
    status = request.query.get("status")
    agents = await db.list_agents(status=status)
    return web.json_response({"agents": agents})


async def _audit_log(request: web.Request) -> web.Response:
    db = request.app[_db_key]
    since = request.query.get("since")
    limit = int(request.query.get("limit", 50))
    logs = await db.query_audit_log(since=since, limit=limit)
    return web.json_response({"logs": logs})


async def _admin_reset_agent(request: web.Request) -> web.Response:
    provisioner = request.app[_provisioner_key]
    body = await request.json()
    open_id: str = body["open_id"]
    actor: str = body["actor"]

    await provisioner.reset_user(open_id, actor=actor)
    return web.json_response({"ok": True})


def create_app(*, db: Database, provisioner: Provisioner) -> web.Application:
    """Create aiohttp app with all routes registered."""
    app = web.Application()
    app[_db_key] = db
    app[_provisioner_key] = provisioner

    app.router.add_post("/api/v1/resolve-sender", _resolve_sender)
    app.router.add_post("/api/v1/provision", _provision)
    app.router.add_post("/api/v1/restore", _restore)
    app.router.add_get("/api/v1/agents", _list_agents)
    app.router.add_get("/api/v1/audit-log", _audit_log)
    app.router.add_post("/api/v1/admin/reset-agent", _admin_reset_agent)

    return app
