"""Periodic reconciler — syncs Feishu group membership to the permission table."""

from __future__ import annotations

import logging

from sidecar.db import Database
from sidecar.provisioner import Provisioner

log = logging.getLogger(__name__)


class FeishuGroupAPI:
    """Abstract base for Feishu group member listing. Override or mock."""

    async def get_group_members(self, chat_id: str) -> list[dict]:
        """Returns list of {open_id, name} for all members."""
        raise NotImplementedError


class LarkFeishuGroupAPI(FeishuGroupAPI):
    """Real Feishu API implementation using lark-oapi SDK."""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark

        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.ERROR)
            .build()
        )

    async def get_group_members(self, chat_id: str) -> list[dict]:
        """GET /open-apis/im/v1/chats/{chat_id}/members with pagination.

        Returns [{open_id, name}, ...] for every member in the chat.
        """
        from lark_oapi.api.im.v1 import GetChatMembersRequest

        members: list[dict] = []
        page_token: str | None = None

        while True:
            builder = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)

            request = builder.build()

            # The SDK client methods are synchronous — safe to call from
            # an async context because they just do HTTP.  We run them in
            # the default executor to avoid blocking the event loop.
            import asyncio

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, self._client.im.v1.chat_members.get, request,
            )

            if not response.success():
                log.error(
                    "Failed to list members for %s: code=%s msg=%s",
                    chat_id,
                    response.code,
                    response.msg,
                )
                break

            for item in response.data.items or []:
                members.append({
                    "open_id": item.member_id or "",
                    "name": item.name or "",
                })

            if response.data.has_more and response.data.page_token:
                page_token = response.data.page_token
            else:
                break

        return members


class Reconciler:
    """Periodic full-sync between Feishu group membership and the permission table."""

    def __init__(
        self,
        *,
        db: Database,
        provisioner: Provisioner,
        feishu_api: FeishuGroupAPI,
        user_group_chat_id: str,
        admin_group_chat_id: str,
    ) -> None:
        self.db = db
        self.provisioner = provisioner
        self.feishu_api = feishu_api
        self.user_group_chat_id = user_group_chat_id
        self.admin_group_chat_id = admin_group_chat_id

    async def reconcile(self) -> None:
        """Full sync:

        1. Fetch user_group and admin_group members from Feishu
        2. Get all permissions from DB
        3. Grant missing / fix inconsistent flags
        4. Revoke stale (suspend if was user_member)
        5. Cleanup old event_dedup entries
        6. Audit all corrections
        """
        # 1. Fetch current Feishu group membership
        user_members = await self.feishu_api.get_group_members(
            self.user_group_chat_id
        )
        admin_members = await self.feishu_api.get_group_members(
            self.admin_group_chat_id
        )

        # Build lookup: open_id -> {name, is_user_member, is_admin}
        feishu_state: dict[str, dict] = {}
        for m in user_members:
            oid = m["open_id"]
            feishu_state[oid] = {
                "name": m["name"],
                "is_user_member": True,
                "is_admin": False,
            }
        for m in admin_members:
            oid = m["open_id"]
            if oid in feishu_state:
                feishu_state[oid]["is_admin"] = True
            else:
                feishu_state[oid] = {
                    "name": m["name"],
                    "is_user_member": False,
                    "is_admin": True,
                }

        # 2. Get all existing permissions from DB
        db_perms = await self.db.list_all_permissions()
        db_by_id: dict[str, dict] = {p["open_id"]: p for p in db_perms}

        # 3. Grant missing / fix inconsistent flags
        for oid, desired in feishu_state.items():
            existing = db_by_id.get(oid)
            needs_upsert = (
                existing is None
                or bool(existing["is_user_member"]) != desired["is_user_member"]
                or bool(existing["is_admin"]) != desired["is_admin"]
            )
            if needs_upsert:
                await self.db.upsert_permission(
                    oid,
                    desired["name"],
                    is_user_member=desired["is_user_member"],
                    is_admin=desired["is_admin"],
                )
                await self.db.write_audit(
                    "reconciled_grant",
                    oid,
                    "reconciler",
                    details=(
                        f"user_member={desired['is_user_member']} "
                        f"admin={desired['is_admin']}"
                    ),
                )
                log.info(
                    "Reconciled grant for %s: user_member=%s admin=%s",
                    oid,
                    desired["is_user_member"],
                    desired["is_admin"],
                )

        # 4. Revoke stale: in DB but not in any Feishu group
        for oid, perm in db_by_id.items():
            if oid not in feishu_state:
                was_user = bool(perm["is_user_member"])

                await self.db.upsert_permission(
                    oid,
                    perm["display_name"],
                    is_user_member=False,
                    is_admin=False,
                )
                await self.db.write_audit(
                    "reconciled_revoke",
                    oid,
                    "reconciler",
                    details=f"was_user_member={was_user}",
                )

                if was_user:
                    try:
                        await self.provisioner.suspend_user(oid)
                    except ValueError:
                        log.warning(
                            "Could not suspend agent for %s (no agent found)", oid
                        )

                log.info("Reconciled revoke for %s", oid)

        # 5. Cleanup old event_dedup entries
        await self.db.cleanup_old_events()

        log.info("Reconciliation complete")
