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
from sidecar.provisioner import Provisioner

log = logging.getLogger("sidecar")


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

    # 6. Create and start HTTP server
    app = create_app(db=db, provisioner=provisioner)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.api_port)
    await site.start()
    log.info("sidecar ready on http://127.0.0.1:%d", cfg.api_port)

    # 7. Run forever (Feishu listener + reconciler are Phase 2)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
