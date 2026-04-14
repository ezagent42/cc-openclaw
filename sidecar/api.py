"""Management HTTP API for the Sidecar service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from sidecar.db import Database
from sidecar.provisioner import Provisioner

if TYPE_CHECKING:
    from sidecar.feishu_events import FeishuEventHandler

DENY_MESSAGE = "您没有权限使用本助手，如需使用请联系管理员"

_db_key = web.AppKey("db", Database)
_provisioner_key = web.AppKey("provisioner", Provisioner)
_event_handler_key: web.AppKey["FeishuEventHandler | None"] = web.AppKey(
    "event_handler",
)


def _is_authorized(perm: dict | None) -> bool:
    if perm is None:
        return False
    return bool(perm.get("is_user_member") or perm.get("is_admin"))


async def _resolve_sender(request: web.Request) -> web.Response:
    db = request.app[_db_key]
    body = await request.json()

    # ── Group resolution (chat_id provided) ──────────────────────
    chat_id: str | None = body.get("chat_id")
    if chat_id is not None:
        agent = await db.get_agent_by_chat_id(chat_id)
        if agent is not None:
            return web.json_response({"action": "active"})
        return web.json_response({"action": "provision_group"})

    # ── DM resolution (open_id provided) ─────────────────────────
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


async def _provision_group(request: web.Request) -> web.Response:
    provisioner = request.app[_provisioner_key]
    body = await request.json()
    chat_id: str = body["chat_id"]

    agent_id = await provisioner.provision_group(chat_id)
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


# ── Feishu event forwarding endpoints ─────────────────────────────


async def _event_member_added(request: web.Request) -> web.Response:
    handler = request.app[_event_handler_key]
    if handler is None:
        return web.json_response({"error": "event handler not configured"}, status=503)
    body = await request.json()
    await handler.handle_member_added(
        event_id=body["event_id"],
        chat_id=body["chat_id"],
        open_id=body["open_id"],
        name=body.get("name"),
    )
    return web.json_response({"ok": True})


async def _event_member_removed(request: web.Request) -> web.Response:
    handler = request.app[_event_handler_key]
    if handler is None:
        return web.json_response({"error": "event handler not configured"}, status=503)
    body = await request.json()
    await handler.handle_member_removed(
        event_id=body["event_id"],
        chat_id=body["chat_id"],
        open_id=body["open_id"],
    )
    return web.json_response({"ok": True})


async def _event_bot_added(request: web.Request) -> web.Response:
    handler = request.app[_event_handler_key]
    if handler is None:
        return web.json_response({"error": "event handler not configured"}, status=503)
    body = await request.json()
    await handler.handle_bot_added(
        event_id=body["event_id"],
        chat_id=body["chat_id"],
    )
    return web.json_response({"ok": True})


async def _event_group_disbanded(request: web.Request) -> web.Response:
    handler = request.app[_event_handler_key]
    if handler is None:
        return web.json_response({"error": "event handler not configured"}, status=503)
    body = await request.json()
    await handler.handle_group_disbanded(
        event_id=body["event_id"],
        chat_id=body["chat_id"],
    )
    return web.json_response({"ok": True})


def create_app(
    *,
    db: Database,
    provisioner: Provisioner,
    event_handler: "FeishuEventHandler | None" = None,
) -> web.Application:
    """Create aiohttp app with all routes registered."""
    app = web.Application()
    app[_db_key] = db
    app[_provisioner_key] = provisioner
    app[_event_handler_key] = event_handler

    app.router.add_post("/api/v1/resolve-sender", _resolve_sender)
    app.router.add_post("/api/v1/provision", _provision)
    app.router.add_post("/api/v1/provision-group", _provision_group)
    app.router.add_post("/api/v1/restore", _restore)
    app.router.add_get("/api/v1/agents", _list_agents)
    app.router.add_get("/api/v1/audit-log", _audit_log)
    app.router.add_post("/api/v1/admin/reset-agent", _admin_reset_agent)

    # Feishu event forwarding (from channel_server)
    app.router.add_post("/api/v1/event/member-added", _event_member_added)
    app.router.add_post("/api/v1/event/member-removed", _event_member_removed)
    app.router.add_post("/api/v1/event/bot-added", _event_bot_added)
    app.router.add_post("/api/v1/event/group-disbanded", _event_group_disbanded)

    return app
