"""config.get / config.patch RPC client for OpenClaw Gateway.

Uses the `openclaw gateway call` CLI as a subprocess, since the RPC is
WebSocket-only (not HTTP).  This avoids reimplementing the WS framing and
auth protocol and stays compatible across Gateway upgrades.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class ConfigPatchClient:
    """Low-level client wrapping `openclaw gateway call` for config RPC."""

    def __init__(self, *, openclaw_bin: str = "openclaw") -> None:
        self._bin = openclaw_bin

    async def _call(self, method: str, params: dict | None = None) -> dict:
        """Call a Gateway RPC method via the CLI."""
        cmd = [self._bin, "gateway", "call", method, "--json"]
        if params:
            cmd += ["--params", json.dumps(params)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(f"openclaw gateway call {method} failed (rc={proc.returncode}): {err}")

        # stdout may contain warning lines before the JSON; find the first { or [
        text = stdout.decode()
        for i, ch in enumerate(text):
            if ch in ('{', '['):
                return json.loads(text[i:])

        raise RuntimeError(f"No JSON found in openclaw gateway call {method} output")

    async def get_config(self) -> dict:
        """config.get → {parsed: {...config...}, hash: "..."}

        Returns a normalised dict with keys:
          - "config": the parsed config object
          - "baseHash": the config hash (for use with config.patch)
        """
        raw = await self._call("config.get")
        return {
            "config": raw.get("parsed", {}),
            "baseHash": raw.get("hash", ""),
        }

    async def patch_config(self, base_hash: str, raw_json5: str) -> dict:
        """config.patch with a JSON5 raw string + baseHash."""
        return await self._call("config.patch", {
            "raw": raw_json5,
            "baseHash": base_hash,
        })

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    async def add_agent_with_binding(
        self,
        *,
        agent_id: str,
        agent_config: dict,
        channel: str,
        peer: dict,
    ) -> None:
        """Atomically add agent definition + peer binding.

        OpenClaw agents are in agents.list (array), not a top-level dict.
        Bindings are also an array. Both use full-replace in merge-patch,
        so we GET current arrays, append, and PATCH the whole thing.
        """
        data = await self.get_config()
        base_hash = data["baseHash"]
        config = data["config"]

        agents_section = copy.deepcopy(config.get("agents", {}))
        agents_list = list(agents_section.get("list", []))
        # Build agent entry in OpenClaw format
        new_agent = {"id": agent_id, "name": agent_id}
        new_agent.update(agent_config)
        agents_list.append(new_agent)
        agents_section["list"] = agents_list

        bindings = list(config.get("bindings", []))
        # Insert before the last entry if it's the fallback catch-all
        insert_idx = len(bindings)
        if bindings and not bindings[-1].get("match", {}).get("peer"):
            insert_idx = len(bindings) - 1
        bindings.insert(insert_idx, {
            "agentId": agent_id,
            "match": {"channel": channel, "peer": peer},
        })

        patch = json.dumps({"agents": agents_section, "bindings": bindings})
        await self.patch_config(base_hash, patch)

    async def add_binding(
        self,
        *,
        agent_id: str,
        channel: str,
        peer: dict | None = None,
        account_id: str | None = None,
    ) -> None:
        """Append a single binding."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        config = data["config"]

        bindings = list(config.get("bindings", []))
        match: dict[str, Any] = {"channel": channel}
        if peer is not None:
            match["peer"] = peer
        if account_id is not None:
            match["accountId"] = account_id
        bindings.append({"agentId": agent_id, "match": match})

        patch = json.dumps({"bindings": bindings})
        await self.patch_config(base_hash, patch)

    async def remove_binding(self, agent_id: str) -> None:
        """Remove all bindings matching agent_id."""
        data = await self.get_config()
        base_hash = data["baseHash"]
        config = data["config"]

        bindings = [
            b for b in config.get("bindings", [])
            if b.get("agentId") != agent_id
        ]

        patch = json.dumps({"bindings": bindings})
        await self.patch_config(base_hash, patch)


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
        await asyncio.sleep(self.MERGE_WINDOW)
        self._flush_task = None
        await self._flush()

    async def _flush(self) -> None:
        """Merge all pending ops, GET config, apply, PATCH."""
        if not self._pending:
            return

        ops = self._pending[:]
        self._pending.clear()

        for attempt in range(self.MAX_RETRIES):
            try:
                data = await self._client.get_config()
                base_hash = data["baseHash"]
                config = data["config"]

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

                patch = json.dumps({"agents": agents, "bindings": bindings})
                await self._client.patch_config(base_hash, patch)
                log.info("config.patch flushed: %d ops merged", len(ops))
                return
            except Exception:
                if attempt < self.MAX_RETRIES - 1:
                    log.warning("config.patch attempt %d failed, retrying", attempt + 1)
                    await asyncio.sleep(1)
                else:
                    log.error("config.patch failed after %d retries, dropping batch", self.MAX_RETRIES)
