"""Voice gateway aiohttp server."""
import json
import logging
import os

import aiohttp.web

from session import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


async def ws_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    log.info("Browser connected")

    msg = await ws.receive()
    if msg.type == aiohttp.WSMsgType.TEXT:
        data = json.loads(msg.data)
        if data.get("type") == "start":
            mode = data.get("mode", "e2e")
            log.info(f"Starting session in {mode} mode")
            if mode == "split":
                from session_split import SplitSession
                session = SplitSession(ws, start_config=data)
            else:
                session = Session(ws, start_config=data)
            await session.run()

    log.info("Browser disconnected")
    return ws


def main():
    # Load .env.local from voice-web directory
    env_path = os.path.join(os.path.dirname(__file__), "..", "voice-web", ".env.local")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    port = int(os.environ.get("GATEWAY_PORT", "8089"))
    app = aiohttp.web.Application()
    app.router.add_get("/ws", ws_handler)

    log.info(f"Starting voice gateway on :{port}")
    aiohttp.web.run_app(app, port=port, print=None)


if __name__ == "__main__":
    main()
