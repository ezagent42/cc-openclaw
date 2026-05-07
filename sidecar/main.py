"""Sidecar entry point — wires config, database, provisioner, and HTTP API."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import web

from sidecar.api import create_app
from sidecar.config import SidecarConfig
from sidecar.config_patch import ConfigPatchClient
from sidecar.db import Database
from sidecar.feishu_events import FeishuEventHandler
from sidecar.provisioner import Provisioner
from sidecar.reconciler import LarkFeishuGroupAPI, Reconciler


def write_pidfile_atomic(pidfile_dir: str, pid: int, port: int) -> str:
    """Atomically write {pid, port} JSON to <pidfile_dir>/sidecar.pid.

    `dir=pidfile_dir` passed to tempfile.mkstemp is LOAD-BEARING, not
    incidental: os.replace is only atomic when src and dst share a
    filesystem. Default tempfile dir ($TMPDIR → /var/folders/.../T on
    macOS) might be on a different volume; never use the default here.

    Returns the absolute path of the pidfile written.
    """
    import json
    import tempfile

    os.makedirs(pidfile_dir, exist_ok=True)
    pidfile_path = os.path.join(pidfile_dir, "sidecar.pid")
    fd, tmp_path = tempfile.mkstemp(prefix=".sidecar.pid.", dir=pidfile_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": pid, "port": port}))
        os.replace(tmp_path, pidfile_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return pidfile_path


log = logging.getLogger("sidecar")


async def reconciler_loop(reconciler: Reconciler, interval_minutes: int) -> None:
    """Run reconciliation periodically in the background."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            await reconciler.reconcile()
        except Exception:
            log.exception("reconciliation failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # 1. Parse config path
    config_path = sys.argv[1] if len(sys.argv) > 1 else "sidecar-config.yaml"
    log.info("loading config from %s", config_path)
    cfg = SidecarConfig.from_yaml(config_path)

    # 2. Initialize database
    db_path = os.path.expanduser(cfg.db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = Database(db_path)
    await db.init()
    log.info("database ready at %s", db_path)

    # 3. Create config-patch client (uses openclaw CLI subprocess)
    config_client = ConfigPatchClient()

    # 4. Resolve paths
    agents_dir = os.path.expanduser(cfg.agents_dir)
    archived_dir = os.path.expanduser(cfg.archived_dir)
    templates_dir = cfg.templates_dir or os.path.join(
        os.path.dirname(__file__), "templates"
    )

    # 5. Create provisioner
    provisioner = Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=agents_dir,
        archived_dir=archived_dir,
        templates_dir=templates_dir,
        account_id=cfg.account_id,
        default_model=cfg.default_model,
    )

    # 6. Feishu event handler (events arrive via HTTP from channel_server)
    feishu_enabled = bool(cfg.feishu_app_id and cfg.feishu_app_secret)
    event_handler: FeishuEventHandler | None = None

    if feishu_enabled:
        event_handler = FeishuEventHandler(
            db=db,
            provisioner=provisioner,
            user_group_chat_id=cfg.user_group_chat_id,
            admin_group_chat_id=cfg.admin_group_chat_id,
        )

    # 7. Create Feishu API client (shared by HTTP API + Reconciler)
    feishu_api: LarkFeishuGroupAPI | None = None
    if feishu_enabled:
        feishu_api = LarkFeishuGroupAPI(cfg.feishu_app_id, cfg.feishu_app_secret)

    # 8. Create broadcaster (for admin DM notifications)
    broadcaster = None
    if feishu_enabled:
        from sidecar.broadcast import FeishuBroadcaster
        broadcaster = FeishuBroadcaster(cfg.feishu_app_id, cfg.feishu_app_secret)

    # 9. Create and start HTTP server
    app = create_app(
        db=db,
        provisioner=provisioner,
        event_handler=event_handler,
        feishu_api=feishu_api,
        broadcaster=broadcaster,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    # Honor api_port from config — the plugin's emergency-fallback URL
    # (openclaw-sidecar-plugin/openclaw.plugin.json sidecarUrl default) is
    # keyed to this port for the fresh-install window before the pidfile
    # is written, so they must agree. A fixed port also makes diagnosis
    # trivial (`curl :18791/api/v1/agents`). When api_port == 0 we let the
    # OS pick (legacy "no conflicts" behaviour for dev side-by-side).
    site = web.TCPSite(runner, "127.0.0.1", cfg.api_port)
    await site.start()
    actual_port = site._server.sockets[0].getsockname()[1]

    # Write pidfile to ~/.openclaw/sidecar.pid (machine-level state alongside
    # sidecar.sqlite). The plugin reads from this absolute path regardless
    # of its own cwd. See docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md.
    pidfile_dir = os.path.expanduser("~/.openclaw")
    pidfile_path = write_pidfile_atomic(pidfile_dir, pid=os.getpid(), port=actual_port)
    log.info("sidecar ready on http://127.0.0.1:%d (pidfile: %s)", actual_port, pidfile_path)

    if feishu_enabled:
        reconciler = Reconciler(
            db=db,
            provisioner=provisioner,
            feishu_api=feishu_api,
            user_group_chat_id=cfg.user_group_chat_id,
            admin_group_chat_id=cfg.admin_group_chat_id,
        )

        # Run initial reconciliation
        try:
            await reconciler.reconcile()
            log.info("Initial reconciliation complete")
        except Exception:
            log.exception("Initial reconciliation failed — will retry on schedule")

        # Start periodic reconciler loop
        asyncio.create_task(
            reconciler_loop(reconciler, cfg.reconcile_interval_minutes),
            name="reconciler-loop",
        )
        log.info(
            "Reconciler loop started (every %d minutes)",
            cfg.reconcile_interval_minutes,
        )
    else:
        log.warning(
            "Feishu credentials not configured — event listener and reconciler disabled"
        )

    # 10. Run forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
