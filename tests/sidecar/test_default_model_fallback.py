"""E2E: default_model fallback flows yaml → provisioner → agent_config that
openclaw will receive.

The fallback feature lets sidecar-config.yaml declare:

    openclaw:
      default_model:
        primary: openrouter/google/gemini-2.5-pro
        fallbacks:
          - openrouter/anthropic/claude-sonnet-4.5
          - openrouter/openai/gpt-4o

…instead of a plain string. Backwards compat: string form must still parse.

The flow under test:
  1. SidecarConfig.from_yaml → cfg.default_model is a dict (or str)
  2. Provisioner stores it as-is
  3. provision_user calls config_client.add_agent_with_binding(agent_config={"model": cfg.default_model})
  4. (Schema gate) the openclaw CLI publishes a schema that accepts this shape
"""

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sidecar.config import SidecarConfig
from sidecar.provisioner import Provisioner

TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent.parent / "sidecar" / "templates")


# ── yaml loader ─────────────────────────────────────────────────────


def test_yaml_loads_dict_default_model(tmp_path):
    """{primary, fallbacks} object form parses into cfg.default_model."""
    yaml_text = """
feishu: {}
openclaw:
  default_model:
    primary: openrouter/google/gemini-2.5-pro
    fallbacks:
      - openrouter/anthropic/claude-sonnet-4.5
      - openrouter/openai/gpt-4o
sidecar: {}
"""
    p = tmp_path / "sc.yaml"
    p.write_text(yaml_text)

    cfg = SidecarConfig.from_yaml(str(p))

    assert cfg.default_model == {
        "primary": "openrouter/google/gemini-2.5-pro",
        "fallbacks": [
            "openrouter/anthropic/claude-sonnet-4.5",
            "openrouter/openai/gpt-4o",
        ],
    }


def test_yaml_string_default_model_still_works(tmp_path):
    """Backwards compat: plain string default_model still parses."""
    yaml_text = """
feishu: {}
openclaw:
  default_model: openrouter/google/gemini-2.5-pro
sidecar: {}
"""
    p = tmp_path / "sc.yaml"
    p.write_text(yaml_text)

    cfg = SidecarConfig.from_yaml(str(p))

    assert cfg.default_model == "openrouter/google/gemini-2.5-pro"


# ── E2E: yaml → provisioner → captured agent_config ─────────────────


async def test_provision_user_forwards_dict_model(tmp_path):
    """E2E: dict default_model from yaml reaches gateway agent_config intact."""
    yaml_text = """
feishu: {}
openclaw:
  default_model:
    primary: openrouter/google/gemini-2.5-pro
    fallbacks:
      - openrouter/anthropic/claude-sonnet-4.5
sidecar: {}
"""
    yaml_path = tmp_path / "sc.yaml"
    yaml_path.write_text(yaml_text)
    cfg = SidecarConfig.from_yaml(str(yaml_path))

    config_client = AsyncMock()
    db = AsyncMock()
    agents_dir = tmp_path / "agents"
    archived_dir = tmp_path / "archived"
    agents_dir.mkdir()
    archived_dir.mkdir()

    p = Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=str(agents_dir),
        archived_dir=str(archived_dir),
        templates_dir=TEMPLATES_DIR,
        account_id="shared",
        default_model=cfg.default_model,
    )

    await p.provision_user("ou_e2e_test", "E2EUser")

    config_client.add_agent_with_binding.assert_awaited_once()
    kwargs = config_client.add_agent_with_binding.call_args.kwargs
    assert kwargs["agent_config"] == {
        "model": {
            "primary": "openrouter/google/gemini-2.5-pro",
            "fallbacks": ["openrouter/anthropic/claude-sonnet-4.5"],
        }
    }


async def test_provision_user_forwards_string_model(tmp_path):
    """Backwards compat E2E: string default_model still reaches gateway as string."""
    config_client = AsyncMock()
    db = AsyncMock()
    agents_dir = tmp_path / "agents"
    archived_dir = tmp_path / "archived"
    agents_dir.mkdir()
    archived_dir.mkdir()

    p = Provisioner(
        db=db,
        config_client=config_client,
        agents_dir=str(agents_dir),
        archived_dir=str(archived_dir),
        templates_dir=TEMPLATES_DIR,
        account_id="shared",
        default_model="openrouter/google/gemini-2.5-pro",
    )

    await p.provision_user("ou_str_test", "StrUser")

    kwargs = config_client.add_agent_with_binding.call_args.kwargs
    assert kwargs["agent_config"] == {"model": "openrouter/google/gemini-2.5-pro"}


# ── E2E schema gate: openclaw still declares fallback support ───────


async def test_openclaw_schema_declares_fallback_support():
    """Production gate: if openclaw upstream removes fallback from its schema,
    this fails — meaning our yaml dict form would no longer be valid.

    Skipped in environments without openclaw CLI installed.
    """
    if not shutil.which("openclaw"):
        pytest.skip("openclaw CLI not in PATH")

    proc = await asyncio.create_subprocess_exec(
        "openclaw", "config", "schema",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, "openclaw config schema failed"

    schema = json.loads(out)

    # Two surfaces accept agent model: agents.defaults.model and agents.list[].model
    for path in [
        ("properties", "agents", "properties", "defaults", "properties", "model"),
        ("properties", "agents", "properties", "list", "items", "properties", "model"),
    ]:
        node = schema
        for key in path:
            node = node[key]
        any_of = node.get("anyOf", [])
        object_branch = next(
            (b for b in any_of if b.get("type") == "object"), None
        )
        assert object_branch is not None, f"{path}: no object branch in anyOf"
        props = object_branch.get("properties", {})
        assert "primary" in props, f"{path}: object branch missing 'primary'"
        assert "fallbacks" in props, f"{path}: object branch missing 'fallbacks'"
        assert props["fallbacks"].get("type") == "array"
