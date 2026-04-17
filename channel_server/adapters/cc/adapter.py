"""CC adapter — bridges Claude Code sessions (via WebSocket) to the actor runtime."""
from __future__ import annotations

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
            await self._forward_session_cmd(ws, msg)
        elif action == "kill_session":
            await self._forward_session_cmd(ws, msg)
        elif action == "list_sessions":
            await self._forward_session_cmd(ws, msg)
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
        # system:admin → cc:user.root → feishu:{app_id}:{chat_id}
        chat_ids = msg.get("chat_ids", [])
        session = instance_id.split(".")[-1] if "." in instance_id else ""
        if session == "root" and chat_ids:
            chat_id = next((c for c in chat_ids if c != "*"), None)
            if chat_id:
                app_id = self.feishu_adapter.app_id if self.feishu_adapter else ""
                feishu_addr = f"feishu:{app_id}:{chat_id}"
                self.runtime.wire(address, feishu_addr)
                log.info("Wired %s → %s", address, feishu_addr)
                self.runtime.wire("system:admin", address)
                log.info("Wired system:admin → %s", address)

                # Notify session-mgr to initialize root session (tool card, etc.)
                user = instance_id.split(".")[0]
                from channel_server.core.actor import Message as ActorMessage
                self.runtime.send(
                    "system:session-mgr",
                    ActorMessage(
                        sender=address,
                        payload={
                            "user": user,
                            "session_name": "root",
                            "chat_id": chat_id,
                            "mode": "root",
                        },
                        metadata={"type": "init_session"},
                    ),
                )

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

    async def push_to_cc(self, actor: Actor, payload: dict) -> dict | None:
        """Transport callback: push message to CC session via WebSocket."""
        action = payload.get("action")

        if action == "spawn_tmux":
            user = payload.get("user", "")
            session_name = payload.get("session_name", "")
            tag = payload.get("tag", "")
            chat_id = payload.get("chat_id", "")
            success = self.spawn_cc_process(user, session_name, tag=tag, chat_id=chat_id)
            return {"tmux_started": success}

        if action == "kill_tmux":
            user = payload.get("user", "")
            session_name = payload.get("session_name", "")
            self.kill_cc_process(user, session_name)
            return {"tmux_killed": True}

        # Existing: send JSON over WebSocket
        ws = self._address_to_ws.get(actor.address)
        if ws:
            try:
                await ws.send(json.dumps(payload))
            except Exception as e:
                log.warning("push_to_cc error for %s: %s", actor.address, e)
        else:
            log.warning("push_to_cc: no WebSocket for actor %s", actor.address)
        return None

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

        Finds the cc:* actor serving this chat_id by checking downstream
        feishu actors' transport config, then delivers the message to that
        cc actor. CCSessionHandler routes it to feishu as plain text.
        """
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        if not chat_id or not text:
            log.warning("_route_anonymous_tool_notify: missing chat_id or text")
            return

        from channel_server.core.actor import Message as ActorMessage

        for addr, actor in self.runtime.actors.items():
            if not addr.startswith("cc:") or actor.state == "ended":
                continue
            for ds_addr in actor.downstream:
                ds = self.runtime.lookup(ds_addr)
                if (ds and ds.transport
                        and ds.transport.type in ("feishu_chat", "feishu_thread")
                        and ds.transport.config.get("chat_id") == chat_id):
                    self.runtime.send(addr, ActorMessage(
                        sender="hook:tool_notify",
                        payload={"action": "tool_notify", "text": text},
                    ))
                    return

        log.debug("_route_anonymous_tool_notify: no cc actor for chat_id=%s", chat_id)

    # ------------------------------------------------------------------
    # Session management — spawn / kill / list
    # ------------------------------------------------------------------

    async def _forward_session_cmd(self, ws, msg: dict) -> None:
        """Handle session commands (spawn/kill/list).

        For spawn: creates feishu anchor + actors + tmux process (I/O in adapter).
        For kill/list: forwards to session-mgr actor.
        """
        address = self._ws_to_address.get(id(ws))
        if not address:
            await ws.send(json.dumps({"action": "error", "message": "Not registered"}))
            return

        parts = address.replace("cc:", "").split(".")
        user = parts[0] if parts else "unknown"

        # Find chat_id from root actor's downstream feishu actor
        root_actor = self.runtime.lookup(address)
        chat_id = ""
        app_id = self.feishu_adapter.app_id if self.feishu_adapter else ""
        if root_actor:
            for ds_addr in root_actor.downstream:
                ds = self.runtime.lookup(ds_addr)
                if ds and ds.transport and ds.transport.type == "feishu_chat":
                    chat_id = ds.transport.config.get("chat_id", "")
                    break

        action = msg.get("action", "")
        session_name = msg.get("session_name", "")
        tag = msg.get("tag", "") or session_name

        if action == "spawn_session":
            await self._handle_spawn(ws, msg, user=user, chat_id=chat_id, app_id=app_id)
            return

        # kill/list → forward to session-mgr
        if action == "kill_session":
            text = f"/kill {session_name}"
        elif action == "list_sessions":
            text = "/sessions"
        else:
            return

        from channel_server.core.actor import Message as ActorMessage
        self.runtime.send(
            "system:session-mgr",
            ActorMessage(
                sender=address,
                payload={"text": text, "user": user, "chat_id": chat_id, "app_id": app_id},
            ),
        )

        await ws.send(json.dumps({
            "action": f"{action}_ack",
            "ok": True,
            "text": f"Command forwarded: {text}",
        }))

    async def _handle_spawn(self, ws, msg: dict, *, user: str, chat_id: str, app_id: str) -> None:
        """Spawn a child session: create anchor, actors, tmux process."""
        session_name = msg.get("session_name", "") or msg.get("name", "")
        tag = msg.get("tag", "") or session_name

        if not session_name:
            await ws.send(json.dumps({"action": "spawn_result", "ok": False, "text": "Missing session_name"}))
            return

        cc_addr = f"cc:{user}.{session_name}"
        existing = self.runtime.lookup(cc_addr)

        # Already active
        if existing and existing.state == "active":
            await ws.send(json.dumps({"action": "spawn_result", "ok": False, "text": f"Session '{session_name}' is already active"}))
            return

        # Resume suspended
        if existing and existing.state == "suspended":
            resume_chat_id = existing.metadata.get("chat_id", "") or chat_id
            if self.spawn_cc_process(user, session_name, tag=tag, chat_id=resume_chat_id):
                anchor_msg_id = existing.metadata.get("anchor_msg_id", "")
                if anchor_msg_id and self.feishu_adapter:
                    await self.feishu_adapter._update_anchor_card(
                        anchor_msg_id, f"\U0001f7e2 [{tag}] resumed",
                        body_text=f"Session [{tag}] has been resumed", template="green",
                    )
                await ws.send(json.dumps({"action": "spawn_result", "ok": True, "text": f"Session '{session_name}' resumed", "session_name": session_name}))
            else:
                await ws.send(json.dumps({"action": "spawn_result", "ok": False, "text": f"Session '{session_name}' resume failed"}))
            return

        # New session
        # 1. Create feishu thread anchor
        anchor_msg_id = None
        if self.feishu_adapter and chat_id:
            anchor_msg_id = await self.feishu_adapter.create_thread_anchor(chat_id, tag)
            if anchor_msg_id:
                await self.feishu_adapter.pin_message(anchor_msg_id)

        # 2. Spawn feishu thread actor
        thread_addr = f"feishu:{app_id}:{chat_id}:thread:{session_name}"
        if anchor_msg_id and chat_id:
            self.runtime.spawn(
                thread_addr, "feishu_inbound", tag=tag,
                transport=Transport(type="feishu_thread", config={"chat_id": chat_id, "root_id": anchor_msg_id}),
                downstream=[cc_addr],
            )

        # 3. Spawn CC actor (suspended, waiting for WS transport)
        downstream = [thread_addr] if anchor_msg_id else []
        self.runtime.spawn(
            cc_addr, "cc_session", tag=tag, state="suspended",
            parent=f"cc:{user}.root", downstream=downstream,
            metadata={"anchor_msg_id": anchor_msg_id or "", "chat_id": chat_id},
        )

        # 4. Start CC process via tmux
        if self.spawn_cc_process(user, session_name, tag=tag, chat_id=chat_id):
            await ws.send(json.dumps({"action": "spawn_result", "ok": True, "text": f"Session '{session_name}' spawned", "session_name": session_name}))
        else:
            # Rollback actors
            for addr in [cc_addr, thread_addr]:
                if addr:
                    await self.runtime.stop(addr)
            await ws.send(json.dumps({"action": "spawn_result", "ok": False, "text": f"Session '{session_name}' failed: tmux error"}))

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def spawn_cc_process(self, user: str, session_name: str, tag: str = "",
                         chat_id: str = "") -> bool:
        """Start CC process via cc-openclaw.sh --user in tmux. Returns True on success."""
        script = PROJECT_ROOT / "cc-openclaw.sh"
        if not script.exists():
            log.warning("cc-openclaw.sh not found at %s", script)
            return False

        window_name = f"{user}.{session_name}"

        env = os.environ.copy()
        env["OC_USER"] = user
        env["OC_SESSION"] = session_name
        env["OC_TAG"] = tag or session_name
        if chat_id:
            env["OC_CHAT_ID"] = chat_id

        # Run cc-openclaw.sh directly — it creates the tmux window itself
        cmd = [str(script), "--user", user, "--session", session_name]
        if tag:
            cmd.extend(["--tag", tag])

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=15, env=env)
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
