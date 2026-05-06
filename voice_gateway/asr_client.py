"""ASR client — Doubao E2E adapter.

Uses the Doubao E2E realtime dialogue API (DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN),
but only exposes the ASR events as a normalized stream. Doubao's LLM and TTS
events emitted on the same connection are simply ignored — we're using the
E2E protocol as a transport for ASR only.

Event stream yielded by receive():
  {"type": "conversation.item.input_audio_transcription.result", "transcript": str}  # interim
  {"type": "conversation.item.input_audio_transcription.completed", "transcript": str}  # final
  {"type": "input_audio_buffer.speech_started"}
  {"type": "error", "error": {"message": str}}
"""
import logging
import uuid
from typing import AsyncGenerator

from config import START_SESSION_CONFIG
from doubao_client import DoubaoClient
from protocol import (
    EVENT_ASR_ENDED,
    EVENT_ASR_INFO,
    EVENT_ASR_RESPONSE,
    EVENT_CONNECTION_FAILED,
    EVENT_CONNECTION_STARTED,
    EVENT_SESSION_FAILED,
    EVENT_SESSION_STARTED,
)

log = logging.getLogger(__name__)


class ASRClient:
    def __init__(self):
        self.session_id = str(uuid.uuid4())
        self._doubao = DoubaoClient(self.session_id)
        self._receiver = None  # async generator of parsed Doubao frames
        self._last_transcript = ""

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
        log.info("ASR ready (Doubao E2E, session=%s)", self.session_id[:8])

    async def _wait_for(self, target_event: int, error_events: tuple = ()) -> dict:
        if self._receiver is None:
            raise RuntimeError("ASRClient not connected")
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

    async def send_audio(self, pcm_bytes: bytes) -> None:
        await self._doubao.send_audio(pcm_bytes)

    async def commit(self) -> None:
        # Doubao's E2E API uses server-side VAD — no explicit commit needed.
        # Method kept to preserve the old Volcengine-AI-Gateway interface.
        pass

    async def receive(self) -> AsyncGenerator[dict, None]:
        """Yield normalized ASR events (not raw Doubao frames)."""
        if self._receiver is None:
            return
        async for frame in self._receiver:
            event = frame.get("event")
            payload = frame.get("payload_msg")

            if event == EVENT_ASR_RESPONSE and isinstance(payload, dict):
                results = payload.get("results", [])
                if results:
                    last = results[-1]
                    text = last.get("text", "")
                    is_interim = bool(last.get("is_interim", True))
                    if text:
                        if is_interim:
                            yield {
                                "type": "conversation.item.input_audio_transcription.result",
                                "transcript": text,
                            }
                        else:
                            # Final transcript — cache for ASR_ENDED
                            self._last_transcript = text

            elif event == EVENT_ASR_INFO:
                yield {"type": "input_audio_buffer.speech_started"}

            elif event == EVENT_ASR_ENDED:
                if self._last_transcript:
                    yield {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": self._last_transcript,
                    }
                    self._last_transcript = ""

            elif frame.get("message_type") == "SERVER_ERROR":
                msg = frame.get("payload_msg", f"code {frame.get('code')}")
                yield {"type": "error", "error": {"message": str(msg)}}

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
