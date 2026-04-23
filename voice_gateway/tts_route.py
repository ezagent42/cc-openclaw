"""Stateless TTS WebSocket handler — accepts speak/abort, streams PCM back."""
import asyncio
import json
import logging

import aiohttp.web

from tts_client import TTSClient

log = logging.getLogger(__name__)


async def _cancel_and_wait(task: asyncio.Task | None) -> None:
    """Cancel a task and await its completion so its cleanup finishes before we proceed.

    Prevents the previous speak's in-flight send_bytes from interleaving with the next
    one's output on the same WebSocket, and swallows the expected CancelledError so it
    doesn't surface in the asyncio log.
    """
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def tts_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    log.info("[/tts] browser connected")

    tts = TTSClient()
    current_task: asyncio.Task | None = None

    try:
        await tts.connect()

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            action = data.get("type")
            if action == "speak":
                text = data.get("text", "")
                await _cancel_and_wait(current_task)
                current_task = asyncio.create_task(_do_speak(tts, ws, text))
            elif action == "abort":
                await _cancel_and_wait(current_task)
    except Exception as e:
        log.exception("[/tts] error")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await _cancel_and_wait(current_task)
        try:
            await tts.close()
        except Exception:
            pass
        log.info("[/tts] browser disconnected")

    return ws


async def _do_speak(tts: TTSClient, ws: aiohttp.web.WebSocketResponse, text: str) -> None:
    try:
        async for chunk in tts.synthesize(text):
            await ws.send_bytes(chunk)
        await ws.send_json({"type": "done"})
    except asyncio.CancelledError:
        log.info("[/tts] speak cancelled")
        raise
    except Exception as e:
        log.error(f"[/tts] speak error: {e}")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
