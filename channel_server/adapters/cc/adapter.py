"""CC adapter — bridges Claude Code sessions (via WebSocket) to the actor runtime."""
from __future__ import annotations

import json
import logging
import os
import shlex
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


# Meta-operations on the actor system that route through the command registry.
# Other WS actions (reply, react, send_file, send_summary, update_title, forward,
# tool_notify, ...) continue to flow through the existing actor-message path.
WS_ACTION_TO_COMMAND: dict[str, str] = {
    "spawn_session": "spawn",
    "kill_session":  "kill",
    "list_sessions": "sessions",
}


def ws_args_to_text(cmd_name: str, payload: dict) -> str:
    """Serialize a WS action payload into shell-tokenizable command text."""
    if cmd_name == "spawn":
        name = payload.get("session_name") or payload.get("name", "")
        tag = payload.get("tag", "")
        parts = []
        if name:
            parts.append(shlex.quote(name))
        if tag:
            parts.extend(["--tag", shlex.quote(tag)])
        return " ".join(parts)
    if cmd_name == "kill":
        name = payload.get("session_name") or payload.get("name", "")
        return shlex.quote(name) if name else ""
    if cmd_name == "sessions":
        return ""
    return ""


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
        self._dispatcher = None  # Set by app.py after construction

        runtime.register_transport_handler("websocket", self.push_to_cc)

    # ------------------------------------------------------------------
    # Dispatcher wiring
    # ------------------------------------------------------------------

    def set_dispatcher(self, dispatcher) -> None:
        """Wire the CommandDispatcher in after construction."""
        self._dispatcher = dispatcher

    def _ws_to_actor(self, ws) -> str | None:
        """Return the cc:* actor address bound to this WS connection, or None."""
        return self._ws_to_address.get(id(ws))

    def _ws_user(self, ws) -> str:
        """Extract feishu_user:{username} from the WS-bound actor address."""
        addr = self._ws_to_actor(ws)
        if not addr or not addr.startswith("cc:"):
            return ""
        user_session = addr[3:]  # strip "cc:"
        user = user_session.split(".")[0]
        return f"feishu_user:{user}"

    def _ws_chat(self, ws) -> str | None:
        """Get chat_id from the actor's metadata."""
        addr = self._ws_to_actor(ws)
        if not addr:
            return None
        actor = self.runtime.lookup(addr)
        if actor is None:
            return None
        return actor.metadata.get("chat_id")

    async def reply(self, ctx, text: str) -> None:
        """Replies for CC-MCP commands go to the Feishu chat the session is bound to."""
        if self.feishu_adapter is not None:
            await self.feishu_adapter.reply(ctx, text)

    async def reply_error(self, ctx_partial, text: str) -> None:
        if self.feishu_adapter is not None:
            await self.feishu_adapter.reply_error(ctx_partial, text)

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
            return

        # -- Meta-action commands route through CommandDispatcher --
        if action in WS_ACTION_TO_COMMAND and self._dispatcher is not None:
            cmd_name = WS_ACTION_TO_COMMAND[action]
            raw_text = f"/{cmd_name} {ws_args_to_text(cmd_name, msg)}".strip()
            source_actor = self._ws_to_actor(ws)
            ctx_partial = {
                "source": "cc_mcp",
                "user": self._ws_user(ws),
                "chat_id": self._ws_chat(ws),
                "app_id": self.feishu_adapter.app_id if self.feishu_adapter else "",
                "current_actor": source_actor,
                "parent_actor": None,
                "thread_root_id": None,
                "raw_msg": None,
            }
            handled = await self._dispatcher.dispatch_from_adapter(
                adapter=self, raw_text=raw_text,
                source_actor=source_actor, ctx_partial=ctx_partial,
            )
            if handled:
                return

        if action == "pong":
            return  # ignore keepalive pongs

        if action == "tool_notify":
            # tool_notify can come from anonymous hook connections (no register).
            if self._ws_to_address.get(id(ws)):
                self._route_to_actor(ws, msg)
            else:
                self._route_anonymous_tool_notify(msg)
            return

        # -- Actor payload actions (forwarded to actor mailbox) --
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

        # Voice gateway registration (voice: prefix)
        if instance_id.startswith("voice:"):
            await self._handle_voice_register(ws, msg, instance_id)
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
            # T12-comms-2: log the incoming state so heartbeat-driven
            # re-registers on a suspended actor (orphan-transport
            # recovery path) are visible in the log without grepping
            # for the preceding "Detached transport" line.
            prev_state = actor.state
            self.runtime.attach(
                address,
                Transport(type="websocket", config={"instance_id": instance_id}),
            )
            if prev_state == "suspended":
                log.info(
                    "Re-attached transport to existing CC actor (resumed from "
                    "suspended — likely heartbeat recovery): %s",
                    address,
                )
            else:
                log.info(
                    "Attached transport to existing CC actor (state=%s): %s",
                    prev_state,
                    address,
                )

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

        await ws.send(json.dumps({"action": "registered", "address": address}))

    async def _handle_voice_register(self, ws, msg: dict, instance_id: str) -> None:
        """Register a voice gateway: spawn voice actor + paired CC session."""
        from channel_server.core.actor import Transport

        address = instance_id  # voice:user.voice-1
        cc_addr = "cc:" + instance_id[len("voice:"):]  # cc:user.voice-1
        tag = msg.get("tag_name", "") or "Voice"

        # Spawn voice actor with WS transport
        self.runtime.spawn(
            address, "voice_session", tag=tag,
            transport=Transport(type="websocket", config={"instance_id": instance_id}),
            metadata={"cc_target": cc_addr},
        )

        # Spawn paired CC session (suspended — waiting for Claude Code WS)
        # on_spawn TransportSend is no-op because actor has no transport yet
        self.runtime.spawn(
            cc_addr, "cc_session", tag=f"voice-{tag}",
            state="suspended",
            downstream=[address],  # CC responses route to voice actor
        )

        # Start Claude Code tmux process
        parts = instance_id[len("voice:"):].split(".", 1)
        user = parts[0] if len(parts) > 1 else ""
        session_name = parts[1] if len(parts) > 1 else parts[0]
        self.spawn_cc_process(user, session_name, cc_addr, tag)

        # Track WS mapping
        self._ws_to_address[id(ws)] = address
        self._address_to_ws[address] = ws

        await ws.send(json.dumps({"action": "registered", "address": address}))
        log.info("Voice registered: %s → CC %s", address, cc_addr)

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def handle_disconnect(self, ws) -> None:
        """Detach transport from CC actor on WS disconnect.

        T12-comms-2 (2026-04-24): ``detach`` always suspends the actor;
        the paired client-side heartbeat (`ChannelClient._heartbeat`)
        will re-register within one interval and the server-side
        ``attach`` resumes. No server→client push of a ``detached``
        frame — the WS is already closed at this point so a send would
        fail anyway; heartbeat is the resilience layer.
        """
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

        Uses session+user from the payload to find the exact cc actor.
        Falls back to chat_id matching if session info is missing.
        """
        chat_id = msg.get("chat_id", "")
        text = msg.get("text", "")
        if not chat_id or not text:
            log.warning("_route_anonymous_tool_notify: missing chat_id or text")
            return

        from channel_server.core.actor import Message as ActorMessage

        # Primary: find cc actor by user+session (exact match)
        session = msg.get("session", "")
        user = msg.get("user", "")
        if user and session:
            target_addr = f"cc:{user}.{session}"
            target = self.runtime.lookup(target_addr)
            if target and target.state != "ended":
                self.runtime.send(target_addr, ActorMessage(
                    sender="hook:tool_notify",
                    payload={"action": "tool_notify", "text": text},
                ))
                return

        # Fallback: find cc actor by downstream feishu chat_id match
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

        log.debug("_route_anonymous_tool_notify: no cc actor for chat_id=%s session=%s", chat_id, session)

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
