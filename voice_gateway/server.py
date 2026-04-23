"""Voice gateway aiohttp server."""
import json
import logging
import os

import aiohttp.web
from aiohttp import web

from asr_route import asr_handler
from config import ALLOWED_ORIGINS
from session import Session
from tts_route import tts_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    # Handle CORS preflight
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)

    origin = request.headers.get("Origin", "")
    # Wildcard XOR allow-list: "*" means open gate (dev only), otherwise origin must
    # be in the explicit list. Mixing both ("*" in a list with other origins) falls
    # back to wildcard semantics — no per-origin echo for disallowed callers.
    if "*" in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    elif origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


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
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    port = int(os.environ.get("GATEWAY_PORT", "8089"))
    middlewares = [cors_middleware] if ALLOWED_ORIGINS else []
    app = aiohttp.web.Application(middlewares=middlewares)
    app.router.add_get("/ws", ws_handler)      # legacy — cc-openclaw voice-web
    app.router.add_get("/asr", asr_handler)    # new — stateless ASR
    app.router.add_get("/tts", tts_handler)    # new — stateless TTS

    log.info(f"Starting voice gateway on :{port}")
    aiohttp.web.run_app(app, port=port, print=None)


if __name__ == "__main__":
    main()
