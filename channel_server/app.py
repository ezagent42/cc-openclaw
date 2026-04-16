"""Channel server entry point — wires runtime + adapters together."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from channel_server.adapters.cc.adapter import CCAdapter
from channel_server.core.persistence import load_actors, save_actors
from channel_server.core.runtime import ActorRuntime

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ChannelServerApp:
    """Main application — assembles runtime, adapters, and persistence."""

    def __init__(
        self,
        *,
        admin_chat_id: str | None = None,
        feishu_enabled: bool = True,
        port: int = 0,
        service_name: str = "channel-server",
    ) -> None:
        self.admin_chat_id = admin_chat_id
        self.feishu_enabled = feishu_enabled
        self.service_name = service_name
        self.runtime = ActorRuntime()
        self.actors_file = PROJECT_ROOT / ".workspace" / "actors.json"
        self.feishu_adapter = None
        self.cc_adapter = CCAdapter(self.runtime, port=port)

        # Inject runtime into CCSessionHandler so send_summary can resolve parent_feishu
        from channel_server.core.handler import HANDLER_REGISTRY
        HANDLER_REGISTRY["cc_session"].set_runtime(self.runtime)
        self.pidfile = PROJECT_ROOT / ".channel-server.pid"
        self._persist_task: asyncio.Task | None = None
        self._stopped = False

    async def start(self) -> None:
        """Start the channel server.

        1. Restore persisted actors (all marked suspended)
        2. Start CC adapter (WebSocket server) and write pidfile
        3. Init Feishu adapter if enabled and wire cc_adapter.feishu_adapter
        4. Reactivate feishu actors (their transport is always available)
        5. Start runtime message loops
        6. Start periodic persistence loop (every 30s)
        """
        # 1. Restore persisted actors
        self.actors_file.parent.mkdir(parents=True, exist_ok=True)
        restored = load_actors(self.actors_file)
        for address, actor in restored.items():
            actor.state = "suspended"
            self.runtime.actors[address] = actor
            self.runtime.mailboxes[address] = asyncio.Queue()
        if restored:
            log.info("Restored %d actors from %s", len(restored), self.actors_file)

        # 2. Start CC adapter
        actual_port = await self.cc_adapter.start()
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(json.dumps({
            "pid": os.getpid(),
            "port": actual_port,
            "service": self.service_name,
        }))
        log.info("Wrote pidfile: %s", self.pidfile)

        # 3. Init Feishu adapter if enabled
        if self.feishu_enabled:
            feishu_client = self._init_feishu_client()
            if feishu_client is not None:
                from channel_server.adapters.feishu.adapter import FeishuAdapter
                self.feishu_adapter = FeishuAdapter(self.runtime, feishu_client)
                self.cc_adapter.feishu_adapter = self.feishu_adapter
                log.info("Feishu adapter initialized")

                # Start Feishu WS event listener
                creds = json.loads((PROJECT_ROOT / ".feishu-credentials.json").read_text())
                self.feishu_adapter.start_feishu_ws(creds["app_id"], creds["app_secret"])
                log.info("Feishu WS listener started")

                # 4. Reactivate actors that don't need external transport
                for actor in self.runtime.actors.values():
                    if actor.state != "suspended":
                        continue
                    # Feishu actors: transport is the API, always available
                    if actor.transport and actor.transport.type in ("feishu_chat", "feishu_thread"):
                        actor.state = "active"
                        log.info("Reactivated feishu actor: %s", actor.address)
                    # System actors: no transport needed, always active
                    elif actor.transport is None and actor.address.startswith("system:"):
                        actor.state = "active"
                        log.info("Reactivated system actor: %s", actor.address)

        # 5. Spawn admin actor if admin_chat_id is set
        if self.admin_chat_id and self.feishu_adapter:
            admin_feishu_addr = f"feishu:{self.feishu_adapter.app_id}:{self.admin_chat_id}"
            admin_actor_addr = "system:admin"

            # Ensure feishu actor exists for admin chat
            if self.runtime.lookup(admin_feishu_addr) is None:
                from channel_server.core.actor import Transport
                self.runtime.spawn(
                    admin_feishu_addr,
                    "feishu_inbound",
                    tag="admin",
                    transport=Transport(type="feishu_chat", config={"chat_id": self.admin_chat_id}),
                    downstream=[admin_actor_addr],
                )

            # Spawn admin actor between feishu and CC (if not already restored from persistence)
            if self.runtime.lookup(admin_actor_addr) is None:
                self.runtime.spawn(
                    admin_actor_addr,
                    "admin",
                    tag="admin",
                )
            log.info("Spawned admin actor: %s", admin_actor_addr)

        # 5b. Spawn session-mgr actor (global singleton, no transport)
        if self.runtime.lookup("system:session-mgr") is None:
            self.runtime.spawn("system:session-mgr", "session_mgr", tag="session-mgr")
            log.info("Spawned session-mgr actor")

        if self.admin_chat_id and self.feishu_adapter:
            # Send startup notification
            await self.feishu_adapter.send_startup_notification(self.admin_chat_id)

        # 6. Start runtime message loops
        self._runtime_task = asyncio.create_task(self.runtime.run())

        # 7. Start periodic persistence
        self._persist_task = asyncio.create_task(self._persist_loop())

    async def stop(self) -> None:
        """Save actors, shutdown runtime, stop CC adapter, remove pidfile."""
        if self._stopped:
            return
        self._stopped = True

        # Cancel persistence loop
        if self._persist_task is not None and not self._persist_task.done():
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass

        # Save actors
        try:
            save_actors(self.runtime.actors, self.actors_file)
            log.info("Saved actors to %s", self.actors_file)
        except Exception as e:
            log.error("Failed to save actors: %s", e)

        # Shutdown runtime
        await self.runtime.shutdown()

        # Stop CC adapter
        await self.cc_adapter.stop()

        # Remove pidfile
        try:
            self.pidfile.unlink(missing_ok=True)
            log.info("Removed pidfile")
        except Exception:
            pass

    def notify_admin(self, text: str) -> None:
        """Send a system notification to the admin actor."""
        from channel_server.core.actor import Message

        self.runtime.send(
            "system:admin",
            Message(sender="system:runtime", payload={"msg_type": "system", "text": text}),
        )

    async def _persist_loop(self) -> None:
        """Save actors every 30 seconds. Handles cancellation gracefully."""
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    save_actors(self.runtime.actors, self.actors_file)
                except Exception as e:
                    log.warning("Periodic save failed: %s", e)
        except asyncio.CancelledError:
            pass

    def _init_feishu_client(self):
        """Load .feishu-credentials.json and build a lark Client.

        Returns the Client or None if credentials are missing or import fails.
        """
        creds_file = PROJECT_ROOT / ".feishu-credentials.json"
        if not creds_file.exists():
            log.warning("Feishu credentials not found at %s", creds_file)
            return None
        try:
            creds = json.loads(creds_file.read_text())
            app_id = creds.get("app_id", "")
            app_secret = creds.get("app_secret", "")
            if not app_id or not app_secret:
                log.warning("Feishu credentials missing app_id or app_secret")
                return None

            import lark_oapi as lark
            client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            return client
        except ImportError:
            log.warning("lark_oapi not installed — Feishu disabled")
            return None
        except Exception as e:
            log.warning("Failed to init Feishu client: %s", e)
            return None


async def main() -> None:
    """Entry point — configure and run the channel server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Read service name from cc-openclaw.sh (SESSION_NAME) or env
    service_name = os.environ.get("SERVICE_NAME", "")
    if not service_name:
        launcher = PROJECT_ROOT / "cc-openclaw.sh"
        if launcher.exists():
            for line in launcher.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("SESSION_NAME="):
                    service_name = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    service_name = service_name or "channel-server"

    # Read app_id for process identification
    app_id_short = ""
    creds_file = PROJECT_ROOT / ".feishu-credentials.json"
    if creds_file.exists():
        try:
            app_id_short = json.loads(creds_file.read_text()).get("app_id", "")[-8:]
        except Exception:
            pass

    # Set process title: {service}-channel-server[{app_id_suffix}]
    proc_title = f"{service_name}-channel-server"
    if app_id_short:
        proc_title += f"[{app_id_short}]"
    try:
        import setproctitle
        setproctitle.setproctitle(proc_title)
    except ImportError:
        pass

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID", "")
    feishu_enabled = os.environ.get("FEISHU_ENABLED", "true").lower() not in ("0", "false", "no")
    port = int(os.environ.get("CHANNEL_SERVER_PORT", "0"))

    app = ChannelServerApp(
        admin_chat_id=admin_chat_id or None,
        feishu_enabled=feishu_enabled,
        port=port,
        service_name=service_name,
    )

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Signal received — shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await app.start()

    feishu_status = "enabled" if app.feishu_adapter else "disabled"
    print(
        f"\n  {service_name}-channel-server started"
        f"\n  WebSocket: ws://127.0.0.1:{app.cc_adapter.port}"
        f"\n  Feishu:    {feishu_status}"
        f"\n  PID:       {os.getpid()}"
        f"\n",
        flush=True,
    )

    await stop_event.wait()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
