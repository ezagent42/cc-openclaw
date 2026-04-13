"""Feishu event handler — processes group membership events.

This module handles the *dispatch logic* for Feishu events (member
added/removed, bot added, group disbanded), and provides the
``FeishuEventListener`` that connects to Feishu via the lark-oapi
WebSocket long-connection and dispatches events to the handler.
"""

from __future__ import annotations

import asyncio
import logging
import threading

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


# =====================================================================
# Feishu WebSocket listener — bridges lark-oapi sync callbacks to
# the async FeishuEventHandler above.
# =====================================================================


class FeishuEventListener:
    """Connects to Feishu via lark-oapi WebSocket and dispatches events."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        handler: FeishuEventHandler,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._handler = handler

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_coro(coro: object) -> None:
        """Schedule *coro* on the main asyncio loop from a sync callback thread."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
            else:
                loop.run_until_complete(coro)  # type: ignore[arg-type]
        except Exception:
            log.exception("Failed to dispatch event coroutine")

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def start(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the Feishu WebSocket long connection in a daemon thread.

        Parameters
        ----------
        loop:
            The *running* asyncio event loop used to schedule async
            handler callbacks via ``run_coroutine_threadsafe``.  When
            ``None`` the loop is captured from the calling thread.
        """
        import lark_oapi as lark  # lazy import — mirrors channel_server.py

        if loop is None:
            loop = asyncio.get_event_loop()

        handler_ref = self._handler  # capture for closures

        # -- per-event callbacks (sync, called from WS daemon thread) --

        def _on_member_added(
            event: "lark.api.im.v1.P2ImChatMemberUserAddedV1",
        ) -> None:
            event_id = (event.header.event_id if event.header else None) or ""
            data = event.event
            if data is None:
                return
            chat_id = data.chat_id or ""
            for user in data.users or []:
                open_id = (user.user_id.open_id if user.user_id else None) or ""
                name = user.name
                if not open_id:
                    continue
                asyncio.run_coroutine_threadsafe(
                    handler_ref.handle_member_added(
                        event_id=event_id,
                        chat_id=chat_id,
                        open_id=open_id,
                        name=name,
                    ),
                    loop,
                )

        def _on_member_deleted(
            event: "lark.api.im.v1.P2ImChatMemberUserDeletedV1",
        ) -> None:
            event_id = (event.header.event_id if event.header else None) or ""
            data = event.event
            if data is None:
                return
            chat_id = data.chat_id or ""
            for user in data.users or []:
                open_id = (user.user_id.open_id if user.user_id else None) or ""
                if not open_id:
                    continue
                asyncio.run_coroutine_threadsafe(
                    handler_ref.handle_member_removed(
                        event_id=event_id,
                        chat_id=chat_id,
                        open_id=open_id,
                    ),
                    loop,
                )

        def _on_bot_added(
            event: "lark.api.im.v1.P2ImChatMemberBotAddedV1",
        ) -> None:
            event_id = (event.header.event_id if event.header else None) or ""
            data = event.event
            if data is None:
                return
            chat_id = data.chat_id or ""
            asyncio.run_coroutine_threadsafe(
                handler_ref.handle_bot_added(
                    event_id=event_id,
                    chat_id=chat_id,
                ),
                loop,
            )

        def _on_disbanded(
            event: "lark.api.im.v1.P2ImChatDisbandedV1",
        ) -> None:
            event_id = (event.header.event_id if event.header else None) or ""
            data = event.event
            if data is None:
                return
            chat_id = data.chat_id or ""
            asyncio.run_coroutine_threadsafe(
                handler_ref.handle_group_disbanded(
                    event_id=event_id,
                    chat_id=chat_id,
                ),
                loop,
            )

        # -- build event dispatcher & WS client --
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_chat_member_user_added_v1(_on_member_added)
            .register_p2_im_chat_member_user_deleted_v1(_on_member_deleted)
            .register_p2_im_chat_member_bot_added_v1(_on_bot_added)
            .register_p2_im_chat_disbanded_v1(_on_disbanded)
            .build()
        )

        ws_client = lark.ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.ERROR,
        )

        # Suppress noisy "processor not found" warnings for unhandled
        # event types (reaction, message_read, etc.) — same as channel_server.
        logging.getLogger("Lark").setLevel(logging.CRITICAL)

        def _ws_thread() -> None:
            # lark_oapi.ws.client stores a module-level ``loop`` captured at
            # import time.  If it imported in the main thread (which already
            # has a running loop), start() raises "This event loop is already
            # running".  Patch it to a fresh loop for this thread.
            import lark_oapi.ws.client as _ws_mod

            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            _ws_mod.loop = new_loop
            try:
                ws_client.start()
            except Exception:
                log.exception("Feishu WS connection error")

        t = threading.Thread(target=_ws_thread, daemon=True, name="feishu-sidecar-ws")
        t.start()
        log.info("Feishu sidecar WS thread started")
