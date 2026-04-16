#!/usr/bin/env python3
"""Minimal Feishu WS listener — diagnostic probe.

Connects to Feishu WS, logs every received message to a file.
No channel server, no actor model, no async API calls — just listen and write.

Usage: uv run python3 tools/feishu-ws-probe.py
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("probe")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / ".openclaw" / "logs" / "feishu-ws-probe.log"
CREDS_FILE = PROJECT_ROOT / ".feishu-credentials.json"

count = 0


def on_message(data: P2ImMessageReceiveV1) -> None:
    global count
    try:
        event = data.event
        msg = event.message
        sender = event.sender

        msg_id = msg.message_id or ""
        msg_type = msg.message_type or "?"
        chat_id = msg.chat_id or ""
        sender_type = sender.sender_type or "?"
        sender_id = sender.sender_id.open_id if sender.sender_id else ""

        content = ""
        try:
            content = json.loads(msg.content or "{}").get("text", "")[:80]
        except Exception:
            pass

        count += 1
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{count}] {ts} msg_id={msg_id[:24]} type={msg_type} sender={sender_type}:{sender_id[:16]} text={content}"

        log.info(line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

    except Exception as e:
        log.error("callback error: %s", e)


def main():
    creds = json.loads(CREDS_FILE.read_text())
    app_id = creds["app_id"]
    app_secret = creds["app_secret"]

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"\n--- probe started {datetime.now(timezone.utc).isoformat()} ---\n")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )

    log.info("Starting Feishu WS probe (logging to %s)", LOG_FILE)
    log.info("Send messages in Feishu and watch for gaps in the count")

    # Run in thread with its own event loop (same as channel server does)
    import lark_oapi.ws.client as _ws_mod
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    _ws_mod.loop = new_loop
    ws_client.start()


if __name__ == "__main__":
    main()
