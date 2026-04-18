"""ASR client using Volcengine Realtime API (OpenAI-compatible)."""
import asyncio
import base64
import json
import logging
from typing import AsyncGenerator

import websockets

from config import REALTIME_ASR_URL, get_realtime_headers

log = logging.getLogger(__name__)


class ASRClient:
    def __init__(self):
        self.ws = None

    async def connect(self) -> None:
        headers = get_realtime_headers()
        self.ws = await websockets.connect(
            REALTIME_ASR_URL,
            additional_headers=headers,
            ping_interval=None,
        )
        session_msg = {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm",
                "input_audio_codec": "raw",
                "input_audio_sample_rate": 16000,
                "input_audio_bits": 16,
                "input_audio_channel": 1,
                "input_audio_transcription": {
                    "model": "bigmodel",
                },
            },
        }
        await self.ws.send(json.dumps(session_msg))

        msg = await self.ws.recv()
        event = json.loads(msg)
        if event.get("type") != "transcription_session.updated":
            raise ConnectionError(f"ASR session setup failed: {event}")
        log.info("ASR connected and session configured")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send raw PCM audio chunk (Base64-encoded for Realtime API)."""
        b64 = base64.b64encode(pcm_bytes).decode("ascii")
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64,
        }))

    async def commit(self) -> None:
        """Signal end of audio input for current utterance."""
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def receive(self) -> AsyncGenerator[dict, None]:
        """Yield ASR events."""
        async for message in self.ws:
            if isinstance(message, str):
                event = json.loads(message)
                yield event

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("ASR WS closed")
