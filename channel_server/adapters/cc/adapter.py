"""CC adapter — bridges Claude Code sessions (via WebSocket) to the actor runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import websockets

from channel_server.core.actor import Actor, Message, Transport
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Maximum child sessions per user
_MAX_CHILDREN = 10


def _read_tmux_session_name() -> str:
    """Read SESSION_NAME from cc-openclaw.sh (single source of truth)."""
    script = PROJECT_ROOT / "cc-openclaw.sh"
    if script.exists():
        for line in script.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("SESSION_NAME="):
                # SESSION_NAME="cc-openclaw" → cc-openclaw
                val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return "cc-openclaw"


_TMUX_SESSION = os.environ.get("TMUX_SESSION") or _read_tmux_session_name()


class CCAdapter:
    """Bridge between Claude Code WebSocket sessions and the actor runtime.

    Inbound: parses WebSocket messages from CC sessions, routes to actors.
    Outbound: pushes actor messages to CC sessions over WebSocket.
    """

    def __init__(self, runtime: ActorRuntime, host: str = "127.0.0.1", port: int = 0) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self._server: websockets.WebSocketServer | None = None
        self._ws_to_address: dict[int, str] = {}  # id(ws) -> actor address
        self._address_to_ws: dict[str, object] = {}  # actor address -> ws
        self.feishu_adapter = None  # Set by app.py after both adapters created

        runtime.register_transport_handler("websocket", self.push_to_cc)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> int:
        """Start WebSocket server, return actual port."""
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port,
        )
        # Get the actual port (useful when port=0 for auto-assign)
        actual_port = self._server.sockets[0].getsockname()[1]
        self.port = actual_port
        log.info("CC WebSocket server started on %s:%d", self.host, actual_port)
        return actual_port

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            log.info("CC WebSocket server stopped")

    # ------------------------------------------------------------------
    # Client handling
    # ------------------------------------------------------------------

    async def _handle_client(self, ws) -> None:
        """Handle a WebSocket client connection. On disconnect, call handle_disconnect."""
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self.handle_message(ws, msg)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from WebSocket client")
                except Exception as e:
                    log.error("Error handling WS message: %s", e)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.handle_disconnect(ws)

    async def handle_message(self, ws, msg: dict) -> None:
        """Route incoming WS messages by action.

        WS messages use `action` for all routing. The action field serves
        double duty: control actions (register, spawn_session, etc.) are
        handled by the adapter; actor payload actions (reply, react, etc.)
        are forwarded to the actor's mailbox as-is.
        """
        action = msg.get("action", "")

        # -- Control actions (adapter-level, not forwarded to actors) --
        if action == "register":
            await self._handle_register(ws, msg)
        elif action == "spawn_session":
            await self._handle_spawn(ws, msg)
        elif action == "kill_session":
            await self._handle_kill(ws, msg)
        elif action == "list_sessions":
            await self._handle_list(ws, msg)
        elif action == "pong":
            pass  # ignore keepalive pongs
        elif action == "tool_notify":
            # tool_notify can come from anonymous hook connections (no register).
            if self._ws_to_address.get(id(ws)):
                self._route_to_actor(ws, msg)
            else:
                self._route_anonymous_tool_notify(msg)
        else:
            # -- Actor payload actions (forwarded to actor mailbox) --
            # No action = default reply; named actions (react, send_file, etc.)
            # are passed through as payload["action"].
            self._route_to_actor(ws, msg)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def _handle_register(self, ws, msg: dict) -> None:
        """CC session registers:
        1. Map instance_id to actor address (prefix cc: if needed)
        2. Look up or auto-spawn CC actor
        3. Attach WebSocket transport
        4. Send registered ack
        """
        instance_id = msg.get("instance_id", "")
        if not instance_id:
            await ws.send(json.dumps({"action": "error", "message": "Missing instance_id"}))
            return

        # Actor address for CC sessions
        address = instance_id if instance_id.startswith("cc:") else f"cc:{instance_id}"

        # Track mapping
        self._ws_to_address[id(ws)] = address
        self._address_to_ws[address] = ws

        # Look up or auto-spawn
        actor = self.runtime.lookup(address)
        if actor is None or actor.state == "ended":
            tag = msg.get("tag_name", "") or instance_id.split(".")[-1]
            self.runtime.spawn(
                address,
                "cc_session",
                tag=tag,
                state="active",
                transport=Transport(type="websocket", config={"instance_id": instance_id}),
            )
            log.info("Auto-spawned CC actor: %s", address)
        else:
            # Attach transport (resumes if suspended)
            self.runtime.attach(
                address,
                Transport(type="websocket", config={"instance_id": instance_id}),
            )
            log.info("Attached transport to existing CC actor: %s", address)

        # Wire topology for root sessions:
        # system:admin → cc:user.root → feishu:chat_id
        chat_ids = msg.get("chat_ids", [])
        session = instance_id.split(".")[-1] if "." in instance_id else ""
        if session == "root" and chat_ids:
            chat_id = next((c for c in chat_ids if c != "*"), None)
            if chat_id:
                feishu_addr = f"feishu:{chat_id}"
                self.runtime.wire(address, feishu_addr)
                log.info("Wired %s → %s", address, feishu_addr)
                self.runtime.wire("system:admin", address)
                log.info("Wired system:admin → %s", address)

        await ws.send(json.dumps({"action": "registered", "address": address}))

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def handle_disconnect(self, ws) -> None:
        """Detach transport from CC actor on WS disconnect."""
        address = self._ws_to_address.pop(id(ws), None)
        if address:
            self._address_to_ws.pop(address, None)
            actor = self.runtime.lookup(address)
            if actor is not None and actor.state != "ended":
                self.runtime.detach(address)
                log.info("Detached transport on disconnect: %s", address)

    # ------------------------------------------------------------------
    # Outbound: push to CC
    # ------------------------------------------------------------------

    async def push_to_cc(self, actor: Actor, payload: dict) -> None:
        """Transport callback: push message to CC session via WebSocket."""
        ws = self._address_to_ws.get(actor.address)
        if ws is None:
            log.warning("push_to_cc: no WebSocket for actor %s", actor.address)
            return
        try:
            data = json.dumps(payload)
            await ws.send(data)
        except Exception as e:
            log.warning("push_to_cc error for %s: %s", actor.address, e)

    # ------------------------------------------------------------------
    # Route inbound CC commands to actor
    # ------------------------------------------------------------------

    def _route_to_actor(self, ws, msg: dict) -> None:
        """Forward a WS message to the CC actor's mailbox as-is.

        The WS payload is the actor payload — no translation needed.
        `action` in the payload matches the actor model convention directly.
        """
        address = self._ws_to_address.get(id(ws))
        if not address:
            log.warning("_route_to_actor: unregistered WebSocket")
            return

        action = msg.get("action", "")
        log.info("CC message from %s: action=%s text=%s", address, action or "(reply)", str(msg.get("text", ""))[:60])

        actor_msg = Message(sender=address, payload=msg)
        self.runtime.send(address, actor_msg)

    def _route_anonymous_tool_notify(self, msg: dict) -> None:
        """Route a tool_notify from an anonymous WS connection (e.g. hook).

        Looks up the tool_card actor by matching chat_id against registered
        CC actors' metadata, then delivers the message directly.
        """
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        if not chat_id or not text:
            log.warning("_route_anonymous_tool_notify: missing chat_id or text")
            return

        # Find tool_card actor whose parent CC actor serves this chat_id
        for addr, actor in self.runtime.actors.items():
            if not addr.startswith("tool_card:") or actor.state == "ended":
                continue
            if actor.transport and actor.transport.config.get("chat_id") == chat_id:
                actor_msg = Message(
                    sender="hook:tool_notify",
                    payload={"action": "tool_notify", "text": text},
                )
                self.runtime.send(addr, actor_msg)
                return

        log.debug("_route_anonymous_tool_notify: no tool_card actor for chat_id=%s", chat_id)

    # ------------------------------------------------------------------
    # Session management — spawn / kill / list
    # ------------------------------------------------------------------

    async def _handle_spawn(self, ws, msg: dict) -> None:
        """Spawn a child session.

        1. Validate (sender is user.root, session doesn't exist, under limit)
        2. Create feishu thread anchor via feishu_adapter
        3. Spawn feishu thread actor
        4. Spawn CC actor (suspended, waiting for transport)
        5. Spawn tool card actor
        6. Start CC process via tmux
        7. Send spawn_result ack
        """
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"action": "error", "message": "Not registered"}))
            return

        session_name = msg.get("session_name", "")
        tag = msg.get("tag", "") or session_name

        if not session_name:
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": False,
                "text": "Missing session_name",
            }))
            return

        # Extract user from address cc:user.root -> user
        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"

        # Only root can spawn
        if len(parts) < 2 or parts[1] != "root":
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": False,
                "text": "Only root session can spawn children",
            }))
            return

        child_cc_addr = f"cc:{user}.{session_name}"
        existing = self.runtime.lookup(child_cc_addr)
        if existing is not None and existing.state == "active":
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": False,
                "text": f"Session '{session_name}' is already active",
            }))
            return

        # Resume suspended actor — just start tmux, CC will reconnect
        if existing is not None and existing.state == "suspended":
            if self.spawn_cc_process(user, session_name, tag=tag):
                await ws.send(json.dumps({
                    "action": "spawn_result", "ok": True,
                    "text": f"Session '{session_name}' resumed",
                    "session_name": session_name,
                    "address": child_cc_addr,
                }))
            else:
                await ws.send(json.dumps({
                    "action": "spawn_result", "ok": False,
                    "text": f"Session '{session_name}' resume failed: could not create tmux window",
                }))
            return

        # Count active children
        prefix = f"cc:{user}."
        active = sum(
            1 for a in self.runtime.actors.values()
            if a.address.startswith(prefix)
            and a.address != address
            and a.state != "ended"
        )
        if active >= _MAX_CHILDREN:
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": False,
                "text": f"Max sessions ({_MAX_CHILDREN}) reached",
            }))
            return

        # Create feishu thread anchor if feishu_adapter is available
        anchor_msg_id = None
        chat_id = ""
        root_actor = self.runtime.lookup(address)
        if root_actor and root_actor.downstream:
            # Find the feishu chat actor to get chat_id
            for ds_addr in root_actor.downstream:
                ds_actor = self.runtime.lookup(ds_addr)
                if ds_actor and ds_actor.transport and ds_actor.transport.type == "feishu_chat":
                    chat_id = ds_actor.transport.config.get("chat_id", "")
                    break

        if self.feishu_adapter and chat_id:
            anchor_msg_id = await self.feishu_adapter.create_thread_anchor(chat_id, tag)
            if anchor_msg_id:
                await self.feishu_adapter.pin_message(anchor_msg_id)

        # Spawn feishu thread actor (if we have anchor)
        feishu_thread_addr = ""
        if anchor_msg_id and chat_id:
            feishu_thread_addr = f"feishu:{chat_id}:{anchor_msg_id}"
            self.runtime.spawn(
                feishu_thread_addr,
                "feishu_inbound",
                tag=tag,
                transport=Transport(
                    type="feishu_thread",
                    config={"chat_id": chat_id, "root_id": anchor_msg_id},
                ),
            )

        # Spawn CC actor (suspended — waiting for WS transport)
        downstream = [feishu_thread_addr] if feishu_thread_addr else []
        child_actor = self.runtime.spawn(
            child_cc_addr,
            "cc_session",
            tag=tag,
            state="suspended",
            parent=address,
            downstream=downstream,
            metadata={"anchor_msg_id": anchor_msg_id or "", "chat_id": chat_id},
        )

        # Spawn tool card actor
        if self.feishu_adapter and chat_id:
            tool_card_msg_id = await self.feishu_adapter.create_tool_card(chat_id, f"[{tag}] starting...")
            if tool_card_msg_id:
                tool_card_addr = f"tool_card:{user}.{session_name}"
                self.runtime.spawn(
                    tool_card_addr,
                    "tool_card",
                    tag=tag,
                    transport=Transport(
                        type="feishu_chat",
                        config={"chat_id": chat_id},
                    ),
                    metadata={"card_msg_id": tool_card_msg_id},
                )

        # Wire feishu thread -> CC actor downstream
        if feishu_thread_addr:
            thread_actor = self.runtime.lookup(feishu_thread_addr)
            if thread_actor:
                thread_actor.downstream.append(child_cc_addr)

        # Start CC process via tmux
        if self.spawn_cc_process(user, session_name, tag=tag):
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": True,
                "text": f"Session '{session_name}' spawned",
                "session_name": session_name,
                "address": child_cc_addr,
            }))
        else:
            # Rollback: clean up all actors created during this spawn
            log.error("Spawn failed for %s — rolling back actors", child_cc_addr)
            tool_card_addr = f"tool_card:{user}.{session_name}"
            for addr in [child_cc_addr, tool_card_addr, feishu_thread_addr]:
                if addr:
                    await self.runtime.stop(addr)
            await ws.send(json.dumps({
                "action": "spawn_result", "ok": False,
                "text": f"Session '{session_name}' failed: could not create tmux window (session={_TMUX_SESSION})",
            }))

    async def _handle_kill(self, ws, msg: dict) -> None:
        """Kill a child session.

        Stops the CC actor — its on_stop lifecycle cascades to stop child
        actors (tool_card, feishu_thread), which in turn run their own
        on_stop (unpin, update anchor card, etc.).
        """
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"action": "error", "message": "Not registered"}))
            return

        session_name = msg.get("session_name", "") or msg.get("name", "")
        if not session_name:
            await ws.send(json.dumps({
                "action": "kill_result", "ok": False,
                "text": "Missing session_name",
            }))
            return

        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"
        child_cc_addr = f"cc:{user}.{session_name}"

        child = self.runtime.lookup(child_cc_addr)
        if child is None or child.state == "ended":
            await ws.send(json.dumps({
                "action": "kill_result", "ok": False,
                "text": f"Session '{session_name}' not found or already ended",
            }))
            return

        # Stop CC actor — lifecycle callbacks handle all cleanup
        await self.runtime.stop(child_cc_addr)

        # Kill tmux window
        self.kill_cc_process(user, session_name)

        await ws.send(json.dumps({
            "action": "kill_result", "ok": True,
            "text": f"Session '{session_name}' killed",
            "session_name": session_name,
        }))

    async def _handle_list(self, ws, msg: dict) -> None:
        """List active sessions for the requesting user."""
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"action": "error", "message": "Not registered"}))
            return

        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"
        prefix = f"cc:{user}."

        sessions = []
        for addr, actor in self.runtime.actors.items():
            if addr.startswith(prefix) and actor.state != "ended":
                session_part = addr[len(prefix):]
                sessions.append({
                    "name": session_part,
                    "address": addr,
                    "state": actor.state,
                    "tag": actor.tag,
                })

        text = f"Active sessions for {user}: {len(sessions)}\n"
        for s in sessions:
            text += f"  - {s['name']} [{s['state']}] tag={s['tag']}\n"

        await ws.send(json.dumps({
            "action": "sessions_list",
            "sessions": sessions,
            "text": text.strip(),
        }))

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def spawn_cc_process(self, user: str, session_name: str, tag: str = "") -> bool:
        """Start CC process via cc-openclaw.sh in tmux. Returns True on success."""
        script = PROJECT_ROOT / "cc-openclaw.sh"
        if not script.exists():
            log.warning("cc-openclaw.sh not found at %s", script)
            return False

        window_name = f"{user}.{session_name}"

        env_vars = {
            "OC_USER": user,
            "OC_SESSION": session_name,
            "OC_TAG": tag or session_name,
        }
        # Build the full command with env vars
        env_prefix = " ".join(f"{k}={v}" for k, v in env_vars.items())
        cmd = [
            "tmux", "new-window", "-t", _TMUX_SESSION,
            "-n", window_name,
            f"{env_prefix} {script}",
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
            log.info("Spawned CC process: %s in tmux window %s", session_name, window_name)
            return True
        except Exception as e:
            log.error("Failed to spawn CC process %s: %s", session_name, e)
            return False

    def kill_cc_process(self, user: str, session_name: str) -> None:
        """Kill tmux window by looking up window index (dots in name break tmux parsing)."""
        window_name = f"{user}.{session_name}"
        tmux_session = _TMUX_SESSION

        try:
            # Use list-windows to find the window index by name
            # (dots in window names break direct tmux targeting)
            result = subprocess.run(
                ["tmux", "list-windows", "-t", tmux_session, "-F", "#{window_index} #{window_name}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == window_name:
                    idx = parts[0]
                    subprocess.run(
                        ["tmux", "kill-window", "-t", f"{tmux_session}:{idx}"],
                        capture_output=True, timeout=5,
                    )
                    log.info("Killed tmux window %s (index %s)", window_name, idx)
                    return
            log.warning("tmux window '%s' not found", window_name)
        except Exception as e:
            log.warning("Failed to kill CC process %s: %s", session_name, e)
