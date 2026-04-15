# voice/token_server.py
"""Async token server — Feishu JSSDK config + auth code verification + LiveKit token."""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

import httpx
from aiohttp import web

from voice.config import VoiceConfig

log = logging.getLogger(__name__)

FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_OIDC_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_JSAPI_TICKET_URL = "https://open.feishu.cn/open-apis/jssdk/ticket/get"

CORS_ORIGIN = "https://voice.ezagent.chat"


async def get_app_access_token(app_id: str, app_secret: str) -> str | None:
    """Get app_access_token from Feishu."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("app_access_token failed: %s", data.get("msg"))
                return None
            return data["app_access_token"]
    except Exception as e:
        log.warning("get_app_access_token error: %s", e)
        return None


async def get_jsapi_ticket(app_access_token: str) -> str | None:
    """Get jsapi_ticket for JSSDK config signature."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_JSAPI_TICKET_URL,
                headers={"Authorization": f"Bearer {app_access_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("jsapi_ticket failed: %s", data.get("msg"))
                return None
            return data["data"]["ticket"]
    except Exception as e:
        log.warning("get_jsapi_ticket error: %s", e)
        return None


def generate_jssdk_signature(ticket: str, nonce: str, timestamp: str, url: str) -> str:
    """SHA1 signature for h5sdk.config()."""
    sign_str = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(sign_str.encode()).hexdigest()


async def verify_feishu_auth_code(code: str, app_id: str, app_secret: str) -> dict | None:
    """Exchange auth code for user info. Returns {open_id, name, access_token} or None."""
    try:
        app_token = await get_app_access_token(app_id, app_secret)
        if not app_token:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                FEISHU_OIDC_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                log.warning("auth code verification failed: %s", data.get("msg"))
                return None
            return {
                "open_id": data["data"]["open_id"],
                "name": data["data"].get("name", ""),
                "access_token": data["data"]["access_token"],
            }
    except Exception as e:
        log.warning("verify_feishu_auth_code error: %s", e)
        return None


def create_token_app(config: VoiceConfig) -> web.Application:
    """Create aiohttp app with /api/jssdk-config and /api/token endpoints."""

    async def handle_jssdk_config(request: web.Request) -> web.Response:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return web.json_response({"error": "Missing url"}, status=400,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        app_token = await get_app_access_token(config.feishu_app_id, config.feishu_app_secret)
        if not app_token:
            return web.json_response({"error": "Failed to get app token"}, status=500,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        ticket = await get_jsapi_ticket(app_token)
        if not ticket:
            return web.json_response({"error": "Failed to get jsapi ticket"}, status=500,
                                     headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        sig = generate_jssdk_signature(ticket, nonce, ts, url)

        return web.json_response(
            {"appId": config.feishu_app_id, "timestamp": ts, "nonceStr": nonce, "signature": sig},
            headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
        )

    async def handle_token(request: web.Request) -> web.Response:
        body = await request.json()
        auth_code = body.get("auth_code", "")

        if not auth_code:
            return web.json_response(
                {"error": "Missing auth_code. Please open from Feishu."},
                status=403, headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
            )

        user_info = await verify_feishu_auth_code(
            auth_code, config.feishu_app_id, config.feishu_app_secret,
        )
        if user_info is None:
            return web.json_response(
                {"error": "Invalid or expired auth code."},
                status=403, headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
            )

        open_id = user_info["open_id"]
        name = user_info.get("name", open_id)
        room = f"voice-{open_id}"

        from livekit.api import AccessToken, VideoGrants  # noqa: PLC0415
        token = AccessToken(api_key=config.livekit_api_key, api_secret=config.livekit_api_secret)
        token.identity = open_id
        token.name = name
        token.add_grant(VideoGrants(room_join=True, room=room))

        return web.json_response(
            {"token": token.to_jwt(), "room": room, "user": name},
            headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
        )

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(status=200, headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    app = web.Application()
    app.router.add_post("/api/jssdk-config", handle_jssdk_config)
    app.router.add_route("OPTIONS", "/api/jssdk-config", handle_options)
    app.router.add_post("/api/token", handle_token)
    app.router.add_route("OPTIONS", "/api/token", handle_options)
    return app


async def run_token_server(config: VoiceConfig) -> web.AppRunner:
    """Start the token server. Returns runner for lifecycle management."""
    app = create_token_app(config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.token_port)
    await site.start()
    log.info("Token server on %s:%d", config.host, config.token_port)
    return runner
