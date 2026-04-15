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
    ) -> None:
        self.admin_chat_id = admin_chat_id
        self.feishu_enabled = feishu_enabled
        self.runtime = ActorRuntime()
        self.actors_file = PROJECT_ROOT / ".workspace" / "actors.json"
        self.feishu_adapter = None
        self.cc_adapter = CCAdapter(self.runtime, port=port)
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

                # 4. Reactivate feishu actors (their transport is always available)
                for actor in self.runtime.actors.values():
                    if (
                        actor.state == "suspended"
                        and actor.transport is not None
                        and actor.transport.type in ("feishu_chat", "feishu_thread")
                    ):
                        actor.state = "active"
                        log.info("Reactivated feishu actor: %s", actor.address)

        # 5. Start runtime message loops
        self._runtime_task = asyncio.create_task(self.runtime.run())

        # 6. Start periodic persistence
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

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID", "")
    feishu_enabled = os.environ.get("FEISHU_ENABLED", "true").lower() not in ("0", "false", "no")
    port = int(os.environ.get("CHANNEL_SERVER_PORT", "0"))

    app = ChannelServerApp(
        admin_chat_id=admin_chat_id or None,
        feishu_enabled=feishu_enabled,
        port=port,
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
        f"\n  Channel Server started"
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
