#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""Minimal Feishu sender — invoked by Claude Code hooks.

Reads `feishu.app_id` + `feishu.app_secret` from `sidecar-config.yaml`,
mints a tenant-access-token, and POSTs a text message to the given
`chat_id` via Feishu's Open API. Fire-and-forget — exits 0 on
success, non-zero on auth/HTTP failure. Hook scripts that wrap this
should `|| true` so a send failure never blocks Claude.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

# Authoritative cred source — same file channel-server reads (channel_server/app.py:99).
# sidecar-config.yaml is a DIFFERENT app (admin-group only); never use it here.
DEFAULT_CREDS = "/Users/h2oslabs/cc-openclaw/.feishu-credentials.json"


def _load_creds(creds_path: str) -> tuple[str, str]:
    try:
        creds = json.loads(open(creds_path).read())
    except FileNotFoundError:
        sys.exit(f"feishu-notify: creds file not found: {creds_path}")
    app_id = creds.get("app_id", "")
    app_secret = creds.get("app_secret", "")
    if not app_id or not app_secret:
        sys.exit(f"feishu-notify: missing app_id/app_secret in {creds_path}")
    return app_id, app_secret


def _tenant_token(app_id: str, app_secret: str) -> str:
    r = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        sys.exit(f"feishu-notify: token error {data}")
    return data["tenant_access_token"]


def send_text(chat_id: str, text: str, *, app_id: str, app_secret: str) -> None:
    token = _tenant_token(app_id, app_secret)
    r = httpx.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    if r.status_code != 200:
        sys.exit(f"feishu-notify: HTTP {r.status_code} — body: {r.text}")
    data = r.json()
    if data.get("code") != 0:
        sys.exit(f"feishu-notify: send error {data}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Minimal Feishu sender — Claude Code hook helper."
    )
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--creds", default=os.environ.get("CC_OPENCLAW_CREDS", DEFAULT_CREDS))
    args = ap.parse_args()
    app_id, app_secret = _load_creds(args.creds)
    send_text(args.chat_id, args.text, app_id=app_id, app_secret=app_secret)


if __name__ == "__main__":
    main()
