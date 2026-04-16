"""Feishu adapter — bridges Feishu events/API to the actor runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreatePinRequest,
    CreatePinRequestBody,
    DeletePinRequest,
    GetMessageResourceRequest,
    P2ImMessageReactionCreatedV1,
    P2ImMessageReactionDeletedV1,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class FeishuAdapter:
    """Bridge between Feishu events/API and the actor runtime.

    Inbound: parses Feishu webhook events and routes them to actors.
    Outbound: handles TransportSend payloads by calling Feishu API.
    """

    def __init__(self, runtime: ActorRuntime, feishu_client) -> None:
        self.runtime = runtime
        self.feishu_client = feishu_client
        self._user_names: dict[str, str] = self._load_user_names()

        # Register transport handlers
        runtime.register_transport_handler("feishu_chat", self._handle_chat_transport)
        runtime.register_transport_handler("feishu_thread", self._handle_thread_transport)

    @staticmethod
    def _load_user_names() -> dict[str, str]:
        """Load open_id → display_name mapping from roles/roles.yaml."""
        roles_file = PROJECT_ROOT / "roles" / "roles.yaml"
        if not roles_file.exists():
            return {}
        try:
            import yaml
            with open(roles_file) as f:
                data = yaml.safe_load(f) or {}
            names: dict[str, str] = {}
            for user_info in (data.get("users") or {}).values():
                oid = user_info.get("open_id", "")
                name = user_info.get("display_name", "")
                if oid and name:
                    names[oid] = name
            return names
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def resolve_actor_address(self, chat_id: str, root_id: str | None) -> str:
        """Return the actor address for a Feishu chat or thread.

        - Main chat: feishu:{chat_id}
        - Thread:    feishu:{chat_id}:{root_id}
        """
        if root_id:
            return f"feishu:{chat_id}:{root_id}"
        return f"feishu:{chat_id}"

    def start_feishu_ws(self, app_id: str, app_secret: str) -> None:
        """Start Feishu WS listener in a background daemon thread.

        1. Create lark_oapi EventDispatcher with on_message callback
        2. Create WS client
        3. Start WS client in daemon thread with its own event loop
        """
        try:
            lark  # noqa: F841 — verify lark_oapi is importable
        except NameError:
            log.warning("lark_oapi not installed — Feishu WS listener disabled")
            return

        loop = asyncio.get_running_loop()

        def on_message(data: P2ImMessageReceiveV1) -> None:
            """Callback from Feishu WS thread — parse message and route to actor."""
            try:
                event = data.event
                sender = event.sender
                message = event.message

                sender_id = sender.sender_id.open_id if sender.sender_id else ""
                sender_type = sender.sender_type or "user"
                msg_id = message.message_id or ""
                msg_type = message.message_type or "text"

                log.info("Feishu WS recv: msg_id=%s type=%s sender=%s sender_type=%s",
                         msg_id[:20], msg_type, sender_id[:16], sender_type)

                # Skip bot's own messages
                if sender_type == "app":
                    log.info("Feishu WS skip: bot's own message %s", msg_id[:20])
                    return

                # Parse content via parsers registry
                from channel_server.adapters.feishu.parsers import parse_message
                msg_type = message.message_type or "text"
                try:
                    content_json = json.loads(message.content or "{}")
                except Exception:
                    content_json = {}
                text, file_path = parse_message(msg_type, content_json, message, self)

                chat_id = message.chat_id or ""
                root_id = message.root_id or ""
                chat_type = message.chat_type or ""

                # Get sender display name from roles.yaml, fallback to open_id
                sender_name = self._user_names.get(sender_id, sender_id)

                evt = {
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "root_id": root_id,
                    "msg_type": msg_type,
                    "text": text,
                    "file_path": file_path,  # now always "" — parsers no longer download
                    "file_key": content_json.get("file_key", "") or content_json.get("image_key", ""),
                    "user": sender_name,
                    "user_id": sender_id,
                    "chat_type": chat_type,
                }

                # Route to on_feishu_event from the main asyncio loop
                loop.call_soon_threadsafe(self.on_feishu_event, evt)
            except Exception as e:
                log.error("on_message callback error: %s", e, exc_info=True)

        def on_reaction_created(data: P2ImMessageReactionCreatedV1) -> None:
            """Callback for reaction added to a message."""
            try:
                event = data.event
                message_id = event.message_id or ""
                emoji_type = event.reaction_type.emoji_type if event.reaction_type else ""
                user_id = event.user_id.open_id if event.user_id else ""
                operator_type = event.operator_type or ""

                # Skip bot's own reactions
                if operator_type == "app":
                    return

                log.info("Reaction created: %s on %s by %s", emoji_type, message_id, user_id)

                evt = {
                    "message_id": message_id,
                    "chat_id": "",  # reaction events don't include chat_id
                    "msg_type": "reaction",
                    "text": f"[reaction:{emoji_type}]",
                    "file_path": "",
                    "user": user_id,
                    "user_id": user_id,
                    "emoji_type": emoji_type,
                    "reaction_action": "created",
                }
                loop.call_soon_threadsafe(self.on_feishu_reaction, evt)
            except Exception as e:
                log.error("on_reaction_created error: %s", e, exc_info=True)

        def on_reaction_deleted(data: P2ImMessageReactionDeletedV1) -> None:
            """Callback for reaction removed from a message."""
            try:
                event = data.event
                message_id = event.message_id or ""
                emoji_type = event.reaction_type.emoji_type if event.reaction_type else ""
                user_id = event.user_id.open_id if event.user_id else ""
                operator_type = event.operator_type or ""

                if operator_type == "app":
                    return

                log.info("Reaction deleted: %s on %s by %s", emoji_type, message_id, user_id)

                evt = {
                    "message_id": message_id,
                    "msg_type": "reaction",
                    "text": f"[reaction_removed:{emoji_type}]",
                    "user": user_id,
                    "user_id": user_id,
                    "emoji_type": emoji_type,
                    "reaction_action": "deleted",
                }
                loop.call_soon_threadsafe(self.on_feishu_reaction, evt)
            except Exception as e:
                log.error("on_reaction_deleted error: %s", e, exc_info=True)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_im_message_reaction_created_v1(on_reaction_created)
            .register_p2_im_message_reaction_deleted_v1(on_reaction_deleted)
            .build()
        )
        ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.ERROR,
        )
        # Suppress noisy Lark logger for unhandled event types
        logging.getLogger("Lark").setLevel(logging.CRITICAL)

        def ws_thread() -> None:
            # lark_oapi.ws.client captures asyncio.get_event_loop() at import time.
            # We need a fresh loop for this thread.
            import lark_oapi.ws.client as _ws_mod
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            _ws_mod.loop = new_loop
            try:
                ws_client.start()
            except Exception as e:
                log.error("Feishu WS error: %s", e)

        t = threading.Thread(target=ws_thread, daemon=True)  # compliance-exempt: WS listener needs own event loop
        t.start()
        log.info("Feishu WS thread started")

    def on_feishu_event(self, event: dict) -> None:
        """Route a parsed Feishu message event to the appropriate actor.

        Pure routing function — dedup and echo prevention are handled downstream
        (runtime.send dedup, FeishuInboundHandler echo prevention).
        """
        message_id = event.get("message_id", "")
        chat_id = event.get("chat_id", "")

        log.info("on_feishu_event: msg_id=%s chat_id=%s text=%s",
                 message_id[:20], chat_id[:20], event.get("text", "")[:40])

        # Thread routing
        root_id = event.get("root_id") or None
        address = self.resolve_actor_address(chat_id, None)
        if root_id:
            thread_addr = self.resolve_actor_address(chat_id, root_id)
            thread_actor = self.runtime.lookup(thread_addr)
            if thread_actor and thread_actor.state != "ended":
                for ds_addr in thread_actor.downstream:
                    ds = self.runtime.lookup(ds_addr)
                    if ds and ds.state == "active" and ds.transport is not None:
                        address = thread_addr
                        break

        # Auto-spawn main chat actor if needed
        actor = self.runtime.lookup(address)
        if actor is None or actor.state == "ended":
            self.runtime.spawn(
                address, "feishu_inbound", tag=chat_id,
                transport=Transport(type="feishu_chat", config={"chat_id": chat_id}),
            )

        # Build and deliver — dedup by runtime
        msg = Message(
            sender=f"feishu_user:{event.get('user_id', '') or 'unknown'}",
            payload={
                "text": event.get("text", ""),
                "file_path": event.get("file_path", ""),
                "chat_id": chat_id,
                "message_id": message_id,
                "msg_type": event.get("msg_type", "text"),
            },
            metadata={
                "user": event.get("user", ""),
                "user_id": event.get("user_id", ""),
                "message_id": message_id,
                "chat_id": chat_id,
                "root_id": root_id or "",
                "msg_type": event.get("msg_type", "text"),
                "chat_type": event.get("chat_type", ""),
            },
        )
        self.runtime.send(address, msg, message_id=message_id)

    def on_feishu_reaction(self, event: dict) -> None:
        """Route a reaction event to the appropriate actor.

        Reaction events don't include chat_id, so we find the feishu actor
        that has the message_id in context by broadcasting to all active
        feishu chat actors.
        """
        message_id = event.get("message_id", "")
        if not message_id:
            return

        emoji_type = event.get("emoji_type", "")
        user_id = event.get("user_id", "")
        action = event.get("reaction_action", "created")

        msg = Message(
            sender=f"feishu_user:{user_id}" if user_id else "feishu_user:unknown",
            payload={
                "text": event.get("text", ""),
                "msg_type": "reaction",
                "message_id": message_id,
                "emoji_type": emoji_type,
                "reaction_action": action,
            },
            metadata={
                "user": user_id,
                "user_id": user_id,
            },
        )

        # Route to all active feishu chat actors (reaction events lack chat_id)
        for addr, actor in self.runtime.actors.items():
            if addr.startswith("feishu:") and actor.state != "ended":
                self.runtime.send(addr, msg)
                break  # typically only one main chat actor

    # ------------------------------------------------------------------
    # Outbound transport handlers
    # ------------------------------------------------------------------

    async def _handle_chat_transport(self, actor: Actor, payload: dict) -> dict | None:
        action = payload.get("action")
        chat_id = actor.transport.config["chat_id"] if actor.transport else ""

        if action is None:
            text = payload.get("text", "")
            sent_id = await self._send_message(chat_id, text, None)
            if sent_id:
                return {"_sent_msg_id": sent_id}
        elif action == "ack_react":
            message_id = payload.get("message_id", "")
            reaction_id = await self._send_reaction(message_id)
            if reaction_id:
                return {"ack_reaction_id": reaction_id}
        elif action == "remove_ack":
            await self._remove_reaction(payload.get("message_id", ""), payload.get("reaction_id", ""))
        elif action == "react":
            await self._send_reaction(payload.get("message_id", ""), payload.get("emoji_type", "THUMBSUP"))
        elif action == "send_file":
            await self._send_file(payload.get("chat_id", chat_id), payload.get("file_path", ""))
        elif action == "tool_notify":
            await self._update_card(payload.get("card_msg_id", ""), payload.get("text", ""))
        elif action == "unpin":
            await self.unpin_message(payload.get("message_id", ""))
        elif action == "update_anchor":
            await self._update_anchor_card(payload.get("msg_id", ""), payload.get("title", ""),
                                           body_text=payload.get("body_text", ""), template=payload.get("template", "red"))
        else:
            log.warning("_handle_chat_transport: unhandled action=%s actor=%s", action, actor.address)
        return None

    async def _handle_thread_transport(self, actor: Actor, payload: dict) -> dict | None:
        config = actor.transport.config if actor.transport else {}
        chat_id = config.get("chat_id", "")
        root_id = config.get("root_id", "")
        action = payload.get("action")

        if action is None:
            text = payload.get("text", "")
            sent_id = await self._send_message(chat_id, text, root_id)
            if sent_id:
                return {"_sent_msg_id": sent_id}
        elif action == "ack_react":
            message_id = payload.get("message_id", "")
            reaction_id = await self._send_reaction(message_id)
            if reaction_id:
                return {"ack_reaction_id": reaction_id}
        elif action == "remove_ack":
            await self._remove_reaction(payload.get("message_id", ""), payload.get("reaction_id", ""))
        elif action == "react":
            await self._send_reaction(payload.get("message_id", ""), payload.get("emoji_type", "THUMBSUP"))
        elif action == "send_file":
            await self._send_file(payload.get("chat_id", chat_id), payload.get("file_path", ""))
        elif action == "update_title":
            await self._update_anchor_card(payload.get("msg_id", ""), payload.get("title", ""))
        elif action == "tool_notify":
            await self._update_card(payload.get("card_msg_id", ""), payload.get("text", ""))
        elif action == "unpin":
            await self.unpin_message(payload.get("message_id", ""))
        elif action == "update_anchor":
            await self._update_anchor_card(payload.get("msg_id", ""), payload.get("title", ""),
                                           body_text=payload.get("body_text", ""), template=payload.get("template", "red"))
        else:
            log.warning("_handle_thread_transport: unhandled action=%s actor=%s", action, actor.address)
        return None

    # ------------------------------------------------------------------
    # Feishu API methods (async)
    # ------------------------------------------------------------------

    async def _send_message(self, chat_id: str, text: str, thread_anchor: str | None = None) -> str:
        """Send a text message to a Feishu chat or thread. Returns sent message_id or ''."""
        if not self.feishu_client:
            return ""
        try:
            if thread_anchor:
                body = (
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .reply_in_thread(True)
                    .build()
                )
                req = ReplyMessageRequest.builder().message_id(thread_anchor).request_body(body).build()
                resp = await self.feishu_client.im.v1.message.areply(req)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
                resp = await self.feishu_client.im.v1.message.acreate(req)

            if resp.success() and resp.data and resp.data.message_id:
                return resp.data.message_id
            elif not resp.success():
                log.warning("_send_message failed: code=%s msg=%s", resp.code, resp.msg)
        except Exception as e:
            log.warning("_send_message error: %s", e)
        return ""

    async def _send_file(self, chat_id: str, file_path: str) -> None:
        """Upload file to Feishu and send as file message."""
        if not self.feishu_client:
            return
        try:
            if not os.path.isfile(file_path):
                log.warning("_send_file: file not found: %s", file_path)
                return

            file_name = os.path.basename(file_path)

            with open(file_path, "rb") as f:
                upload_body = (
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(file_name)
                    .file(f)
                    .build()
                )
                upload_req = CreateFileRequest.builder().request_body(upload_body).build()
                upload_resp = await self.feishu_client.im.v1.file.acreate(upload_req)

            if not upload_resp.success():
                log.warning("_send_file upload failed: code=%s msg=%s", upload_resp.code, upload_resp.msg)
                return

            file_key = upload_resp.data.file_key if upload_resp.data else ""
            if not file_key:
                log.warning("_send_file: no file_key in upload response")
                return

            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}))
                .build()
            )
            send_req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            send_resp = await self.feishu_client.im.v1.message.acreate(send_req)

            if send_resp.success() and send_resp.data and send_resp.data.message_id:
                log.info("_send_file: sent %s to %s", file_name, chat_id)
            else:
                log.warning("_send_file send failed: code=%s msg=%s", send_resp.code, send_resp.msg)

        except Exception as e:
            log.warning("_send_file error: %s", e)

    async def _send_reaction(self, message_id: str, emoji_type: str = "MUSCLE") -> str:
        """Add emoji reaction to a Feishu message. Returns reaction_id or ''."""
        if not self.feishu_client:
            log.warning("_send_reaction: no feishu_client")
            return ""
        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.POST)
                .uri(f"/open-apis/im/v1/messages/{message_id}/reactions")
                .token_types({lark.AccessTokenType.TENANT})
                .body({"reaction_type": {"emoji_type": emoji_type}})
                .build()
            )
            resp = await self.feishu_client.arequest(req)
            if not resp.success():
                log.warning("Reaction failed for %s: code=%s msg=%s", message_id, resp.code, resp.msg)
                return ""
            log.info("Reaction sent: %s %s", emoji_type, message_id)
            try:
                data = json.loads(resp.raw.content)
                reaction_id = data.get("data", {}).get("reaction_id", "")
                return reaction_id
            except Exception:
                return ""
        except Exception as e:
            log.warning("Reaction error for %s: %s", message_id, e)
            return ""

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """Remove a reaction from a message."""
        if not reaction_id or not self.feishu_client:
            return
        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.DELETE)
                .uri(f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = await self.feishu_client.arequest(req)
            if resp.success():
                log.debug("Removed ACK reaction: msg=%s", message_id)
            else:
                log.debug("Remove reaction failed: %s", resp.code)
        except Exception as e:
            log.debug("Remove reaction error: %s", e)

    async def _update_card(self, msg_id: str, text: str) -> bool:
        """Update an existing tool notification card."""
        if not self.feishu_client:
            return False
        try:
            card = self._build_tool_card(text)
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            resp = await self.feishu_client.im.v1.message.apatch(req)
            return resp.success()
        except Exception:
            return False

    async def _update_anchor_card(self, msg_id: str, title: str, body_text: str = "", template: str = "green") -> bool:
        """Update the card content of a thread anchor message."""
        if not self.feishu_client:
            return False
        try:
            card = {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": body_text or title}}],
            }
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            resp = await self.feishu_client.im.v1.message.apatch(req)
            if resp.success():
                log.info("Updated anchor %s: %s", msg_id, title[:60])
                return True
            else:
                log.warning("Failed to update anchor %s: %s", msg_id, resp.msg)
                return False
        except Exception as e:
            log.warning("Error updating anchor: %s", e)
            return False

    async def create_thread_anchor(self, chat_id: str, tag: str) -> str | None:
        """Send a thread anchor card to Feishu chat, auto-create the topic thread.

        Returns the anchor message_id or None.
        """
        if not self.feishu_client:
            return None
        try:
            card = {
                "header": {"title": {"tag": "plain_text", "content": f"\U0001f7e2 [{tag}]"}, "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": f"Session [{tag}] started \u2014 reply in this thread"}}],
            }
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            )
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = await self.feishu_client.im.v1.message.acreate(req)
            if not (resp.success() and resp.data and resp.data.message_id):
                log.warning("Failed to create thread anchor: %s", resp.msg if resp else "no response")
                return None

            anchor_msg_id = resp.data.message_id

            # Reply in thread to auto-create the topic
            reply_body = (
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": f"\U0001f4ac [{tag}] \u5728\u6b64\u8bdd\u9898\u4e2d\u5bf9\u8bdd"}))
                .reply_in_thread(True)
                .build()
            )
            reply_req = ReplyMessageRequest.builder().message_id(anchor_msg_id).request_body(reply_body).build()
            reply_resp = await self.feishu_client.im.v1.message.areply(reply_req)
            if reply_resp.success() and reply_resp.data and reply_resp.data.message_id:
                log.info("Auto-created thread for anchor %s", anchor_msg_id)
            else:
                log.warning("Failed to auto-create thread: %s", reply_resp.msg if reply_resp else "no response")

            return anchor_msg_id

        except Exception as e:
            log.warning("Error creating thread anchor: %s", e)
            return None

    async def create_tool_card(self, chat_id: str, text: str) -> str | None:
        """Create an interactive card for tool notifications. Returns msg_id or None."""
        if not self.feishu_client:
            return None
        try:
            card = self._build_tool_card(text)
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            )
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = await self.feishu_client.im.v1.message.acreate(req)
            if resp.success() and resp.data and resp.data.message_id:
                msg_id = resp.data.message_id
                log.info("Created tool card for chat %s: %s", chat_id, msg_id)
                return msg_id
            log.warning("Failed to create tool card: %s", resp.msg if resp else "no response")
            return None
        except Exception as e:
            log.warning("Error creating tool card: %s", e)
            return None

    async def pin_message(self, message_id: str) -> bool:
        """Pin a message in its chat."""
        if not self.feishu_client:
            return False
        try:
            body = CreatePinRequestBody.builder().message_id(message_id).build()
            req = CreatePinRequest.builder().request_body(body).build()
            resp = await self.feishu_client.im.v1.pin.acreate(req)
            if resp.success():
                log.info("Pinned message %s", message_id)
                return True
            else:
                log.warning("Failed to pin message %s: %s", message_id, resp.msg)
                return False
        except Exception as e:
            log.warning("Error pinning message: %s", e)
            return False

    async def unpin_message(self, message_id: str) -> bool:
        """Unpin a message."""
        if not self.feishu_client:
            return False
        try:
            req = DeletePinRequest.builder().message_id(message_id).build()
            resp = await self.feishu_client.im.v1.pin.adelete(req)
            if resp.success():
                log.info("Unpinned message %s", message_id)
                return True
            else:
                log.warning("Failed to unpin message %s: %s", message_id, resp.msg)
                return False
        except Exception as e:
            log.warning("Error unpinning message: %s", e)
            return False

    def download_file(self, message_id: str, message) -> str:
        """Download a file/image/audio/media attachment from Feishu.

        Uses the typed SDK (GetMessageResourceRequest) which handles binary
        responses correctly — BaseRequest cannot.

        Returns the local file path on success, empty string on failure.
        """
        try:
            content = json.loads(message.content or "{}")
        except Exception:
            log.warning("Cannot parse message content for file download")
            return ""

        msg_type = message.message_type or ""
        chat_id = message.chat_id or "unknown"

        if msg_type == "image":
            file_key = content.get("image_key", "")
            file_name = f"{file_key}.png" if file_key else ""
            resource_type = "image"
        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", file_key or "unknown_file")
            resource_type = "file"
        elif msg_type in ("audio", "media"):
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", f"{file_key}.bin" if file_key else "media.bin")
            resource_type = "file"
        else:
            return ""

        if not file_key:
            log.warning("No file_key in %s message", msg_type)
            return ""

        return self._download_resource(message_id, file_key, resource_type, file_name, chat_id, msg_type)

    def download_image_by_key(self, message_id: str, image_key: str) -> str:
        """Download an inline image by image_key.

        Returns the local file path on success, empty string on failure.
        """
        if not self.feishu_client or not message_id or not image_key:
            return ""
        return self._download_resource(message_id, image_key, "image", f"{image_key}.png", "inline", "inline_image")

    def _download_resource(self, message_id: str, file_key: str, resource_type: str,
                           file_name: str, chat_id: str, label: str) -> str:
        """Download a message resource using the typed SDK API (sync — called from parsers).

        Returns the local file path on success, empty string on failure.
        """
        try:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            resp = self.feishu_client.im.v1.message_resource.get(req)
            if not resp.success():
                log.warning("%s download failed: code=%s msg=%s", label, resp.code, resp.msg)
                return ""

            upload_dir = PROJECT_ROOT / ".openclaw" / "uploads" / chat_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / file_name

            # Typed API returns file content via resp.file
            if resp.file:
                dest.write_bytes(resp.file.read())
            elif resp.raw and resp.raw.content:
                dest.write_bytes(resp.raw.content)
            else:
                log.warning("%s download: empty response body", label)
                return ""

            log.info("Downloaded %s -> %s (%d bytes)", label, dest, dest.stat().st_size)
            return str(dest)

        except Exception as e:
            log.error("%s download error: %s", label, e, exc_info=True)
            return ""

    # ------------------------------------------------------------------
    # Startup notification
    # ------------------------------------------------------------------

    async def send_startup_notification(self, admin_chat_id: str) -> None:
        """Send a startup notification to the admin chat."""
        if not self.feishu_client or not admin_chat_id:
            return
        try:
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(admin_chat_id)
                .msg_type("text")
                .content(json.dumps({"text": "Channel-Server online"}))
                .build()
            )
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = await self.feishu_client.im.v1.message.acreate(req)
            if resp.success() and resp.data and resp.data.message_id:
                log.info("Sent startup notification to %s", admin_chat_id)
            else:
                log.warning("Startup notification failed: %s", resp.msg if resp else "no response")
        except Exception as e:
            log.warning("Startup notification error: %s", e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_card(text: str) -> dict:
        """Build a card JSON for tool notification display."""
        return {
            "header": {"title": {"tag": "plain_text", "content": "\U0001f527 Tool Activity"}, "template": "grey"},
            "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}],
        }
