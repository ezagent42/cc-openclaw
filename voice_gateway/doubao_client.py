"""High-level async client for doubao E2E realtime voice API."""
import json
import logging
from typing import AsyncGenerator

import websockets

from config import DOUBAO_WS_URL, get_ws_headers
from protocol import (
    build_client_frame, parse_server_frame,
    EVENT_START_CONNECTION, EVENT_FINISH_CONNECTION,
    EVENT_START_SESSION, EVENT_FINISH_SESSION,
    EVENT_TASK_REQUEST, EVENT_SAY_HELLO,
    EVENT_CHAT_TTS_TEXT, EVENT_CHAT_RAG_TEXT,
)

log = logging.getLogger(__name__)


class DoubaoClient:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.ws = None
        self.logid = ""

    async def connect(self) -> str:
        headers = get_ws_headers()
        self.ws = await websockets.connect(
            DOUBAO_WS_URL,
            additional_headers=headers,
            ping_interval=None,
        )
        log.info("Connected to doubao")
        return ""

    async def send_start_connection(self) -> None:
        frame = build_client_frame(EVENT_START_CONNECTION, payload={})
        await self.ws.send(frame)

    async def send_start_session(self, config: dict) -> None:
        frame = build_client_frame(EVENT_START_SESSION, self.session_id, config)
        await self.ws.send(frame)

    async def send_say_hello(self, text: str) -> None:
        frame = build_client_frame(EVENT_SAY_HELLO, self.session_id, {"content": text})
        await self.ws.send(frame)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        frame = build_client_frame(EVENT_TASK_REQUEST, self.session_id, pcm_bytes, is_audio=True)
        await self.ws.send(frame)

    async def send_chat_tts_text(self, content: str, start: bool = True, end: bool = True) -> None:
        frame = build_client_frame(EVENT_CHAT_TTS_TEXT, self.session_id, {
            "start": start, "content": content, "end": end,
        })
        await self.ws.send(frame)

    async def send_chat_rag_text(self, external_rag: str) -> None:
        frame = build_client_frame(EVENT_CHAT_RAG_TEXT, self.session_id, {
            "external_rag": external_rag,
        })
        await self.ws.send(frame)

    async def send_finish_session(self) -> None:
        frame = build_client_frame(EVENT_FINISH_SESSION, self.session_id, {})
        await self.ws.send(frame)

    async def send_finish_connection(self) -> None:
        frame = build_client_frame(EVENT_FINISH_CONNECTION, payload={})
        await self.ws.send(frame)

    async def receive(self) -> AsyncGenerator[dict, None]:
        async for message in self.ws:
            if isinstance(message, bytes):
                parsed = parse_server_frame(message)
                if parsed:
                    yield parsed

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            log.info("Doubao WS closed")
