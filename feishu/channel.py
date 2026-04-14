#!/usr/bin/env python3
"""
openclaw-channel: Claude Code Channel MCP Server
Connects to channel-server.py via WebSocket, bridges messages to Claude Code via MCP stdio.

Architecture:
- ChannelClient connects to channel-server via WebSocket (auto-reconnect)
- MCP low-level Server + stdio_server()
- consume_messages reads from ChannelClient queue, injects into MCP write_stream
- server.run, ChannelClient.connect, and consume_messages run in parallel via anyio task group
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import anyio
import websockets
import mcp.server.stdio
from mcp.server.lowlevel import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, Tool, TextContent

# -- Config ------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / ".openclaw" / "logs" / "channel.log"
INSTRUCTIONS_PATH = Path(__file__).parent / "channel-instructions.md"
IDENTITY_PATH = PROJECT_ROOT / ".openclaw" / "identity.yaml"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="[openclaw-channel] %(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
# File gets DEBUG, stderr gets INFO
logging.getLogger().handlers[0].setLevel(logging.DEBUG)
logging.getLogger().handlers[1].setLevel(logging.INFO)
log = logging.getLogger("openclaw-channel")
log.info(f"=== Channel started PID={os.getpid()} ===")


# -- Channel Server Client ---------------------------------------------------


class ChannelClient:
    """WebSocket client that connects to channel-server.py."""

    def __init__(self, server_url="ws://localhost:9999", chat_ids=None,
                 instance_id="", runtime_mode="discussion",
                 pidfile_path=None):
        self.server_url = server_url
        self._pidfile_path = pidfile_path
        self.chat_ids = chat_ids or ["*"]
        self.instance_id = instance_id or f"channel-{os.getpid()}"
        self.runtime_mode = runtime_mode
        self.ws = None
        self._message_queue: asyncio.Queue = asyncio.Queue()

    def _resolve_server_url(self) -> str:
        """Re-read the PID file to get the latest port (handles server restarts)."""
        if not self._pidfile_path:
            return self.server_url
        try:
            pidfile = Path(self._pidfile_path)
            if pidfile.exists():
                parts = pidfile.read_text().strip().split(":")
                port = int(parts[1])
                url = f"ws://localhost:{port}"
                if url != self.server_url:
                    log.info("channel-server port changed: %s → %s", self.server_url, url)
                    self.server_url = url
                return url
        except Exception as e:
            log.warning("Failed to read pidfile: %s", e)
        return self.server_url

    async def connect(self):
        """Connect to channel-server with auto-reconnect.

        On each reconnect attempt, re-reads the PID file to pick up
        a new port if channel-server was restarted.
        """
        while True:
            try:
                url = self._resolve_server_url()
                async with websockets.connect(url) as ws:
                    self.ws = ws
                    await self._register(ws)
                    await self._message_loop(ws)
            except Exception as e:
                log.warning(f"channel-server disconnected ({type(e).__name__}: {e}), retrying in 3s...")
                self.ws = None
                await asyncio.sleep(3)

    async def _register(self, ws):
        await ws.send(json.dumps({
            "type": "register",
            "role": "developer" if "*" in self.chat_ids else "production",
            "chat_ids": self.chat_ids,
            "instance_id": self.instance_id,
            "runtime_mode": self.runtime_mode,
        }))
        resp = json.loads(await ws.recv())
        if resp.get("type") == "error":
            log.error(f"Registration failed: {resp}")
            raise RuntimeError(resp.get("message", "Registration failed"))
        log.info(f"Registered with channel-server: chat_ids={self.chat_ids}")

    async def _message_loop(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "message":
                await self._message_queue.put(msg)
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif msg.get("type") == "error":
                log.error(f"Server error: {msg}")

    async def send_reply(self, chat_id, text):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "reply", "chat_id": chat_id, "text": text,
            }))

    async def send_react(self, message_id, emoji_type):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "react", "message_id": message_id, "emoji_type": emoji_type,
            }))

    async def send_file(self, chat_id, file_path):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "send_file", "chat_id": chat_id, "file_path": file_path,
            }))

    async def send_ux_event(self, chat_id, event, data=None):
        if self.ws:
            await self.ws.send(json.dumps({
                "type": "ux_event", "chat_id": chat_id, "event": event,
                "data": data or {},
            }))


# -- Module-level state for tool handlers ------------------------------------

_channel_client: ChannelClient | None = None
_event_loop: asyncio.AbstractEventLoop | None = None


# -- MCP Notification Injection -----------------------------------------------


async def inject_message(write_stream, msg: dict):
    """Send a channel notification to Claude Code via the MCP write stream."""
    # Build meta — omit None values to avoid potential issues with Claude Code
    meta = {
        "chat_id": msg["chat_id"],
        "message_id": msg.get("message_id", ""),
        "user": msg.get("user", "unknown"),
        "user_id": msg.get("user_id", ""),
        "runtime_mode": msg.get("runtime_mode", "discussion"),
        "source": msg.get("source", "feishu"),
        "ts": msg.get("ts", datetime.now(tz=timezone.utc).isoformat()),
    }
    if msg.get("routed_to"):
        meta["routed_to"] = msg["routed_to"]
    if msg.get("file_path"):
        meta["file_path"] = msg["file_path"]
    if msg.get("admin_chat_id"):
        meta["admin_chat_id"] = msg["admin_chat_id"]

    params = {"content": msg["text"], "meta": meta}

    log.debug(f"inject_message params: {json.dumps(params, ensure_ascii=False)[:500]}")

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params=params,
    )
    session_msg = SessionMessage(message=JSONRPCMessage(notification))
    log.debug(f"inject_message SessionMessage: {session_msg}")
    await write_stream.send(session_msg)
    log.info(f"Injected: '{msg['text'][:60]}...' from {msg.get('user', '?')}")


# -- MCP Server + Tools -------------------------------------------------------


_FALLBACK_INSTRUCTIONS = (
    "Messages from Feishu arrive as <channel> tags with chat_id, user, ts attributes. "
    "Reply with the reply tool -- your transcript never reaches the Feishu chat. "
    "When users send requests, use available plugin tools to process them, "
    "then reply with the result."
)
_instructions_mtime: float = 0.0
_identity_mtime: float = 0.0


def _load_identity() -> str:
    """Read identity.yaml and format as instructions preamble."""
    if not IDENTITY_PATH.exists():
        return ""
    try:
        import yaml
        data = yaml.safe_load(IDENTITY_PATH.read_text(encoding="utf-8"))
        lines = [f"## Identity\n"]
        lines.append(f"You are **{data.get('name', 'AI Bot')}** — {data.get('description', '')}.")
        modes = data.get("modes", {})
        for mode_key, mode_name in modes.items():
            lines.append(f"- In {mode_key} mode (`runtime_mode: {mode_key}`): introduce yourself as **{mode_name}**")
        for rule in data.get("rules", []):
            lines.append(f"- {rule}")
        return "\n".join(lines) + "\n\n"
    except Exception as e:
        log.warning("Failed to load identity.yaml: %s", e)
        return ""


def _build_instructions() -> str:
    """Combine identity + channel-instructions into full instructions text."""
    identity = _load_identity()
    if INSTRUCTIONS_PATH.exists():
        base = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    else:
        base = _FALLBACK_INSTRUCTIONS
    return identity + base


def _refresh_instructions(server: Server) -> None:
    """Reload instructions if channel-instructions.md or identity.yaml changed."""
    global _instructions_mtime, _identity_mtime
    if not INSTRUCTIONS_PATH.exists():
        return
    inst_mtime = INSTRUCTIONS_PATH.stat().st_mtime
    id_mtime = IDENTITY_PATH.stat().st_mtime if IDENTITY_PATH.exists() else 0.0
    if inst_mtime != _instructions_mtime or id_mtime != _identity_mtime:
        server.instructions = _build_instructions()
        _instructions_mtime = inst_mtime
        _identity_mtime = id_mtime
        log.info("Instructions reloaded (identity=%s)", "yes" if id_mtime else "no")


def create_server() -> Server:
    global _instructions_mtime, _identity_mtime
    text = _build_instructions()
    if INSTRUCTIONS_PATH.exists():
        _instructions_mtime = INSTRUCTIONS_PATH.stat().st_mtime
    if IDENTITY_PATH.exists():
        _identity_mtime = IDENTITY_PATH.stat().st_mtime
    return Server("openclaw-channel", instructions=text)


def register_tools(server: Server):

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name="reply",
                description=(
                    "Send a message to a Feishu chat. The user reads Feishu, not this "
                    "session -- anything you want them to see must go through this tool. "
                    "chat_id is from the inbound <channel> tag (oc_xxx format)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Feishu chat ID (oc_xxx)"},
                        "text": {"type": "string", "description": "Message text"},
                    },
                    "required": ["chat_id", "text"],
                },
            ),
            Tool(
                name="react",
                description="Add an emoji reaction to a Feishu message",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Message ID (om_xxx)"},
                        "emoji_type": {"type": "string", "description": "Feishu emoji (THUMBSUP, DONE, OK)"},
                    },
                    "required": ["message_id", "emoji_type"],
                },
            ),
            Tool(
                name="send_file",
                description=(
                    "Send a file to a Feishu chat. Uploads the local file to Feishu "
                    "and sends it as a file message. chat_id is from the inbound "
                    "<channel> tag (oc_xxx format)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string", "description": "Feishu chat ID (oc_xxx)"},
                        "file_path": {"type": "string", "description": "Absolute path to the local file to send"},
                    },
                    "required": ["chat_id", "file_path"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "reply":
            return _handle_reply(arguments)
        elif name == "react":
            return _handle_react(arguments)
        elif name == "send_file":
            return _handle_send_file(arguments)
        raise ValueError(f"Unknown tool: {name}")


def _handle_reply(args: dict) -> list[TextContent]:
    chat_id = args["chat_id"]
    text = args["text"]
    if _channel_client and _channel_client.ws and _event_loop:
        asyncio.run_coroutine_threadsafe(
            _channel_client.send_reply(chat_id, text), _event_loop,
        )
        log.info(f"Reply to {chat_id}: {text[:50]}...")
        return [TextContent(type="text", text=f"Sent to {chat_id}")]
    return [TextContent(type="text", text="Error: not connected to channel-server")]


def _handle_react(args: dict) -> list[TextContent]:
    if _channel_client and _channel_client.ws and _event_loop:
        asyncio.run_coroutine_threadsafe(
            _channel_client.send_react(args["message_id"], args["emoji_type"]),
            _event_loop,
        )
        return [TextContent(type="text", text=f"Reacted {args['emoji_type']}")]
    return [TextContent(type="text", text="Error: not connected to channel-server")]


def _handle_send_file(args: dict) -> list[TextContent]:
    chat_id = args["chat_id"]
    file_path = args["file_path"]
    if not os.path.isfile(file_path):
        return [TextContent(type="text", text=f"Error: file not found: {file_path}")]
    if _channel_client and _channel_client.ws and _event_loop:
        asyncio.run_coroutine_threadsafe(
            _channel_client.send_file(chat_id, file_path),
            _event_loop,
        )
        log.info(f"Send file to {chat_id}: {file_path}")
        return [TextContent(type="text", text=f"File sent to {chat_id}: {os.path.basename(file_path)}")]
    return [TextContent(type="text", text="Error: not connected to channel-server")]


# -- Main ---------------------------------------------------------------------


async def main():
    global _channel_client, _event_loop
    _event_loop = asyncio.get_running_loop()

    pidfile = PROJECT_ROOT / ".channel-server.pid"
    if not pidfile.exists():
        log.error("channel-server not running — .channel-server.pid not found")
        sys.exit(1)
    parts = pidfile.read_text().strip().split(":")
    pid, port = int(parts[0]), int(parts[1])
    try:
        os.kill(pid, 0)
    except OSError:
        log.error("channel-server PID %d not alive", pid)
        sys.exit(1)
    server_url = f"ws://localhost:{port}"

    chat_id_str = os.environ.get("OPENCLAW_CHAT_ID", "*")
    chat_ids = [chat_id_str]

    _channel_client = ChannelClient(
        server_url=server_url,
        chat_ids=chat_ids,
        runtime_mode=os.environ.get("OPENCLAW_RUNTIME_MODE", "discussion"),
        pidfile_path=str(pidfile),
    )

    server = create_server()
    register_tools(server)

    init_opts = InitializationOptions(
        server_name="openclaw-channel",
        server_version="1.0.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={"claude/channel": {}},
        ),
    )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        async def consume_messages():
            while True:
                msg = await _channel_client._message_queue.get()
                log.info(f"consume_messages: got msg type={msg.get('type')} chat_id={msg.get('chat_id')} source={msg.get('source')} text={msg.get('text','')[:40]}")
                try:
                    _refresh_instructions(server)
                    await inject_message(write_stream, msg)
                except Exception as e:
                    log.error(f"inject error: {e}")

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run, read_stream, write_stream, init_opts)
                tg.start_soon(_channel_client.connect)
                tg.start_soon(consume_messages)
        except Exception as e:
            log.error(f"Task group error: {e}")


def entry_point():
    asyncio.run(main())


if __name__ == "__main__":
    entry_point()
