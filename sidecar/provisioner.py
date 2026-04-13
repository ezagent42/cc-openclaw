"""Agent provisioner — lifecycle management for user and group agents."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sidecar.config_patch import ConfigPatchClient
from sidecar.db import Database

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_template(text: str, variables: dict[str, str]) -> str:
    """Simple {{key}} replacement."""
    for key, value in variables.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


class Provisioner:
    """Manages agent lifecycle: provision, suspend, restore, reset."""

    def __init__(
        self,
        *,
        db: Database,
        config_client: ConfigPatchClient,
        agents_dir: str,
        archived_dir: str,
        templates_dir: str,
        account_id: str,
        default_model: str,
    ) -> None:
        self.db = db
        self.config_client = config_client
        self.agents_dir = agents_dir
        self.archived_dir = archived_dir
        self.templates_dir = templates_dir
        self.account_id = account_id
        self.default_model = default_model

    def _copy_and_render_templates(
        self, template_name: str, dest: Path, variables: dict[str, str]
    ) -> None:
        """Copy template dir to dest, rendering {{key}} placeholders.

        Files ending in .tmpl have the extension stripped on output.
        """
        src = Path(self.templates_dir) / template_name
        for entry in src.iterdir():
            if entry.is_file():
                content = entry.read_text()
                rendered = _render_template(content, variables)

                out_name = entry.name
                if out_name.endswith(".tmpl"):
                    out_name = out_name[: -len(".tmpl")]

                (dest / out_name).write_text(rendered)

    async def provision_user(self, open_id: str, display_name: str) -> str:
        """Create a new user agent."""
        agent_id = f"u-{self.account_id}-{open_id}"
        created_at = _now_iso()

        # Create directories
        agent_root = Path(self.agents_dir) / agent_id
        workspace = agent_root / "workspace"
        agent_dir = agent_root / "agent"
        workspace.mkdir(parents=True, exist_ok=True)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Copy and render templates
        variables = {
            "display_name": display_name,
            "open_id": open_id,
            "created_at": created_at,
        }
        self._copy_and_render_templates("user-agent", workspace, variables)

        # Register with gateway
        agent_config = {"model": self.default_model}
        await self.config_client.add_agent_with_binding(
            agent_id=agent_id,
            agent_config=agent_config,
            channel="feishu",
            peer={"kind": "direct", "id": open_id},
            account_id=self.account_id,
        )

        # Register in database
        await self.db.create_agent(
            agent_id=agent_id,
            open_id=open_id,
            chat_id="",
            agent_type="user",
            workspace_path=str(workspace),
        )

        await self.db.write_audit(
            "provision", f"agent/{agent_id}", "system",
            details=f"user={display_name} open_id={open_id}",
        )

        log.info("Provisioned user agent %s for %s", agent_id, display_name)
        return agent_id

    async def suspend_user(self, open_id: str) -> None:
        """Suspend a user agent: remove binding, keep agent definition."""
        agent = await self.db.get_agent_by_open_id(open_id)
        if agent is None:
            raise ValueError(f"No user agent found for open_id={open_id}")

        agent_id = agent["agent_id"]

        await self.config_client.remove_binding(agent_id)
        await self.db.update_agent_status(agent_id, "suspended")
        await self.db.write_audit(
            "suspend", f"agent/{agent_id}", "system",
            details=f"open_id={open_id}",
        )

        log.info("Suspended user agent %s", agent_id)

    async def restore_user(self, open_id: str) -> None:
        """Restore a suspended user agent: re-add binding, set active."""
        agent = await self.db.get_agent_by_open_id(open_id)
        if agent is None:
            raise ValueError(f"No user agent found for open_id={open_id}")
        if agent["status"] != "suspended":
            raise ValueError(f"Agent {agent['agent_id']} is not suspended (status={agent['status']})")

        agent_id = agent["agent_id"]

        await self.config_client.add_binding(
            agent_id=agent_id,
            channel="feishu",
            peer={"kind": "direct", "id": open_id},
            account_id=self.account_id,
        )
        await self.db.update_agent_status(agent_id, "active")
        await self.db.write_audit(
            "restore", f"agent/{agent_id}", "system",
            details=f"open_id={open_id}",
        )

        log.info("Restored user agent %s", agent_id)

    async def reset_user(self, open_id: str, *, actor: str) -> None:
        """Reset: remove binding, archive workspace, delete from registry."""
        agent = await self.db.get_agent_by_open_id(open_id)
        if agent is None:
            raise ValueError(f"No user agent found for open_id={open_id}")

        agent_id = agent["agent_id"]
        agent_root = Path(self.agents_dir) / agent_id

        # Remove binding
        await self.config_client.remove_binding(agent_id)

        # Move workspace to archived dir with timestamp
        if agent_root.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            archived_name = f"{agent_id}_{timestamp}"
            dest = Path(self.archived_dir) / archived_name
            shutil.move(str(agent_root), str(dest))

        # Delete from registry
        await self.db.delete_agent(agent_id)
        await self.db.write_audit(
            "reset", f"agent/{agent_id}", actor,
            details=f"open_id={open_id}",
        )

        log.info("Reset user agent %s (actor=%s)", agent_id, actor)

    async def provision_group(self, chat_id: str) -> str:
        """Create a new group agent."""
        agent_id = f"g-{self.account_id}-{chat_id}"
        created_at = _now_iso()

        # Create directories
        agent_root = Path(self.agents_dir) / agent_id
        workspace = agent_root / "workspace"
        agent_dir = agent_root / "agent"
        workspace.mkdir(parents=True, exist_ok=True)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Copy and render templates
        variables = {
            "chat_id": chat_id,
            "created_at": created_at,
        }
        self._copy_and_render_templates("group-agent", workspace, variables)

        # Register with gateway
        agent_config = {"model": self.default_model}
        await self.config_client.add_agent_with_binding(
            agent_id=agent_id,
            agent_config=agent_config,
            channel="feishu",
            peer={"kind": "group", "id": chat_id},
            account_id=self.account_id,
        )

        # Register in database
        await self.db.create_agent(
            agent_id=agent_id,
            open_id="",
            chat_id=chat_id,
            agent_type="group",
            workspace_path=str(workspace),
        )

        await self.db.write_audit(
            "provision", f"agent/{agent_id}", "system",
            details=f"chat_id={chat_id}",
        )

        log.info("Provisioned group agent %s", agent_id)
        return agent_id
