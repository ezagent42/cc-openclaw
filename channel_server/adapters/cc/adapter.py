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
        """Route incoming WS messages by type."""
        msg_type = msg.get("type", "")

        if msg_type == "register":
            await self._handle_register(ws, msg)
        elif msg_type in ("reply", "forward", "send_summary", "update_title",
                          "send_file", "react", "tool_notify"):
            self._route_to_actor(ws, msg)
        elif msg_type == "spawn_session":
            await self._handle_spawn(ws, msg)
        elif msg_type == "kill_session":
            await self._handle_kill(ws, msg)
        elif msg_type == "list_sessions":
            await self._handle_list(ws, msg)
        elif msg_type == "pong":
            pass  # ignore keepalive pongs
        else:
            log.debug("Unknown WS message type: %s", msg_type)

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
            await ws.send(json.dumps({"type": "error", "message": "Missing instance_id"}))
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

        await ws.send(json.dumps({"type": "registered", "address": address}))

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

    def push_to_cc(self, actor: Actor, payload: dict) -> None:
        """Transport callback: push message to CC session via WebSocket."""
        ws = self._address_to_ws.get(actor.address)
        if ws is None:
            log.warning("push_to_cc: no WebSocket for actor %s", actor.address)
            return
        try:
            data = json.dumps(payload)
            asyncio.ensure_future(ws.send(data))
        except Exception as e:
            log.warning("push_to_cc error for %s: %s", actor.address, e)

    # ------------------------------------------------------------------
    # Route inbound CC commands to actor
    # ------------------------------------------------------------------

    def _route_to_actor(self, ws, msg: dict) -> None:
        """Convert a WS message to an actor Message and send to the CC actor."""
        address = self._ws_to_address.get(id(ws))
        if not address:
            log.warning("_route_to_actor: unregistered WebSocket")
            return

        msg_type = msg.get("type", "")
        payload = dict(msg)
        payload["command"] = msg_type  # CCSessionHandler dispatches on 'command'

        actor_msg = Message(
            sender=address,
            type=msg_type,
            payload=payload,
        )
        self.runtime.send(address, actor_msg)

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
            await ws.send(json.dumps({"type": "error", "message": "Not registered"}))
            return

        session_name = msg.get("session_name", "")
        tag = msg.get("tag", "") or session_name

        if not session_name:
            await ws.send(json.dumps({
                "type": "spawn_result", "ok": False,
                "text": "Missing session_name",
            }))
            return

        # Extract user from address cc:user.root -> user
        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"

        # Only root can spawn
        if len(parts) < 2 or parts[1] != "root":
            await ws.send(json.dumps({
                "type": "spawn_result", "ok": False,
                "text": "Only root session can spawn children",
            }))
            return

        child_cc_addr = f"cc:{user}.{session_name}"
        if self.runtime.lookup(child_cc_addr) is not None and \
                self.runtime.lookup(child_cc_addr).state != "ended":
            await ws.send(json.dumps({
                "type": "spawn_result", "ok": False,
                "text": f"Session '{session_name}' already exists",
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
                "type": "spawn_result", "ok": False,
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
            anchor_msg_id = self.feishu_adapter.create_thread_anchor(chat_id, tag)
            if anchor_msg_id:
                self.feishu_adapter.pin_message(anchor_msg_id)

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
            tool_card_msg_id = self.feishu_adapter.create_tool_card(chat_id, f"[{tag}] starting...")
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
        self.spawn_cc_process(user, session_name, tag=tag)

        await ws.send(json.dumps({
            "type": "spawn_result", "ok": True,
            "text": f"Session '{session_name}' spawned",
            "session_name": session_name,
            "address": child_cc_addr,
        }))

    async def _handle_kill(self, ws, msg: dict) -> None:
        """Kill a child session.

        1. Update anchor card to red 'ended'
        2. Stop CC actor, tool card actor, feishu thread actor
        3. Kill tmux window
        4. Send kill_result ack
        """
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"type": "error", "message": "Not registered"}))
            return

        session_name = msg.get("session_name", "") or msg.get("name", "")
        if not session_name:
            await ws.send(json.dumps({
                "type": "kill_result", "ok": False,
                "text": "Missing session_name",
            }))
            return

        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"
        child_cc_addr = f"cc:{user}.{session_name}"

        child = self.runtime.lookup(child_cc_addr)
        if child is None or child.state == "ended":
            await ws.send(json.dumps({
                "type": "kill_result", "ok": False,
                "text": f"Session '{session_name}' not found or already ended",
            }))
            return

        # Update anchor card to red
        anchor_msg_id = child.metadata.get("anchor_msg_id", "")
        if anchor_msg_id and self.feishu_adapter:
            self.feishu_adapter._update_anchor_card(
                anchor_msg_id,
                f"\U0001f534 [{child.tag}] ended",
                body_text=f"Session [{child.tag}] has been terminated",
                template="red",
            )

        # Stop feishu thread actor
        chat_id = child.metadata.get("chat_id", "")
        if anchor_msg_id and chat_id:
            feishu_thread_addr = f"feishu:{chat_id}:{anchor_msg_id}"
            self.runtime.stop(feishu_thread_addr)

        # Stop tool card actor
        tool_card_addr = f"tool_card:{user}.{session_name}"
        if self.runtime.lookup(tool_card_addr):
            self.runtime.stop(tool_card_addr)

        # Stop CC actor
        self.runtime.stop(child_cc_addr)

        # Kill tmux window
        self.kill_cc_process(user, session_name)

        await ws.send(json.dumps({
            "type": "kill_result", "ok": True,
            "text": f"Session '{session_name}' killed",
            "session_name": session_name,
        }))

    async def _handle_list(self, ws, msg: dict) -> None:
        """List active sessions for the requesting user."""
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"type": "error", "message": "Not registered"}))
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
            "type": "sessions_list",
            "sessions": sessions,
            "text": text.strip(),
        }))

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def spawn_cc_process(self, user: str, session_name: str, tag: str = "") -> None:
        """Start CC process via cc-openclaw.sh in tmux."""
        script = PROJECT_ROOT / "cc-openclaw.sh"
        if not script.exists():
            log.warning("cc-openclaw.sh not found at %s", script)
            return

        window_name = f"{user}.{session_name}"
        tmux_session = os.environ.get("TMUX_SESSION", "openclaw")

        cmd = [
            "tmux", "new-window", "-t", tmux_session,
            "-n", window_name,
            str(script),
        ]
        env_vars = {
            "OC_USER": user,
            "OC_SESSION": session_name,
            "OC_TAG": tag or session_name,
        }
        # Build the full command with env vars
        env_prefix = " ".join(f"{k}={v}" for k, v in env_vars.items())
        cmd = [
            "tmux", "new-window", "-t", tmux_session,
            "-n", window_name,
            f"{env_prefix} {script}",
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
            log.info("Spawned CC process: %s in tmux window %s", session_name, window_name)
        except Exception as e:
            log.warning("Failed to spawn CC process %s: %s", session_name, e)

    def kill_cc_process(self, user: str, session_name: str) -> None:
        """Kill tmux window by looking up window index (dots in name break tmux parsing)."""
        window_name = f"{user}.{session_name}"
        tmux_session = os.environ.get("TMUX_SESSION", "openclaw")

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
