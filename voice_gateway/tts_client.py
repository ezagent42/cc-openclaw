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
    EVENT_SESSION_FINISHED,
    EVENT_SESSION_STARTED,
    EVENT_TTS_ENDED,
    EVENT_TTS_RESPONSE,
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

    async def _reopen(self) -> None:
        """Close the current Doubao connection and open a fresh one.

        Doubao's `say_hello` event only triggers TTS once per WebSocket
        connection; `chat_tts_text` requires conversational context that's
        absent in our split-mode use case. Workaround: fully reconnect
        between synthesize calls. Cost: ~300-500ms per call.
        """
        try:
            await self._doubao.close()
        except Exception:
            pass
        self.session_id = str(uuid.uuid4())
        self._doubao = DoubaoClient(self.session_id)
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
        log.info("TTS re-opened (Doubao E2E, session=%s)", self.session_id[:8])

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Ask Doubao to speak `text`. Yield PCM chunks.

        Uses `say_hello` (Doubao's reliable TTS trigger). For every synthesis
        after the first, we first reconnect to Doubao — `say_hello` only
        works once per WS connection.
        """
        if self._receiver is None:
            raise RuntimeError("TTSClient not connected")

        if not self._first_synthesis:
            log.info("TTS reconnecting for re-use")
            await self._reopen()
        self._first_synthesis = False

        log.info("TTS synthesize (say_hello): %r", text[:60])
        await self._doubao.send_say_hello(text)

        audio_bytes = 0
        async for frame in self._receiver:
            event = frame.get("event")
            payload = frame.get("payload_msg")

            if event == EVENT_TTS_RESPONSE and isinstance(payload, bytes):
                audio_bytes += len(payload)
                yield payload

            elif event == EVENT_TTS_ENDED:
                log.info("TTS synthesize done (%d bytes)", audio_bytes)
                break

            elif frame.get("message_type") == "SERVER_ERROR":
                raise RuntimeError(
                    f"Doubao TTS server error {frame.get('code')}: {payload}"
                )

            else:
                log.debug("TTS unexpected event %s: %s", event, str(payload)[:120])

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
