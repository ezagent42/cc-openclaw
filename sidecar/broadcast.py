"""Feishu DM broadcaster using tenant_access_token."""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("sidecar.broadcast")


class FeishuBroadcaster:
    """Send DMs to users via the shared Feishu app's tenant_access_token."""

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = None

    def _get_client(self):
        if self._client is None:
            import lark_oapi as lark
            self._client = (
                lark.Client.builder()
                .app_id(self._app_id)
                .app_secret(self._app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    async def send_dm(self, open_id: str, text: str):
        """Send a text DM to a user via tenant_access_token."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_dm_sync, open_id, text)

    def _send_dm_sync(self, open_id: str, text: str):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        client = self._get_client()
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )

        resp = client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"Failed to send DM to {open_id}: {resp.code} {resp.msg}")
        log.info("Broadcast DM sent to %s", open_id)
