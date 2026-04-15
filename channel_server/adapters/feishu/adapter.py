"""Feishu adapter — bridges Feishu events/API to the actor runtime."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Max size for dedup set before clearing old entries
_SEEN_MAX = 10_000


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

    def on_feishu_event(self, event: dict) -> None:
        """Route a parsed Feishu message event to the appropriate actor.

        - Dedup by message_id
        - Skip own messages (_recent_sent)
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
            # Bound the set size
            if len(self._seen) >= _SEEN_MAX:
                self._seen.clear()
            self._seen.add(message_id)

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
            type=msg_type,
            payload={"text": text, "chat_id": chat_id, "message_id": message_id},
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

    # ------------------------------------------------------------------
    # Outbound transport handlers
    # ------------------------------------------------------------------

    def _handle_chat_transport(self, actor: Actor, payload: dict) -> None:
        """Send message to a Feishu chat (main conversation).

        Dispatches based on payload type:
        - text -> _send_message
        - send_file -> _send_file
        - react -> _send_reaction
        - tool_card_update -> _update_card
        """
        ptype = payload.get("type", "text")
        chat_id = actor.transport.config["chat_id"] if actor.transport else ""

        if ptype == "text":
            text = payload.get("text", "")
            threading.Thread(
                target=self._send_message,
                args=(chat_id, text, None),
                daemon=True,
            ).start()
        elif ptype == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(
                target=self._send_file,
                args=(chat_id, file_path),
                daemon=True,
            ).start()
        elif ptype == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(
                target=self._send_reaction,
                args=(message_id, emoji_type),
                daemon=True,
            ).start()
        elif ptype == "tool_card_update":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(
                target=self._update_card,
                args=(msg_id, text),
                daemon=True,
            ).start()

    def _handle_thread_transport(self, actor: Actor, payload: dict) -> None:
        """Send message to a Feishu thread.

        Similar dispatch as chat but uses thread anchor for reply_in_thread.
        Also handles: update_title -> _update_anchor_card
        """
        config = actor.transport.config if actor.transport else {}
        chat_id = config.get("chat_id", "")
        root_id = config.get("root_id", "")
        ptype = payload.get("type", "text")

        if ptype == "text":
            text = payload.get("text", "")
            threading.Thread(
                target=self._send_message,
                args=(chat_id, text, root_id),
                daemon=True,
            ).start()
        elif ptype == "send_file":
            file_path = payload.get("file_path", "")
            threading.Thread(
                target=self._send_file,
                args=(chat_id, file_path),
                daemon=True,
            ).start()
        elif ptype == "react":
            message_id = payload.get("message_id", "")
            emoji_type = payload.get("emoji_type", "THUMBSUP")
            threading.Thread(
                target=self._send_reaction,
                args=(message_id, emoji_type),
                daemon=True,
            ).start()
        elif ptype == "update_title":
            msg_id = payload.get("msg_id", "")
            title = payload.get("title", "")
            threading.Thread(
                target=self._update_anchor_card,
                args=(msg_id, title),
                daemon=True,
            ).start()
        elif ptype == "tool_card_update":
            msg_id = payload.get("card_msg_id", "")
            text = payload.get("text", "")
            threading.Thread(
                target=self._update_card,
                args=(msg_id, text),
                daemon=True,
            ).start()

    # ------------------------------------------------------------------
    # Feishu API methods (blocking — run in threads)
    # ------------------------------------------------------------------

    def _send_message(self, chat_id: str, text: str, thread_anchor: str | None = None) -> None:
        """Send a text message to a Feishu chat or thread. Blocking."""
        if not self.feishu_client:
            return
        try:
            if thread_anchor:
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
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
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
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
            from lark_oapi.api.im.v1 import (
                CreateFileRequest, CreateFileRequestBody,
                CreateMessageRequest, CreateMessageRequestBody,
            )

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

    def _send_reaction(self, message_id: str, emoji_type: str = "THUMBSUP", *, track: bool = False) -> None:
        """Add emoji reaction to a Feishu message. Blocking."""
        if not self.feishu_client:
            return
        import lark_oapi as lark
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
                log.debug("Reaction failed: %s", resp.code)
                return
            if track:
                try:
                    data = json.loads(resp.raw.content)
                    reaction_id = data.get("data", {}).get("reaction_id", "")
                    if reaction_id:
                        self._ack_reactions[message_id] = reaction_id
                except Exception:
                    pass
        except Exception as e:
            log.debug("Reaction error: %s", e)

    def _remove_reaction(self, message_id: str) -> None:
        """Remove an ACK reaction from a message. Blocking."""
        reaction_id = self._ack_reactions.pop(message_id, "")
        if not reaction_id or not self.feishu_client:
            return
        import lark_oapi as lark
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
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

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
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

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
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody,
                ReplyMessageRequest, ReplyMessageRequestBody,
            )

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
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

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
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import CreatePinRequest, CreatePinRequestBody

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

    def download_file(self, message_id: str, message) -> str:
        """Download a file/image/audio/media attachment from Feishu.

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

        try:
            import lark_oapi as lark

            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self.feishu_client.request(req)
            if not resp.success():
                log.warning("File download failed: code=%s msg=%s", resp.code, resp.msg)
                return ""

            upload_dir = PROJECT_ROOT / ".openclaw" / "uploads" / chat_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / file_name
            dest.write_bytes(resp.raw.content)
            log.info("Downloaded %s -> %s (%d bytes)", msg_type, dest, len(resp.raw.content))
            return str(dest)

        except Exception as e:
            log.error("File download error: %s", e, exc_info=True)
            return ""

    def download_image_by_key(self, message_id: str, image_key: str) -> str:
        """Download an inline image by image_key.

        Returns the local file path on success, empty string on failure.
        """
        if not self.feishu_client or not message_id or not image_key:
            return ""
        try:
            import lark_oapi as lark

            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self.feishu_client.request(req)
            if not resp.success():
                log.warning("Inline image download failed: code=%s msg=%s", resp.code, resp.msg)
                return ""

            upload_dir = PROJECT_ROOT / ".openclaw" / "uploads" / "inline"
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / f"{image_key}.png"
            dest.write_bytes(resp.raw.content)
            log.info("Downloaded inline image -> %s (%d bytes)", dest, len(resp.raw.content))
            return str(dest)
        except Exception as e:
            log.error("Inline image download error: %s", e, exc_info=True)
            return ""

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
