"""Feishu adapter — bridges Feishu events/API to the actor runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
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

# Max size for dedup set before clearing old entries
_SEEN_MAX = 10_000

# Path for persisting open_id -> chat_id mapping
_CHAT_ID_MAP_PATH = PROJECT_ROOT / ".openclaw" / "chat_id_map.json"


class FeishuAdapter:
    """Bridge between Feishu events/API and the actor runtime.

    Inbound: parses Feishu webhook events and routes them to actors.
    Outbound: handles TransportSend payloads by calling Feishu API.
    """

    def __init__(self, runtime: ActorRuntime, feishu_client) -> None:
        self.runtime = runtime
        self.feishu_client = feishu_client
        self._recent_sent: set[str] = set()  # echo prevention — message_ids we sent
        self._seen: set[str] = set()          # dedup — message_ids already processed
        self._ack_reactions: dict[str, str] = {}  # message_id -> reaction_id
        self._last_msg_id: dict[str, str] = {}  # chat_id -> last inbound message_id
        self._chat_id_map: dict[str, str] = self._load_chat_id_map()

        # Register transport handlers
        runtime.register_transport_handler("feishu_chat", self._handle_chat_transport)
        runtime.register_transport_handler("feishu_thread", self._handle_thread_transport)

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

                # Skip bot's own messages
                if sender_type == "app":
                    return

                msg_id = message.message_id or ""
                if msg_id in self._recent_sent:
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

                # Get sender name
                sender_name = ""
                if sender.sender_id:
                    sender_name = getattr(sender, "tenant_key", "") or sender_id

                evt = {
                    "message_id": msg_id,
                    "chat_id": chat_id,
                    "root_id": root_id,
                    "msg_type": msg_type,
                    "text": text,
                    "file_path": file_path,
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

        t = threading.Thread(target=ws_thread, daemon=True)
        t.start()
        log.info("Feishu WS thread started")

    def on_feishu_event(self, event: dict) -> None:
        """Route a parsed Feishu message event to the appropriate actor.

        - Dedup by message_id
        - Skip own messages (_recent_sent)
        - ACK reaction
        - Record chat_id mapping
        - Auto-spawn feishu actor if not exists
        - Build Message with full metadata
        - runtime.send() to the feishu actor
        """
        message_id = event.get("message_id", "")
        chat_id = event.get("chat_id", "")

        # Skip own messages
        if message_id and message_id in self._recent_sent:
            return

        # Dedup
        if message_id:
            if message_id in self._seen:
                return
            self._seen.add(message_id)
            if len(self._seen) > _SEEN_MAX:
                # Remove oldest half
                to_remove = list(self._seen)[:5000]
                self._seen -= set(to_remove)

        # ACK reaction
        if message_id:
            threading.Thread(
                target=self._send_reaction,
                args=(message_id,),
                kwargs={"track": True},
                daemon=True,
            ).start()
            self._last_msg_id[chat_id] = message_id

        # Record chat_id mapping for DMs
        open_id = event.get("user_id", "")
        chat_type = event.get("chat_type", "")
        if open_id and chat_id and chat_type == "p2p":
            self._record_chat_id(open_id, chat_id)

        root_id = event.get("root_id") or None
        address = self.resolve_actor_address(chat_id, root_id)

        # Auto-spawn actor if not present
        actor = self.runtime.lookup(address)
        if actor is None or actor.state == "ended":
            transport_type = "feishu_thread" if root_id else "feishu_chat"
            config: dict = {"chat_id": chat_id}
            if root_id:
                config["root_id"] = root_id
            self.runtime.spawn(
                address,
                "feishu_inbound",
                tag=chat_id,
                transport=Transport(type=transport_type, config=config),
            )

        # Build and deliver message
        msg_type = event.get("msg_type", "text")
        text = event.get("text", "")
        user = event.get("user", "")
        user_id = event.get("user_id", "")

        msg = Message(
            sender=f"feishu_user:{user_id}" if user_id else "feishu_user:unknown",
            payload={
                # --- content (message body) ---
                "text": text,
                "file_path": event.get("file_path", ""),
                # --- addressing (routing metadata) ---
                "chat_id": chat_id,
                "message_id": message_id,
                # --- discriminator (content format) ---
                "msg_type": msg_type,
            },
            metadata={
                "user": user,
                "user_id": user_id,
                "message_id": message_id,
                "chat_id": chat_id,
                "root_id": root_id or "",
                "msg_type": msg_type,
            },
        )
        self.runtime.send(address, msg)

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

    def _handle_chat_transport(self, actor: Actor, payload: dict) -> None:
        action = payload.get("action")
        chat_id = actor.transport.config["chat_id"] if actor.transport else ""

        # Remove ACK reaction for the last inbound message in this chat
        last_msg = self._last_msg_id.pop(chat_id, "")
        if last_msg:
            threading.Thread(target=self._remove_reaction, args=(last_msg,), daemon=True).start()

        if action is None:
            # Default: send text message
            text = payload.get("text", "")
            threading.Thread(target=self._send_message, args=(chat_id, text, None), daemon=True).start()
        elif action == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(target=self._send_reaction, args=(message_id, emoji_type), daemon=True).start()
        elif action == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(target=self._send_file, args=(payload.get("chat_id", chat_id), file_path), daemon=True).start()
        elif action == "tool_notify":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(target=self._update_card, args=(msg_id, text), daemon=True).start()
        elif action == "unpin":
            message_id = payload.get("message_id", "")
            threading.Thread(target=self.unpin_message, args=(message_id,), daemon=True).start()
        elif action == "update_anchor":
            msg_id = payload.get("msg_id", "")
            title = payload.get("title", "")
            body_text = payload.get("body_text", "")
            template = payload.get("template", "red")
            threading.Thread(
                target=self._update_anchor_card,
                args=(msg_id, title),
                kwargs={"body_text": body_text, "template": template},
                daemon=True,
            ).start()
        else:
            log.warning("_handle_chat_transport: unhandled action=%s actor=%s", action, actor.address)

    def _handle_thread_transport(self, actor: Actor, payload: dict) -> None:
        config = actor.transport.config if actor.transport else {}
        chat_id = config.get("chat_id", "")
        root_id = config.get("root_id", "")
        action = payload.get("action")

        last_msg = self._last_msg_id.pop(chat_id, "")
        if last_msg:
            threading.Thread(target=self._remove_reaction, args=(last_msg,), daemon=True).start()

        if action is None:
            text = payload.get("text", "")
            threading.Thread(target=self._send_message, args=(chat_id, text, root_id), daemon=True).start()
        elif action == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(target=self._send_reaction, args=(message_id, emoji_type), daemon=True).start()
        elif action == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(target=self._send_file, args=(payload.get("chat_id", chat_id), file_path), daemon=True).start()
        elif action == "update_title":
            msg_id = payload.get("msg_id", "")
            title = payload.get("title", "")
            threading.Thread(target=self._update_anchor_card, args=(msg_id, title), daemon=True).start()
        elif action == "tool_notify":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(target=self._update_card, args=(msg_id, text), daemon=True).start()
        elif action == "unpin":
            message_id = payload.get("message_id", "")
            threading.Thread(target=self.unpin_message, args=(message_id,), daemon=True).start()
        elif action == "update_anchor":
            msg_id = payload.get("msg_id", "")
            title = payload.get("title", "")
            body_text = payload.get("body_text", "")
            template = payload.get("template", "red")
            threading.Thread(
                target=self._update_anchor_card,
                args=(msg_id, title),
                kwargs={"body_text": body_text, "template": template},
                daemon=True,
            ).start()
        else:
            log.warning("_handle_thread_transport: unhandled action=%s actor=%s", action, actor.address)

    # ------------------------------------------------------------------
    # Feishu API methods (blocking — run in threads)
    # ------------------------------------------------------------------

    def _send_message(self, chat_id: str, text: str, thread_anchor: str | None = None) -> None:
        """Send a text message to a Feishu chat or thread. Blocking."""
        if not self.feishu_client:
            return
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
                resp = self.feishu_client.im.v1.message.reply(req)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
                resp = self.feishu_client.im.v1.message.create(req)

            if resp.success() and resp.data and resp.data.message_id:
                self._recent_sent.add(resp.data.message_id)
            elif not resp.success():
                log.warning("_send_message failed: code=%s msg=%s", resp.code, resp.msg)
        except Exception as e:
            log.warning("_send_message error: %s", e)

    def _send_file(self, chat_id: str, file_path: str) -> None:
        """Upload file to Feishu and send as file message. Blocking."""
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
                upload_resp = self.feishu_client.im.v1.file.create(upload_req)

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
            send_resp = self.feishu_client.im.v1.message.create(send_req)

            if send_resp.success() and send_resp.data and send_resp.data.message_id:
                self._recent_sent.add(send_resp.data.message_id)
                log.info("_send_file: sent %s to %s", file_name, chat_id)
            else:
                log.warning("_send_file send failed: code=%s msg=%s", send_resp.code, send_resp.msg)

        except Exception as e:
            log.warning("_send_file error: %s", e)

    def _send_reaction(self, message_id: str, emoji_type: str = "MUSCLE", *, track: bool = False) -> None:
        """Add emoji reaction to a Feishu message. Blocking."""
        if not self.feishu_client:
            log.warning("_send_reaction: no feishu_client")
            return
        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.POST)
                .uri(f"/open-apis/im/v1/messages/{message_id}/reactions")
                .token_types({lark.AccessTokenType.TENANT})
                .body({"reaction_type": {"emoji_type": emoji_type}})
                .build()
            )
            resp = self.feishu_client.request(req)
            if not resp.success():
                log.warning("Reaction failed for %s: code=%s msg=%s", message_id, resp.code, resp.msg)
                return
            log.info("Reaction sent: %s %s", emoji_type, message_id)
            if track:
                try:
                    data = json.loads(resp.raw.content)
                    reaction_id = data.get("data", {}).get("reaction_id", "")
                    if reaction_id:
                        self._ack_reactions[message_id] = reaction_id
                except Exception:
                    pass
        except Exception as e:
            log.warning("Reaction error for %s: %s", message_id, e)

    def _remove_reaction(self, message_id: str) -> None:
        """Remove an ACK reaction from a message. Blocking."""
        reaction_id = self._ack_reactions.pop(message_id, "")
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
            resp = self.feishu_client.request(req)
            if resp.success():
                log.debug("Removed ACK reaction: msg=%s", message_id)
            else:
                log.debug("Remove reaction failed: %s", resp.code)
        except Exception as e:
            log.debug("Remove reaction error: %s", e)

    def _update_card(self, msg_id: str, text: str) -> bool:
        """Update an existing tool notification card. Blocking."""
        if not self.feishu_client:
            return False
        try:
            card = self._build_tool_card(text)
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            resp = self.feishu_client.im.v1.message.patch(req)
            return resp.success()
        except Exception:
            return False

    def _update_anchor_card(self, msg_id: str, title: str, body_text: str = "", template: str = "green") -> bool:
        """Update the card content of a thread anchor message. Blocking."""
        if not self.feishu_client:
            return False
        try:
            card = {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": body_text or title}}],
            }
            body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
            req = PatchMessageRequest.builder().message_id(msg_id).request_body(body).build()
            resp = self.feishu_client.im.v1.message.patch(req)
            if resp.success():
                log.info("Updated anchor %s: %s", msg_id, title[:60])
                return True
            else:
                log.warning("Failed to update anchor %s: %s", msg_id, resp.msg)
                return False
        except Exception as e:
            log.warning("Error updating anchor: %s", e)
            return False

    def create_thread_anchor(self, chat_id: str, tag: str) -> str | None:
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
            resp = self.feishu_client.im.v1.message.create(req)
            if not (resp.success() and resp.data and resp.data.message_id):
                log.warning("Failed to create thread anchor: %s", resp.msg if resp else "no response")
                return None

            anchor_msg_id = resp.data.message_id
            self._recent_sent.add(anchor_msg_id)

            # Reply in thread to auto-create the topic
            reply_body = (
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": f"\U0001f4ac [{tag}] \u5728\u6b64\u8bdd\u9898\u4e2d\u5bf9\u8bdd"}))
                .reply_in_thread(True)
                .build()
            )
            reply_req = ReplyMessageRequest.builder().message_id(anchor_msg_id).request_body(reply_body).build()
            reply_resp = self.feishu_client.im.v1.message.reply(reply_req)
            if reply_resp.success() and reply_resp.data and reply_resp.data.message_id:
                self._recent_sent.add(reply_resp.data.message_id)
                log.info("Auto-created thread for anchor %s", anchor_msg_id)
            else:
                log.warning("Failed to auto-create thread: %s", reply_resp.msg if reply_resp else "no response")

            return anchor_msg_id

        except Exception as e:
            log.warning("Error creating thread anchor: %s", e)
            return None

    def create_tool_card(self, chat_id: str, text: str) -> str | None:
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
            resp = self.feishu_client.im.v1.message.create(req)
            if resp.success() and resp.data and resp.data.message_id:
                msg_id = resp.data.message_id
                self._recent_sent.add(msg_id)
                log.info("Created tool card for chat %s: %s", chat_id, msg_id)
                return msg_id
            log.warning("Failed to create tool card: %s", resp.msg if resp else "no response")
            return None
        except Exception as e:
            log.warning("Error creating tool card: %s", e)
            return None

    def pin_message(self, message_id: str) -> bool:
        """Pin a message in its chat. Blocking."""
        if not self.feishu_client:
            return False
        try:
            body = CreatePinRequestBody.builder().message_id(message_id).build()
            req = CreatePinRequest.builder().request_body(body).build()
            resp = self.feishu_client.im.v1.pin.create(req)
            if resp.success():
                log.info("Pinned message %s", message_id)
                return True
            else:
                log.warning("Failed to pin message %s: %s", message_id, resp.msg)
                return False
        except Exception as e:
            log.warning("Error pinning message: %s", e)
            return False

    def unpin_message(self, message_id: str) -> bool:
        """Unpin a message. Blocking."""
        if not self.feishu_client:
            return False
        try:
            req = DeletePinRequest.builder().message_id(message_id).build()
            resp = self.feishu_client.im.v1.pin.delete(req)
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
        """Download a message resource using the typed SDK API.

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
    # Chat ID map — maps open_id to DM chat_id for user routing
    # ------------------------------------------------------------------

    @staticmethod
    def _load_chat_id_map() -> dict[str, str]:
        """Read chat_id map from disk. Returns {} if file doesn't exist."""
        try:
            if _CHAT_ID_MAP_PATH.exists():
                return json.loads(_CHAT_ID_MAP_PATH.read_text())
        except Exception as e:
            log.warning("Failed to load chat_id map: %s", e)
        return {}

    def _save_chat_id_map(self) -> None:
        """Write chat_id map to disk."""
        try:
            _CHAT_ID_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CHAT_ID_MAP_PATH.write_text(json.dumps(self._chat_id_map, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning("Failed to save chat_id map: %s", e)

    def _record_chat_id(self, open_id: str, chat_id: str) -> None:
        """Record a user's DM chat_id if not already known."""
        if open_id not in self._chat_id_map:
            self._chat_id_map[open_id] = chat_id
            self._save_chat_id_map()
            log.info("Recorded chat_id mapping: %s -> %s", open_id[:16], chat_id)

    # ------------------------------------------------------------------
    # Startup notification
    # ------------------------------------------------------------------

    def send_startup_notification(self, admin_chat_id: str) -> None:
        """Send a startup notification to the admin chat. Blocking — run in thread."""
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
            resp = self.feishu_client.im.v1.message.create(req)
            if resp.success() and resp.data and resp.data.message_id:
                self._recent_sent.add(resp.data.message_id)
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
