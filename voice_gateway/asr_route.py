"""Stateless ASR WebSocket handler — proxies browser PCM → Volcengine ASR → text frames back."""
import json
import logging

import aiohttp.web

from asr_client import ASRClient

log = logging.getLogger(__name__)


# Volcengine ASR event types we care about
_EV_PARTIAL = "conversation.item.input_audio_transcription.result"
_EV_FINAL = "conversation.item.input_audio_transcription.completed"
_EV_SPEECH_STARTED = "input_audio_buffer.speech_started"


async def asr_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    log.info("[/asr] browser connected")

    asr = ASRClient()
    try:
        # Expect first message to be {"type":"start"}
        first = await ws.receive()
        if first.type != aiohttp.WSMsgType.TEXT:
            await ws.send_json({"type": "error", "message": "expected start frame"})
            return ws
        try:
            data = json.loads(first.data)
        except json.JSONDecodeError:
            await ws.send_json({"type": "error", "message": "invalid start frame"})
            return ws
        if data.get("type") != "start":
            await ws.send_json({"type": "error", "message": "first frame must be type=start"})
            return ws

        await asr.connect()

        import asyncio
        reader_task = asyncio.create_task(_forward_asr_events(asr, ws))
        browser_task = asyncio.create_task(_forward_browser_audio(ws, asr))

        done, pending = await asyncio.wait(
            {reader_task, browser_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception as e:
        log.exception("[/asr] error")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            await asr.close()
        except Exception:
            pass
        log.info("[/asr] browser disconnected")

    return ws


async def _forward_asr_events(asr: ASRClient, ws: aiohttp.web.WebSocketResponse) -> None:
    async for event in asr.receive():
        evt_type = event.get("type", "")
        text = event.get("transcript", "")

        if evt_type == _EV_PARTIAL and text:
            await ws.send_json({"type": "partial", "text": text})
        elif evt_type == _EV_FINAL and text:
            await ws.send_json({"type": "final", "text": text})
        elif evt_type == _EV_SPEECH_STARTED:
            await ws.send_json({"type": "speech_started"})
        elif evt_type.startswith("error") or "error" in event:
            await ws.send_json({"type": "error", "message": event.get("error", {}).get("message", "ASR error")})


async def _forward_browser_audio(ws: aiohttp.web.WebSocketResponse, asr: ASRClient) -> None:
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            await asr.send_audio(msg.data)
        elif msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "stop":
                break
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break
