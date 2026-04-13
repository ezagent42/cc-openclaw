"""config.get / config.patch RPC client for OpenClaw Gateway."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ConfigPatchClient:
    """Low-level client for OpenClaw config.get / config.patch RPC."""

    def __init__(self, *, gateway_url: str, auth_token: str) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._auth_token = auth_token

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

    async def get_config(self) -> dict:
        """POST /api/config.get -> {baseHash, config}"""
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{self._gateway_url}/api/config.get",
                json={},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def patch_config(self, base_hash: str, patch: dict) -> dict:
        """POST /api/config.patch with {baseHash, patch}"""
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{self._gateway_url}/api/config.patch",
                json={"baseHash": base_hash, "patch": patch},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def add_agent_with_binding(
        self,
        *,
        agent_id: str,
        agent_config: dict,
        channel: str,
        peer: str,
    ) -> dict:
        """Atomically add agent definition + binding in one patch.

        Arrays are full-replace in JSON merge patch, so we GET current
        bindings first, append the new one, and PATCH the entire array.
        """
        current = await self.get_config()
        base_hash = current["baseHash"]
        config = current["config"]

        agents = copy.deepcopy(config.get("agents", {}))
        agents[agent_id] = agent_config

        bindings = list(config.get("bindings", []))
        bindings.append({"agentId": agent_id, "channel": channel, "peer": peer})

        patch = {"agents": agents, "bindings": bindings}
        return await self.patch_config(base_hash, patch)

    async def add_binding(
        self,
        *,
        agent_id: str,
        channel: str,
        peer: str | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Append a binding. GET current bindings, append, PATCH entire array."""
        current = await self.get_config()
        base_hash = current["baseHash"]
        config = current["config"]

        bindings = list(config.get("bindings", []))
        new_binding: dict[str, Any] = {"agentId": agent_id, "channel": channel}
        if peer is not None:
            new_binding["peer"] = peer
        if account_id is not None:
            new_binding["accountId"] = account_id
        bindings.append(new_binding)

        return await self.patch_config(base_hash, {"bindings": bindings})

    async def remove_binding(self, agent_id: str) -> dict:
        """Remove all bindings matching agent_id. GET + filter + PATCH."""
        current = await self.get_config()
        base_hash = current["baseHash"]
        config = current["config"]

        bindings = [
            b for b in config.get("bindings", []) if b.get("agentId") != agent_id
        ]

        return await self.patch_config(base_hash, {"bindings": bindings})


class ConfigPatchQueue:
    """Rate-limited merge queue for config.patch operations."""

    RATE_LIMIT = 3  # per 60s
    MERGE_WINDOW = 5.0  # seconds
    MAX_RETRIES = 3

    def __init__(self, client: ConfigPatchClient) -> None:
        self._client = client
        self._pending: list[dict] = []
        self._flush_task: asyncio.Task | None = None

    async def enqueue(self, operation: dict) -> None:
        """Queue an operation dict. Flushes after MERGE_WINDOW seconds."""
        self._pending.append(operation)
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def flush_now(self) -> None:
        """Force immediate flush."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        await self._flush()

    async def _delayed_flush(self) -> None:
        """Wait MERGE_WINDOW then flush."""
        await asyncio.sleep(self.MERGE_WINDOW)
        self._flush_task = None
        await self._flush()

    async def _flush(self) -> None:
        """Merge all pending ops, GET config, apply, PATCH.

        Retry up to MAX_RETRIES on baseHash conflict.
        After MAX_RETRIES, log error and drop batch.
        """
        if not self._pending:
            return

        ops = self._pending[:]
        self._pending.clear()

        for attempt in range(self.MAX_RETRIES):
            try:
                current = await self._client.get_config()
                base_hash = current["baseHash"]
                config = current["config"]

                agents = copy.deepcopy(config.get("agents", {}))
                bindings = list(config.get("bindings", []))

                for op in ops:
                    if "add_agent" in op:
                        agents[op["agent_id"]] = op["add_agent"]
                    if "add_binding" in op:
                        bindings.append(op["add_binding"])
                    if "remove_binding_agent_id" in op:
                        rid = op["remove_binding_agent_id"]
                        bindings = [
                            b for b in bindings if b.get("agentId") != rid
                        ]

                patch = {"agents": agents, "bindings": bindings}
                await self._client.patch_config(base_hash, patch)
                return  # success
            except Exception:
                if attempt < self.MAX_RETRIES - 1:
                    log.warning(
                        "config.patch attempt %d failed, retrying", attempt + 1
                    )
                else:
                    log.error(
                        "config.patch failed after %d retries, dropping batch",
                        self.MAX_RETRIES,
                    )
