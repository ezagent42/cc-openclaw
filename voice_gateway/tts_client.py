"""TTS client — Doubao E2E adapter.

Uses the Doubao E2E realtime dialogue API (DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN)
as a transport to directly synthesize a given text via the `chat_tts_text`
event (which bypasses Doubao's LLM and makes it speak the provided text
directly). Audio chunks are yielded as they arrive from the server.
"""
import logging
import uuid
from typing import AsyncGenerator

from config import START_SESSION_CONFIG
from doubao_client import DoubaoClient
from protocol import (
    EVENT_CONNECTION_FAILED,
    EVENT_CONNECTION_STARTED,
    EVENT_SESSION_FAILED,
    EVENT_SESSION_STARTED,
    EVENT_TTS_ENDED,
    EVENT_TTS_RESPONSE,
    EVENT_TTS_SENTENCE_START,
)

log = logging.getLogger(__name__)


class TTSClient:
    def __init__(self):
        self.session_id = str(uuid.uuid4())
        self._doubao = DoubaoClient(self.session_id)
        self._receiver = None  # async generator of parsed Doubao frames
        self._first_synthesis = True

    async def connect(self) -> None:
        await self._doubao.connect()
        self._receiver = self._doubao.receive()

        await self._doubao.send_start_connection()
        await self._wait_for(
            EVENT_CONNECTION_STARTED,
            error_events=(EVENT_CONNECTION_FAILED,),
        )
        await self._doubao.send_start_session(START_SESSION_CONFIG)
        await self._wait_for(
            EVENT_SESSION_STARTED,
            error_events=(EVENT_SESSION_FAILED,),
        )
        log.info("TTS ready (Doubao E2E, session=%s)", self.session_id[:8])

    async def _wait_for(self, target_event: int, error_events: tuple = ()) -> dict:
        if self._receiver is None:
            raise RuntimeError("TTSClient not connected")
        async for frame in self._receiver:
            ev = frame.get("event")
            if ev == target_event:
                return frame
            if ev in error_events:
                raise RuntimeError(
                    f"Doubao rejected: event={ev} payload={frame.get('payload_msg')}"
                )
            if frame.get("message_type") == "SERVER_ERROR":
                raise RuntimeError(
                    f"Doubao server error {frame.get('code')}: {frame.get('payload_msg')}"
                )
        raise RuntimeError("Doubao stream ended before event %d arrived" % target_event)

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Ask Doubao to speak `text` (direct TTS, not via LLM). Yield PCM chunks.

        - First synthesis in a session: use `say_hello` (the session's primary
          TTS trigger that Doubao reliably responds to).
        - Subsequent syntheses: use `chat_tts_text` which injects mid-conversation.
        Either way, we yield every TTS_RESPONSE audio frame until TTS_ENDED.
        """
        if self._receiver is None:
            raise RuntimeError("TTSClient not connected")

        if self._first_synthesis:
            await self._doubao.send_say_hello(text)
            self._first_synthesis = False
        else:
            await self._doubao.send_chat_tts_text(text, start=True, end=False)
            await self._doubao.send_chat_tts_text("", start=False, end=True)

        async for frame in self._receiver:
            event = frame.get("event")
            payload = frame.get("payload_msg")

            if event == EVENT_TTS_RESPONSE and isinstance(payload, bytes):
                yield payload

            elif event == EVENT_TTS_ENDED:
                break

            elif frame.get("message_type") == "SERVER_ERROR":
                raise RuntimeError(
                    f"Doubao TTS server error {frame.get('code')}: {payload}"
                )

    async def close(self) -> None:
        try:
            await self._doubao.send_finish_session()
        except Exception:
            pass
        try:
            await self._doubao.send_finish_connection()
        except Exception:
            pass
        await self._doubao.close()
