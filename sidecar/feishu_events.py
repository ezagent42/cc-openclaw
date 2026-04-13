"""Feishu event handler — processes group membership events.

This module handles the *dispatch logic* for Feishu events (member
added/removed, bot added, group disbanded).  The actual WebSocket
connection is Phase 2; this layer is transport-agnostic.
"""

from __future__ import annotations

import logging

from sidecar.db import Database
from sidecar.provisioner import Provisioner

log = logging.getLogger(__name__)


class FeishuEventHandler:
    """Processes Feishu group-membership events."""

    def __init__(
        self,
        *,
        db: Database,
        provisioner: Provisioner,
        user_group_chat_id: str,
        admin_group_chat_id: str,
    ) -> None:
        self.db = db
        self.provisioner = provisioner
        self.user_group_chat_id = user_group_chat_id
        self.admin_group_chat_id = admin_group_chat_id

    # ── member added ─────────────────────────────────────────────────

    async def handle_member_added(
        self,
        *,
        event_id: str,
        chat_id: str,
        open_id: str,
        name: str | None = None,
    ) -> None:
        """Member added to a group.

        1. Dedup check
        2. Determine user_group vs admin_group
        3. Preserve existing permissions
        4. upsert_permission
        5. write_audit
        """
        if not await self.db.check_event_dedup(event_id):
            return

        existing = await self.db.get_permission(open_id)

        is_user_member = bool(existing and existing["is_user_member"])
        is_admin = bool(existing and existing["is_admin"])
        display_name = name or (existing["display_name"] if existing else open_id)

        if chat_id == self.user_group_chat_id:
            is_user_member = True
            action = "permission_granted"
        elif chat_id == self.admin_group_chat_id:
            is_admin = True
            action = "admin_granted"
        else:
            log.warning("member_added for unknown group %s — ignoring", chat_id)
            return

        await self.db.upsert_permission(
            open_id, display_name, is_user_member=is_user_member, is_admin=is_admin,
        )
        await self.db.write_audit(action, f"user/{open_id}", "feishu_event")

        log.info("%s: open_id=%s chat_id=%s", action, open_id, chat_id)

    # ── member removed ───────────────────────────────────────────────

    async def handle_member_removed(
        self,
        *,
        event_id: str,
        chat_id: str,
        open_id: str,
    ) -> None:
        """Member removed from a group.

        1. Dedup check
        2. Determine which group
        3. Revoke only the relevant flag
        4. If removed from user_group → suspend_user
        5. write_audit
        """
        if not await self.db.check_event_dedup(event_id):
            return

        existing = await self.db.get_permission(open_id)
        if existing is None:
            log.warning("member_removed for unknown user %s — ignoring", open_id)
            return

        is_user_member = bool(existing["is_user_member"])
        is_admin = bool(existing["is_admin"])
        display_name = existing["display_name"]

        if chat_id == self.user_group_chat_id:
            is_user_member = False
            action = "permission_revoked"
        elif chat_id == self.admin_group_chat_id:
            is_admin = False
            action = "admin_revoked"
        else:
            log.warning("member_removed for unknown group %s — ignoring", chat_id)
            return

        await self.db.upsert_permission(
            open_id, display_name, is_user_member=is_user_member, is_admin=is_admin,
        )

        if chat_id == self.user_group_chat_id:
            await self.provisioner.suspend_user(open_id)

        await self.db.write_audit(action, f"user/{open_id}", "feishu_event")

        log.info("%s: open_id=%s chat_id=%s", action, open_id, chat_id)

    # ── bot added ────────────────────────────────────────────────────

    async def handle_bot_added(
        self,
        *,
        event_id: str,
        chat_id: str,
    ) -> None:
        """Bot added to a group (not user/admin group).

        1. Dedup check
        2. Skip if user_group or admin_group
        3. provision_group
        """
        if not await self.db.check_event_dedup(event_id):
            return

        if chat_id in (self.user_group_chat_id, self.admin_group_chat_id):
            log.info("Bot added to managed group %s — skipping provision", chat_id)
            return

        await self.provisioner.provision_group(chat_id)
        log.info("Provisioned group agent for chat_id=%s", chat_id)

    # ── group disbanded ──────────────────────────────────────────────

    async def handle_group_disbanded(
        self,
        *,
        event_id: str,
        chat_id: str,
    ) -> None:
        """Group disbanded — archive the agent.

        1. Dedup check
        2. Look up agent by chat_id
        3. Update status to archived
        4. write_audit
        """
        if not await self.db.check_event_dedup(event_id):
            return

        agent = await self.db.get_agent_by_chat_id(chat_id)
        if agent is None:
            log.warning("group_disbanded for unknown chat_id=%s — ignoring", chat_id)
            return

        agent_id = agent["agent_id"]
        await self.db.update_agent_status(agent_id, "archived")
        await self.db.write_audit(
            "group_disbanded", f"agent/{agent_id}", "feishu_event",
        )

        log.info("Archived agent %s (group disbanded)", agent_id)
