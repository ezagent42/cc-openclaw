"""
channel-server: standalone WebSocket daemon for multi-instance message routing.

Listens on a local port (random by default) and routes messages between
Feishu inbound connections and registered channel.py instances.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = PROJECT_ROOT / ".feishu-credentials.json"
ACK_EMOJI = "OnIt"

log = logging.getLogger("channel-server")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Instance:
    """A registered channel.py client."""
    ws: ServerConnection
    instance_id: str
    role: str                          # "developer" | "agent"
    chat_ids: list[str]
    runtime_mode: str = "discussion"   # "discussion" | "admin"
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Channel Server
# ---------------------------------------------------------------------------

class ChannelServer:
    """Local WebSocket server that routes messages between instances."""

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 0,
        feishu_enabled: bool = True,
        admin_chat_id: str | None = None,
        sidecar_url: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.feishu_enabled = feishu_enabled
        self.admin_chat_id = admin_chat_id
        self.sidecar_url = sidecar_url or os.environ.get(
            "SIDECAR_URL", "http://127.0.0.1:18791"
        )
        self.pidfile = PROJECT_ROOT / ".channel-server.pid"

        # Route tables
        self.exact_routes: dict[str, Instance] = {}      # chat_id -> Instance
        self.wildcard_instances: list[Instance] = []      # role=developer, chat_ids=["*"]

        # ws -> Instance reverse lookup (for disconnect cleanup)
        self._ws_to_instance: dict[ServerConnection, Instance] = {}

        self._stop_event = asyncio.Event()
        self._server: websockets.asyncio.server.Server | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket server (and optionally the Feishu connection)."""
        self._stop_event.clear()

        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=20,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.pidfile.write_text(f"{os.getpid()}:{self.port}")
        log.info("WebSocket server listening on %s:%s", self.host, self.port)

        if self.feishu_enabled:
            task = asyncio.create_task(self._run_feishu_safe(), name="feishu-ws")
            self._tasks.append(task)

        await self._notify_admin("Channel-Server online")

    async def stop(self) -> None:
        """Gracefully shut down."""
        log.info("Shutting down channel-server ...")
        self._stop_event.set()
        self.pidfile.unlink(missing_ok=True)

        # Cancel background tasks
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        # Close WebSocket server
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        await self._notify_admin("Channel-Server offline")
        log.info("Channel-server stopped.")

    # ------------------------------------------------------------------
    # Feishu integration
    # ------------------------------------------------------------------

    # -- Feishu state (initialised in _run_feishu) ----------------------

    _feishu_client: object | None = None      # lark.Client (typed loosely to avoid import at module level)
    _bot_open_id: str | None = None
    _seen: set[str] = set()
    _recent_sent: set[str] = set()
    _user_cache: dict[str, str] = {}          # open_id -> display name
    _chat_modes: dict[str, str] = {}          # chat_id -> "discussion" | "admin"
    _known_chats: dict[str, dict] = {}         # chat_id → {"user": ..., "source": ..., "label": ...}
    _msg_counter: dict[str, int] = {"sent": 0, "received": 0}
    _ack_reactions: dict[str, str] = {}        # message_id → reaction_id (for removal after reply)
    _last_msg_id: dict[str, str] = {}          # chat_id → last inbound message_id

    # -- Credentials ----------------------------------------------------

    @staticmethod
    def _load_credentials() -> tuple[str, str]:
        """Read Feishu app credentials from env vars or .feishu-credentials.json."""
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if app_id and app_secret:
            return app_id, app_secret
        # Search: project root, then git toplevel (for worktrees)
        search_paths = [CREDENTIALS_PATH]
        try:
            import subprocess
            git_root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
            ).decode().strip()
            search_paths.append(Path(git_root) / ".feishu-credentials.json")
        except Exception:
            pass
        for p in search_paths:
            if p.exists():
                creds = json.loads(p.read_text())
                log.info("Loaded Feishu credentials from %s", p)
                return creds["app_id"], creds["app_secret"]
        raise RuntimeError(
            "Missing Feishu credentials — set FEISHU_APP_ID/FEISHU_APP_SECRET "
            f"or create .feishu-credentials.json (searched: {[str(p) for p in search_paths]})"
        )

    # -- User resolution ------------------------------------------------

    def _resolve_user(self, open_id: str) -> str:
        """Look up user name via cache / Feishu API. Returns 'Name (open_id…)' or bare open_id."""
        if open_id in self._user_cache:
            return self._user_cache[open_id]

        import lark_oapi as lark  # lazy

        name = ""
        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self._feishu_client.request(req)
            if resp.success():
                user_data = json.loads(resp.raw.content).get("data", {}).get("user", {})
                name = user_data.get("name", "")
        except Exception as e:
            log.debug("User lookup error for %s: %s", open_id, e)

        display = f"{name} ({open_id[:12]})" if name else open_id
        self._user_cache[open_id] = display
        return display

    # -- Reaction helper ------------------------------------------------

    def _send_reaction(self, message_id: str, emoji_type: str = ACK_EMOJI, *, track: bool = False) -> None:
        """Add emoji reaction to a Feishu message. Blocking, meant for daemon thread."""
        import lark_oapi as lark  # lazy

        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.POST)
                .uri(f"/open-apis/im/v1/messages/{message_id}/reactions")
                .token_types({lark.AccessTokenType.TENANT})
                .body({"reaction_type": {"emoji_type": emoji_type}})
                .build()
            )
            resp = self._feishu_client.request(req)
            if not resp.success():
                log.debug("Reaction failed: %s", resp.code)
                return
            # Store reaction_id for later removal
            if track:
                try:
                    data = json.loads(resp.raw.content)
                    reaction_id = data.get("data", {}).get("reaction_id", "")
                    if reaction_id:
                        self._ack_reactions[message_id] = reaction_id
                        log.debug("Tracked ACK reaction: msg=%s reaction=%s", message_id, reaction_id)
                except Exception:
                    pass
        except Exception as e:
            log.debug("Reaction error: %s", e)

    def _remove_reaction(self, message_id: str) -> None:
        """Remove the ACK reaction from a message. Blocking, meant for daemon thread."""
        reaction_id = self._ack_reactions.pop(message_id, "")
        if not reaction_id:
            return

        import lark_oapi as lark  # lazy

        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.DELETE)
                .uri(f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self._feishu_client.request(req)
            if resp.success():
                log.debug("Removed ACK reaction: msg=%s", message_id)
            else:
                log.debug("Remove reaction failed: %s", resp.code)
        except Exception as e:
            log.debug("Remove reaction error: %s", e)

    # -- File download ---------------------------------------------------

    def _download_feishu_file(self, message_id: str, message) -> str:
        """Download a file/image/audio/media attachment from Feishu.

        Returns the local file path on success, empty string on failure.
        Files are saved to .openclaw/uploads/{chat_id}/{file_name}.
        """
        try:
            content = json.loads(message.content or "{}")
        except Exception:
            log.warning("Cannot parse message content for file download")
            return ""

        msg_type = message.message_type or ""
        chat_id = message.chat_id or "unknown"

        # Determine file_key and file_name based on message type
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

        # Download via Feishu API
        try:
            import lark_oapi as lark

            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self._feishu_client.request(req)
            if not resp.success():
                log.warning("File download failed: code=%s msg=%s", resp.code, resp.msg)
                return ""

            # Save to .openclaw/uploads/{chat_id}/
            upload_dir = PROJECT_ROOT / ".openclaw" / "uploads" / chat_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = upload_dir / file_name

            # Write raw bytes
            dest.write_bytes(resp.raw.content)
            log.info("Downloaded %s → %s (%d bytes)", msg_type, dest, len(resp.raw.content))
            return str(dest)

        except Exception as e:
            log.error("File download error: %s", e, exc_info=True)
            return ""

    # -- Admin message handling -----------------------------------------

    async def _handle_admin_message(self, msg: dict) -> None:
        """Process slash-commands from the admin chat."""
        text = msg.get("text", "").strip()

        if text == "/help":
            await self._reply_feishu(msg["chat_id"], self.help_text())
            return

        if text == "/status":
            await self._reply_feishu(msg["chat_id"], self.status_text())
            return

        if text.startswith("/inject"):
            # /inject #N <text> or /inject <chat_id> <text>
            parts = text.split(None, 2)
            if len(parts) < 3:
                await self._reply_feishu(msg["chat_id"], "Usage: /inject #N <text>  or  /inject <chat_id> <text>")
                return
            raw_target = parts[1]
            target_chat_id = self._resolve_chat_target(raw_target)
            if target_chat_id is None:
                await self._reply_feishu(msg["chat_id"], f"Unknown target: {raw_target}\nUse /status to see active chats with #N numbers.")
                return
            injected_text = parts[2]
            injected_msg = {
                "type": "message",
                "chat_id": target_chat_id,
                "text": injected_text,
                "message_id": f"inject_{datetime.now(timezone.utc).timestamp():.0f}",
                "user": "admin-inject",
                "user_id": "",
                "runtime_mode": "discussion",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await self.route_message(target_chat_id, injected_msg)
            await self._reply_feishu(msg["chat_id"], f"Injected to {target_chat_id}")
            return

        if text.startswith("/explain"):
            query = text[len("/explain"):].strip()
            if not query:
                await self._reply_feishu(msg["chat_id"], "Usage: /explain <场景描述>\n  Example: /explain 用户问DID号码的价格")
                return
            await self._reply_feishu(msg["chat_id"], f"🔍 正在分析流程: {query[:50]}...")
            explain_msg = {
                "type": "message",
                "chat_id": "admin_explain",
                "text": query,
                "message_id": f"explain_{datetime.now(timezone.utc).timestamp():.0f}",
                "user": "admin",
                "user_id": "",
                "runtime_mode": "explain",
                "source": "admin",
                "admin_chat_id": self.admin_chat_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await self.route_message("admin_explain", explain_msg)
            return

        # Unknown command
        cmd = text.split()[0] if text else text
        await self._reply_feishu(msg["chat_id"], f"未知命令: {cmd}\n发送 /help 查看可用命令")

    # -- Feishu reply helper --------------------------------------------

    async def _reply_feishu(self, chat_id: str, text: str) -> None:
        """Send a text message to a Feishu chat. Best-effort."""
        if self._feishu_client is None:
            log.debug("_reply_feishu: no feishu client")
            return

        def _do_send():
            try:
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
                resp = self._feishu_client.im.v1.message.create(req)
                if resp.success() and resp.data and resp.data.message_id:
                    self._recent_sent.add(resp.data.message_id)
                    self._msg_counter["sent"] += 1
            except Exception as e:
                log.warning("_reply_feishu error: %s", e)

        threading.Thread(target=_do_send, daemon=True).start()

    # -- Main Feishu loop -----------------------------------------------

    async def _run_feishu_safe(self) -> None:
        """Wrapper that logs errors instead of silently swallowing them."""
        try:
            await self._run_feishu()
        except Exception as e:
            log.error("Feishu integration failed: %s", e, exc_info=True)

    async def _run_feishu(self) -> None:
        """Connect to Feishu via WebSocket, consume messages and route them."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            P2ImMessageReceiveV1,
        )

        # --- credentials + client ---
        app_id, app_secret = self._load_credentials()

        self._feishu_client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        # --- per-instance mutable state (avoid class-var sharing) ---
        self._seen = set()
        self._recent_sent = set()
        self._user_cache = {}
        self._chat_modes = {}
        self._known_chats = {}
        self._msg_counter = {"sent": 0, "received": 0}

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        # --- on_message callback (runs in WS thread) ---
        def on_message(data: P2ImMessageReceiveV1):
            event = data.event
            sender = event.sender
            message = event.message

            sender_id = sender.sender_id.open_id if sender.sender_id else ""
            sender_type = sender.sender_type or "user"

            # Detect bot open_id
            if sender_type == "app" and not self._bot_open_id:
                self._bot_open_id = sender_id
            # Skip bot's own messages
            if sender_type == "app" or (self._bot_open_id and sender_id == self._bot_open_id):
                return

            msg_id = message.message_id or ""
            if msg_id in self._seen or msg_id in self._recent_sent:
                return
            # Bound the _seen set
            if len(self._seen) > 10000:
                self._seen.clear()
            self._seen.add(msg_id)

            # ACK reaction (tracked for removal after reply)
            threading.Thread(target=self._send_reaction, args=(msg_id,), kwargs={"track": True}, daemon=True).start()

            # Parse message content via extensible parser registry
            from feishu.message_parsers import parse_message
            msg_type = message.message_type or "text"
            try:
                content_json = json.loads(message.content or "{}")
            except Exception:
                content_json = {}
            text, file_path = parse_message(msg_type, content_json, message, self)

            chat_id = message.chat_id or ""
            ts = datetime.now(tz=timezone.utc).isoformat()
            if message.create_time:
                try:
                    ts = datetime.fromtimestamp(
                        int(message.create_time) / 1000, tz=timezone.utc
                    ).isoformat()
                except Exception:
                    pass

            # Resolve user name
            display_name = self._resolve_user(sender_id)

            # Track first-message from a chat_id for admin notification
            is_new_chat = chat_id and chat_id not in self._known_chats
            if is_new_chat:
                # Determine label
                label = display_name.split(" (")[0] if display_name else "unknown"
                if chat_id == self.admin_chat_id:
                    label = "管理群"
                self._known_chats[chat_id] = {
                    "user": label,
                    "source": "feishu",
                }

            # Mode switching commands
            text_stripped = text.strip().lower()
            if text_stripped == "/admin":
                self._chat_modes[chat_id] = "admin"
                threading.Thread(target=self._send_reaction, args=(msg_id, "DONE"), daemon=True).start()
                msg = {
                    "type": "message",
                    "text": "[MODE SWITCH] 已切换到 admin 模式。你现在拥有完全权限。发送 /discussion 回到讨论模式。",
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "user": display_name,
                    "user_id": sender_id,
                    "runtime_mode": "admin",
                    "ts": ts,
                }
                loop.call_soon_threadsafe(queue.put_nowait, msg)
                return
            elif text_stripped == "/discussion":
                self._chat_modes[chat_id] = "discussion"
                threading.Thread(target=self._send_reaction, args=(msg_id, "DONE"), daemon=True).start()
                msg = {
                    "type": "message",
                    "text": "[MODE SWITCH] 已切换到 discussion 模式。",
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "user": display_name,
                    "user_id": sender_id,
                    "runtime_mode": "discussion",
                    "ts": ts,
                }
                loop.call_soon_threadsafe(queue.put_nowait, msg)
                return

            current_mode = self._chat_modes.get(chat_id, "discussion")
            msg = {
                "type": "message",
                "text": text,
                "chat_id": chat_id,
                "message_id": msg_id,
                "user": display_name,
                "user_id": sender_id,
                "source": "feishu",
                "runtime_mode": current_mode,
                "ts": ts,
            }
            if file_path:
                msg["file_path"] = file_path
            # Track last message_id per chat for ACK reaction removal on reply
            if msg_id and chat_id:
                self._last_msg_id[chat_id] = msg_id
            log.info("[feishu] %s: %s", display_name, text[:60])
            loop.call_soon_threadsafe(queue.put_nowait, msg)
            self._msg_counter["received"] += 1

            # Notify admin about new user's first message
            if is_new_chat and self.admin_chat_id and chat_id != self.admin_chat_id:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"_admin_notify": f"New chat: {chat_id} from {display_name}"},
                )

        # --- Group membership event callbacks (forward to Sidecar API) ---
        sidecar_url = self.sidecar_url

        def _forward_to_sidecar(endpoint: str, payload: dict) -> None:
            """POST an event payload to the Sidecar API (sync, runs in WS thread)."""
            try:
                import httpx
                httpx.post(
                    f"{sidecar_url}/api/v1/event/{endpoint}",
                    json=payload,
                    timeout=5.0,
                )
            except Exception as exc:
                log.error("Failed to forward %s event to sidecar: %s", endpoint, exc)

        def on_member_added(data) -> None:
            event = data.event
            if event is None:
                return
            event_id = (data.header.event_id if data.header else None) or ""
            chat_id = event.chat_id or ""
            for user in event.users or []:
                open_id = (user.user_id.open_id if user.user_id else None) or ""
                name = user.name or ""
                if not open_id:
                    continue
                _forward_to_sidecar("member-added", {
                    "event_id": event_id,
                    "chat_id": chat_id,
                    "open_id": open_id,
                    "name": name,
                })

        def on_member_deleted(data) -> None:
            event = data.event
            if event is None:
                return
            event_id = (data.header.event_id if data.header else None) or ""
            chat_id = event.chat_id or ""
            for user in event.users or []:
                open_id = (user.user_id.open_id if user.user_id else None) or ""
                if not open_id:
                    continue
                _forward_to_sidecar("member-removed", {
                    "event_id": event_id,
                    "chat_id": chat_id,
                    "open_id": open_id,
                })

        def on_bot_added(data) -> None:
            event = data.event
            if event is None:
                return
            event_id = (data.header.event_id if data.header else None) or ""
            chat_id = event.chat_id or ""
            _forward_to_sidecar("bot-added", {
                "event_id": event_id,
                "chat_id": chat_id,
            })

        def on_group_disbanded(data) -> None:
            event = data.event
            if event is None:
                return
            event_id = (data.header.event_id if data.header else None) or ""
            chat_id = event.chat_id or ""
            _forward_to_sidecar("group-disbanded", {
                "event_id": event_id,
                "chat_id": chat_id,
            })

        # --- Start Feishu WebSocket in daemon thread ---
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_im_chat_member_user_added_v1(on_member_added)
            .register_p2_im_chat_member_user_deleted_v1(on_member_deleted)
            .register_p2_im_chat_member_bot_added_v1(on_bot_added)
            .register_p2_im_chat_disbanded_v1(on_group_disbanded)
            .build()
        )
        ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.ERROR,  # suppress "processor not found" warnings for unhandled event types
        )
        # Suppress noisy Lark logger for unhandled event types (reaction, message_read, etc.)
        logging.getLogger("Lark").setLevel(logging.CRITICAL)

        def ws_thread():
            # lark_oapi.ws.client captures asyncio.get_event_loop() at import
            # time into a module-level `loop` variable, then calls
            # loop.run_until_complete() in start(). If the import happened in
            # the main thread (which has a running loop), this fails with
            # "This event loop is already running".
            # Fix: patch the module-level loop to a fresh one for this thread.
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

        # --- Startup notification (daemon thread, 4s delay) ---
        def send_startup():
            time.sleep(4)
            try:
                import requests as _req
                resp = _req.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                token = resp.json().get("tenant_access_token", "")
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

                scope_resp = _req.get(
                    "https://open.feishu.cn/open-apis/contact/v3/scopes", headers=headers,
                )
                scope = scope_resp.json().get("data", {})
                user_ids = set(scope.get("user_ids", []))

                for dept_id in scope.get("department_ids", []):
                    page_token = ""
                    while True:
                        url = (f"https://open.feishu.cn/open-apis/contact/v3/users/find_by_department"
                               f"?department_id={dept_id}&page_size=50&user_id_type=open_id")
                        if page_token:
                            url += f"&page_token={page_token}"
                        dr = _req.get(url, headers=headers)
                        dd = dr.json().get("data", {})
                        for u in dd.get("items", []):
                            uid = u.get("open_id", "")
                            if uid:
                                user_ids.add(uid)
                        if not dd.get("has_more"):
                            break
                        page_token = dd.get("page_token", "")

                if not user_ids:
                    log.info("No users in scope — startup msg skipped")
                    return

                log.info("Sending startup to %d user(s)", len(user_ids))
                for uid in user_ids:
                    body = (
                        CreateMessageRequestBody.builder()
                        .receive_id(uid).msg_type("text")
                        .content(json.dumps({"text": "OpenClaw 已上线 ✅\n发送任意消息开始使用"}))
                        .build()
                    )
                    req = CreateMessageRequest.builder().receive_id_type("open_id").request_body(body).build()
                    resp_msg = self._feishu_client.im.v1.message.create(req)
                    if resp_msg.success():
                        self._recent_sent.add(resp_msg.data.message_id)
                        log.info("Startup msg sent to %s", uid[:20])
                    else:
                        log.debug("Startup msg skipped %s: %s", uid[:20], resp_msg.code)
            except Exception as e:
                log.error("Startup msg error: %s", e)

        threading.Thread(target=send_startup, daemon=True).start()

        # --- Consumer loop: read from queue, route ---
        try:
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Internal admin notification pseudo-message
                if "_admin_notify" in msg:
                    await self._notify_admin(msg["_admin_notify"])
                    continue

                chat_id = msg.get("chat_id", "")

                # Admin group: intercept slash commands, pass through normal messages
                if self.admin_chat_id and chat_id == self.admin_chat_id:
                    text = msg.get("text", "").strip()
                    if text.startswith("/"):
                        await self._handle_admin_message(msg)
                        continue
                    # Non-command messages in admin group → route normally
                    # so Claude Code can assist the admin

                # Normal routing
                await self.route_message(chat_id, msg)
        except asyncio.CancelledError:
            log.info("Feishu consumer loop cancelled")

    # ------------------------------------------------------------------
    # Client handler
    # ------------------------------------------------------------------

    async def _handle_client(self, ws: ServerConnection) -> None:
        """Handle a single WebSocket client (channel.py)."""
        log.info("Client connected from %s", ws.remote_address)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(ws, {"type": "error", "code": "INVALID_JSON", "message": "Could not parse message"})
                    continue

                msg_type = msg.get("type")
                if msg_type == "register":
                    await self._handle_register(ws, msg)
                elif msg_type == "reply":
                    await self._handle_reply(ws, msg)
                elif msg_type == "react":
                    await self._handle_react(ws, msg)
                elif msg_type == "send_file":
                    await self._handle_send_file(ws, msg)
                elif msg_type == "message":
                    await self._handle_inbound_message(ws, msg)
                elif msg_type == "pong":
                    pass  # heartbeat response, no-op
                else:
                    log.warning("Unknown message type: %s", msg_type)
        except websockets.ConnectionClosed:
            log.info("Client disconnected")
        finally:
            self._unregister(ws)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def _handle_register(self, ws: ServerConnection, msg: dict) -> None:
        chat_ids: list[str] = msg.get("chat_ids", [])
        instance_id: str = msg.get("instance_id", "unknown")
        role: str = msg.get("role", "agent")
        runtime_mode: str = msg.get("runtime_mode", "discussion")

        # Check for conflicts on exact chat_ids
        for cid in chat_ids:
            if cid == "*":
                continue
            if cid in self.exact_routes:
                existing = self.exact_routes[cid]
                await self._send(ws, {
                    "type": "error",
                    "code": "REGISTRATION_CONFLICT",
                    "message": f"chat_id {cid} already registered by instance {existing.instance_id}",
                })
                return

        inst = Instance(
            ws=ws,
            instance_id=instance_id,
            role=role,
            chat_ids=chat_ids,
            runtime_mode=runtime_mode,
        )
        self._ws_to_instance[ws] = inst

        for cid in chat_ids:
            if cid == "*":
                self.wildcard_instances.append(inst)
            else:
                self.exact_routes[cid] = inst

        await self._send(ws, {"type": "registered", "chat_ids": chat_ids})
        log.info("Registered instance %s role=%s chat_ids=%s", instance_id, role, chat_ids)
        await self._notify_admin(f"Instance connected: {instance_id} chat_ids={chat_ids}")

    def _unregister(self, ws: ServerConnection) -> None:
        inst = self._ws_to_instance.pop(ws, None)
        if inst is None:
            return

        for cid in inst.chat_ids:
            if cid == "*":
                try:
                    self.wildcard_instances.remove(inst)
                except ValueError:
                    pass
            else:
                self.exact_routes.pop(cid, None)

        log.info("Unregistered instance %s", inst.instance_id)
        # Fire-and-forget admin notification -- can't await in sync context
        # The caller should handle this if needed; we log instead.

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def route_message(self, chat_id: str, message: dict) -> None:
        """Route a message to the appropriate instance(s)."""
        routed_instance: Instance | None = None

        # 1. Exact match
        if chat_id in self.exact_routes:
            routed_instance = self.exact_routes[chat_id]
            await self._send(routed_instance.ws, message)

        # 2. Wildcard -- always receives a copy.
        #    When no dedicated instance exists, elect the first wildcard as
        #    handler so exactly ONE instance processes the message; the rest
        #    receive it with a routed_to hint (observation mode).
        elected: Instance | None = None
        if routed_instance is None and self.wildcard_instances:
            elected = self.wildcard_instances[0]

        for inst in self.wildcard_instances:
            # Skip if this wildcard instance is also the exact match
            if routed_instance is not None and inst.ws is routed_instance.ws:
                continue
            # Determine whether this wildcard should observe only
            if routed_instance is not None:
                # Dedicated instance owns the message — wildcard observes
                wc_msg = {**message, "routed_to": routed_instance.instance_id}
            elif inst is elected:
                # No dedicated route — this wildcard is elected as handler
                wc_msg = message
            else:
                # No dedicated route — other wildcards observe
                wc_msg = {**message, "routed_to": elected.instance_id}
            await self._send(inst.ws, wc_msg)

        # 3. Log actionable info when no dedicated instance exists
        if routed_instance is None and self.wildcard_instances:
            user = message.get("user", "unknown")
            source = message.get("source", "?")
            log.info(
                "💬 [%s] %s → wildcard (no dedicated instance)\n"
                "   To start dedicated instance:  ./cc-openclaw.sh %s",
                source, user, chat_id,
            )
        elif routed_instance is None and not self.wildcard_instances:
            log.warning("No route for chat_id=%s, message dropped", chat_id)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_reply(self, ws: ServerConnection, msg: dict) -> None:
        """Reverse-route a reply from an instance back to the originating channel."""
        chat_id = msg.get("chat_id", "")

        if chat_id.startswith("oc_"):
            text = msg.get("text", "")
            log.info("Reply to Feishu chat_id=%s text=%s", chat_id, text[:60])
            await self._reply_feishu(chat_id, text)
            # Remove ACK reaction now that we've replied
            last_msg = self._last_msg_id.get(chat_id, "")
            if last_msg and last_msg in self._ack_reactions:
                threading.Thread(target=self._remove_reaction, args=(last_msg,), daemon=True).start()
        else:
            log.warning("Reply for unknown channel prefix: chat_id=%s", chat_id)

    async def _handle_react(self, ws: ServerConnection, msg: dict) -> None:
        """Forward a reaction to Feishu API."""
        message_id = msg.get("message_id", "")
        emoji_type = msg.get("emoji_type", "THUMBSUP")
        log.info("React message_id=%s emoji=%s", message_id, emoji_type)
        if message_id and self._feishu_client:
            threading.Thread(
                target=self._send_reaction, args=(message_id, emoji_type), daemon=True
            ).start()

    async def _handle_send_file(self, ws: ServerConnection, msg: dict) -> None:
        """Upload a local file to Feishu and send it as a file message."""
        chat_id = msg.get("chat_id", "")
        file_path = msg.get("file_path", "")
        log.info("Send file chat_id=%s file_path=%s", chat_id, file_path)
        if chat_id and file_path and self._feishu_client:
            threading.Thread(
                target=self._send_file, args=(chat_id, file_path), daemon=True
            ).start()
            await self._send(ws, {"type": "send_file_ack", "ok": True})
        else:
            await self._send(ws, {
                "type": "send_file_ack", "ok": False,
                "error": "Missing chat_id, file_path, or no Feishu client",
            })

    def _send_file(self, chat_id: str, file_path: str) -> None:
        """Upload file to Feishu and send as file message. Blocking, meant for daemon thread."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        try:
            if not os.path.isfile(file_path):
                log.warning("_send_file: file not found: %s", file_path)
                return

            file_name = os.path.basename(file_path)

            # Step 1: Upload file to Feishu via SDK
            with open(file_path, "rb") as f:
                upload_body = (
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(file_name)
                    .file(f)
                    .build()
                )
                upload_req = CreateFileRequest.builder().request_body(upload_body).build()
                upload_resp = self._feishu_client.im.v1.file.create(upload_req)

            if not upload_resp.success():
                log.warning("_send_file upload failed: code=%s msg=%s", upload_resp.code, upload_resp.msg)
                return

            file_key = upload_resp.data.file_key if upload_resp.data else ""
            if not file_key:
                log.warning("_send_file: no file_key in upload response")
                return

            log.info("_send_file: uploaded %s -> file_key=%s", file_name, file_key)

            # Step 2: Send file message to chat
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}))
                .build()
            )
            send_req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            send_resp = self._feishu_client.im.v1.message.create(send_req)

            if send_resp.success():
                log.info("_send_file: sent %s to %s", file_name, chat_id)
                if send_resp.data and send_resp.data.message_id:
                    self._recent_sent.add(send_resp.data.message_id)
                    self._msg_counter["sent"] += 1
            else:
                log.warning("_send_file send failed: code=%s msg=%s", send_resp.code, send_resp.msg)

        except Exception as e:
            log.warning("_send_file error: %s", e)

    async def _handle_inbound_message(self, ws: ServerConnection, msg: dict) -> None:
        """Handle an inbound message from another source."""
        chat_id = msg.get("chat_id", "")
        if not chat_id:
            await self._send(ws, {"type": "error", "code": "MISSING_CHAT_ID", "message": "message requires chat_id"})
            return
        # Track chats
        if chat_id not in self._known_chats:
            user = msg.get("user", "anonymous")
            source = msg.get("source", "unknown")
            self._known_chats[chat_id] = {"user": user, "source": source}
        # Route to registered instances
        await self.route_message(chat_id, msg)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _chat_index(self) -> list[tuple[int, str, dict]]:
        """Return numbered list of (index, chat_id, info) for active chats."""
        return [(i + 1, cid, info) for i, (cid, info) in enumerate(self._known_chats.items())]

    def _resolve_chat_target(self, target: str) -> str | None:
        """Resolve a /inject target — accepts #N (index) or full chat_id."""
        if target.startswith("#"):
            try:
                idx = int(target[1:])
                index = self._chat_index()
                for num, cid, _ in index:
                    if num == idx:
                        return cid
            except (ValueError, IndexError):
                pass
            return None
        return target  # full chat_id

    def status_text(self) -> str:
        """Generate human-readable status for /status command."""
        lines = ["📊 Channel Server Status"]
        lines.append(f"消息统计: {self._msg_counter.get('received', 0)} 收 / {self._msg_counter.get('sent', 0)} 发")
        lines.append("")

        # Service desks (Claude Code instances)
        desks = list(self._ws_to_instance.values())
        lines.append(f"🖥️ 服务台 ({len(desks)}):")
        for inst in desks:
            uptime = datetime.now(timezone.utc) - inst.connected_at
            mins = int(uptime.total_seconds() // 60)
            if "*" in inst.chat_ids:
                scope = "全部对话"
            else:
                scope = ", ".join(inst.chat_ids)
            lines.append(f"  • {inst.instance_id} — {scope} ({mins}分钟)")
        if not desks:
            lines.append("  (无在线服务台)")
        lines.append("")

        # Channels (frontends)
        feishu_ok = self._feishu_client is not None
        lines.append("📡 渠道:")
        lines.append(f"  • 飞书 IM: {'✅ 在线' if feishu_ok else '❌ 离线'}")
        lines.append("")

        # Active conversations with numbers
        lines.append(f"💬 活跃对话 ({len(self._known_chats)}):")
        for num, cid, info in self._chat_index():
            user = info.get("user", "?")
            source = info.get("source", "?")
            source_icon = "🔵" if source == "feishu" else "⚪"
            # Routing info
            if cid in self.exact_routes:
                desk = self.exact_routes[cid].instance_id
            else:
                desk = "通配服务台"
            lines.append(f"  #{num} {source_icon} {user} → {desk}")
        if not self._known_chats:
            lines.append("  (暂无)")

        lines.append("")
        lines.append("💡 /inject #序号 <消息> 向对话发送管理指令")

        return "\n".join(lines)

    def help_text(self) -> str:
        """Generate help text for /help command."""
        return (
            "📖 Channel Server Commands\n"
            "\n"
            "/status — Instances, active chats (with #N numbers)\n"
            "/help — This message\n"
            "/inject #N <text> — Send to chat by number\n"
            "/inject <chat_id> <text> — Send to chat by full ID\n"
            "  Example: /inject #1 注意当前活动打八折\n"
            "/explain <场景描述> — Generate flow visualization\n"
            "  Example: /explain 用户问DID号码的价格\n"
            "\n"
            "Non-command messages → forwarded to Claude Code\n"
            "\n"
            "Start dedicated instance:\n"
            "  ./cc-openclaw.sh oc_<chat_id>"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send(ws: ServerConnection, msg: dict) -> None:
        try:
            data = json.dumps(msg, ensure_ascii=False)
            await ws.send(data)
            log.debug("Sent %d bytes to %s: type=%s chat_id=%s",
                       len(data), getattr(ws, 'id', '?'), msg.get('type'), msg.get('chat_id'))
        except websockets.ConnectionClosed:
            log.warning("Send failed -- connection already closed")
        except Exception as e:
            log.error("Send error: %s", e)

    async def _notify_admin(self, text: str) -> None:
        """Fire-and-forget admin notification. Degrades gracefully."""
        if not self.admin_chat_id:
            log.info("[admin] %s", text)
            return
        # Placeholder: would send via Feishu API
        log.info("[admin → %s] %s", self.admin_chat_id, text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _async_main() -> None:
    log_file = PROJECT_ROOT / ".openclaw" / "logs" / "channel-server.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), mode="a", encoding="utf-8"),
            logging.StreamHandler(),  # also print to terminal
        ],
    )
    # Keep terminal output at INFO, file at DEBUG
    logging.getLogger().handlers[0].setLevel(logging.DEBUG)   # file
    logging.getLogger().handlers[1].setLevel(logging.INFO)    # terminal

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    feishu_enabled = os.environ.get("FEISHU_ENABLED", "true").lower() in ("true", "1", "yes")

    server = ChannelServer(
        port=0,
        feishu_enabled=feishu_enabled,
        admin_chat_id=admin_chat_id,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    await server.start()

    sep = "=" * 60
    print(f"\n{sep}")
    print("  OpenClaw Channel Server")
    print(f"  Listening  : ws://localhost:{server.port}")
    print(f"  Feishu     : {'enabled' if feishu_enabled else 'disabled'}")
    if admin_chat_id:
        print(f"  Admin group: {admin_chat_id}")
    print(f"  Pidfile    : {server.pidfile}")
    print()
    print("  Next steps:")
    print(f"    1. Start Claude Code:  ./cc-openclaw.sh")
    print(f"    2. Start Claude Code:  ./cc-openclaw.sh oc_<chat_id>")
    print(sep + "\n")

    await server._stop_event.wait()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
