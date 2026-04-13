"""Sidecar configuration loader with YAML + env-var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from typing import Any

import yaml

_ENV_RE = re.compile(r"^\$\{([^}]+)\}$")


def _substitute(value: Any) -> Any:
    """Replace ${ENV_VAR} strings with their environment value."""
    if not isinstance(value, str):
        return value
    m = _ENV_RE.match(value)
    if m:
        return os.environ.get(m.group(1), "")
    return value


@dataclass
class SidecarConfig:
    """Configuration for the sidecar service."""

    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    user_group_chat_id: str = ""
    admin_group_chat_id: str = ""
    # OpenClaw
    gateway_url: str = "http://127.0.0.1:18789"
    auth_token: str = ""
    default_model: str = "openrouter/google/gemini-3.1-flash-lite-preview"
    account_id: str = "shared"
    # Sidecar
    api_port: int = 18791
    db_path: str = "~/.openclaw/sidecar.sqlite"
    reconcile_interval_minutes: int = 10
    deny_rate_limit_minutes: int = 10
    # Paths
    agents_dir: str = "~/.openclaw/agents"
    archived_dir: str = "~/.openclaw/archived"
    templates_dir: str = ""  # default to sidecar/templates/

    @classmethod
    def from_yaml(cls, path: str) -> SidecarConfig:
        """Load from YAML. Supports ${ENV_VAR} substitution in string values."""
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        flat: dict[str, Any] = {}

        # feishu section
        feishu = raw.get("feishu", {})
        flat["feishu_app_id"] = feishu.get("app_id", "")
        flat["feishu_app_secret"] = feishu.get("app_secret", "")
        flat["user_group_chat_id"] = feishu.get("user_group_chat_id", "")
        flat["admin_group_chat_id"] = feishu.get("admin_group_chat_id", "")

        # openclaw section
        oc = raw.get("openclaw", {})
        flat["gateway_url"] = oc.get("gateway_url", "http://127.0.0.1:18789")
        flat["auth_token"] = oc.get("auth_token", "")
        flat["default_model"] = oc.get("default_model", "openrouter/google/gemini-3.1-flash-lite-preview")
        flat["account_id"] = oc.get("account_id", "shared")

        # sidecar section
        sc = raw.get("sidecar", {})
        flat["api_port"] = sc.get("api_port", 18791)
        flat["db_path"] = sc.get("db_path", "~/.openclaw/sidecar.sqlite")
        flat["reconcile_interval_minutes"] = sc.get("reconcile_interval_minutes", 10)
        flat["deny_rate_limit_minutes"] = sc.get("deny_rate_limit_minutes", 10)
        flat["agents_dir"] = sc.get("agents_dir", "~/.openclaw/agents")
        flat["archived_dir"] = sc.get("archived_dir", "~/.openclaw/archived")

        # templates section
        tmpl = raw.get("templates", {})
        flat["templates_dir"] = tmpl.get("user_agent_dir", "")

        # Apply env-var substitution to all string values
        valid_names = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in flat.items():
            if key in valid_names:
                kwargs[key] = _substitute(value)

        return cls(**kwargs)
