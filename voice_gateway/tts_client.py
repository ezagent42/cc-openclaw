"""TTS client using Volcengine Realtime API (OpenAI-compatible)."""
import base64
import json
import logging
from typing import AsyncGenerator

import websockets

from config import REALTIME_TTS_URL, REALTIME_TTS_VOICE, REALTIME_TTS_SAMPLE_RATE, get_realtime_headers

log = logging.getLogger(__name__)


class TTSClient:
    def __init__(self):
        self.ws = None

    async def connect(self) -> None:
        headers = get_realtime_headers()
        self.ws = await websockets.connect(
            REALTIME_TTS_URL,
            additional_headers=headers,
            ping_interval=None,
        )
        session_msg = {
            "type": "tts_session.update",
            "session": {
                "voice": REALTIME_TTS_VOICE,
                "output_audio_format": "pcm",
                "output_audio_sample_rate": REALTIME_TTS_SAMPLE_RATE,
                "text_to_speech": {
                    "model": "doubao-tts",
                },
            },
        }
        await self.ws.send(json.dumps(session_msg))

        msg = await self.ws.recv()
        event = json.loads(msg)
        if event.get("type") != "tts_session.updated":
            raise ConnectionError(f"TTS session setup failed: {event}")
        log.info("TTS connected and session configured")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Send text and yield PCM audio chunks as they arrive."""
        await self.ws.send(json.dumps({
            "type": "input_text.append",
            "delta": text,
        }))
        await self.ws.send(json.dumps({"type": "input_text.done"}))

        async for message in self.ws:
            if isinstance(message, str):
                event = json.loads(message)
                if event["type"] == "response.audio.delta":
                    yield base64.b64decode(event["delta"])
                elif event["type"] == "response.audio.done":
                    break

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("TTS WS closed")
