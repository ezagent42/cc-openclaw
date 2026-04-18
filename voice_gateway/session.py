"""Voice call session state machine."""
import asyncio
import copy
import json
import logging
import uuid
from typing import Any

import aiohttp
import aiohttp.web

from config import START_SESSION_CONFIG, GREETING_TEXT, COMFORT_TEXT
from doubao_client import DoubaoClient
from protocol import (
    EVENT_CONNECTION_STARTED, EVENT_CONNECTION_FAILED,
    EVENT_SESSION_STARTED, EVENT_SESSION_FAILED, EVENT_SESSION_FINISHED,
    EVENT_CONNECTION_FINISHED,
    EVENT_TTS_SENTENCE_START, EVENT_TTS_SENTENCE_END,
    EVENT_TTS_RESPONSE, EVENT_TTS_ENDED,
    EVENT_USAGE_RESPONSE,
    EVENT_ASR_INFO, EVENT_ASR_RESPONSE, EVENT_ASR_ENDED,
    EVENT_CHAT_RESPONSE, EVENT_CHAT_ENDED,
)
from actor_bridge import ActorBridge
from config import CHANNEL_SERVER_WS_URL, VOICE_INSTANCE_PREFIX

log = logging.getLogger(__name__)


class Session:
    def __init__(self, browser_ws: aiohttp.web.WebSocketResponse, start_config: dict | None = None):
        self.browser_ws = browser_ws
        self.session_id = str(uuid.uuid4())
        self.doubao = DoubaoClient(self.session_id)
        self.state = "idle"
        self.is_user_querying = False
        self.is_sending_custom_tts = False
        self.asr_text = ""
        self.bot_text_accumulator = ""
        self._query_task: asyncio.Task | None = None
        self._doubao_task: asyncio.Task | None = None
        self._event_waiters: dict[int, asyncio.Event] = {}
        self._last_waited_frame: dict = {}
        self._bridge = ActorBridge()

        # Apply custom config from browser
        start_config = start_config or {}
        self._session_config = copy.deepcopy(START_SESSION_CONFIG)
        if start_config.get("systemRole"):
            self._session_config["dialog"]["system_role"] = start_config["systemRole"]
            log.info(f"Custom system_role: {start_config['systemRole'][:80]}...")
        self._greeting_text = start_config.get("greeting") or GREETING_TEXT
        self._comfort_text = start_config.get("comfortText") or COMFORT_TEXT

    async def send_state(self, state: str) -> None:
        self.state = state
        await self.browser_ws.send_json({"type": "state", "state": state})

    async def send_transcript(self, role: str, text: str, interim: bool) -> None:
        if not text:
            return
        await self.browser_ws.send_json({
            "type": "transcript", "role": role, "text": text, "interim": interim,
        })

    async def send_error(self, message: str) -> None:
        await self.browser_ws.send_json({"type": "error", "message": message})

    async def run(self) -> None:
        """Run the full call lifecycle. Called from server.py after browser sends 'start'."""
        try:
            # Connect to actor model (CC session for LLM)
            voice_id = f"{VOICE_INSTANCE_PREFIX}.{self.session_id[:8]}"
            await self._bridge.connect(CHANNEL_SERVER_WS_URL, voice_id)

            await self.doubao.connect()
            self._doubao_task = asyncio.create_task(self._read_doubao())

            await self._connect_and_greet()
            if self.state == "talking":
                await self._talking_loop()
        except Exception as e:
            log.exception("Session error")
            await self.send_error(str(e))
        finally:
            await self._cleanup()

    # --- Internal flows ---

    async def _connect_and_greet(self) -> None:
        log.info("Starting connect_and_greet flow")
        await self.send_state("connecting")

        await self.doubao.send_start_connection()
        frame = await self._wait_for_event(EVENT_CONNECTION_STARTED,
                                            error_event=EVENT_CONNECTION_FAILED, timeout=10.0)
        if frame.get("event") == EVENT_CONNECTION_FAILED:
            raise ConnectionError(f"ConnectionFailed: {frame.get('payload_msg')}")

        await self.doubao.send_start_session(self._session_config)
        frame = await self._wait_for_event(EVENT_SESSION_STARTED,
                                            error_event=EVENT_SESSION_FAILED, timeout=10.0)
        if frame.get("event") == EVENT_SESSION_FAILED:
            raise ConnectionError(f"SessionFailed: {frame.get('payload_msg')}")

        await self.send_state("greeting")
        await self.doubao.send_say_hello(self._greeting_text)

        # Wait for greeting TTS to finish
        await self._wait_for_event(EVENT_TTS_ENDED, timeout=30.0)
        await self.send_state("talking")

    async def _talking_loop(self) -> None:
        """Read browser messages: binary audio or JSON control."""
        async for msg in self.browser_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self.doubao.send_audio(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "stop":
                    await self._stop()
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    async def _stop(self) -> None:
        """Graceful stop."""
        await self.send_state("ending")
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        try:
            await self.doubao.send_finish_session()
            await self._wait_for_event(EVENT_SESSION_FINISHED, timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            await self.doubao.send_finish_connection()
            await self._wait_for_event(EVENT_CONNECTION_FINISHED, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        await self.send_state("idle")

    # --- Single doubao consumer ---

    async def _read_doubao(self) -> None:
        """Single consumer of all doubao messages. Dispatches events and resolves waiters."""
        try:
            async for frame in self.doubao.receive():
                event = frame.get("event")

                if event in self._event_waiters:
                    self._last_waited_frame = frame
                    self._event_waiters[event].set()

                await self._dispatch_doubao_event(frame)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Doubao read error: {e}")

    async def _wait_for_event(self, target_event: int, timeout: float = 10.0,
                               error_event: int | None = None) -> dict:
        """Wait for a specific event from doubao. Uses asyncio.Event resolved by _read_doubao."""
        evt = asyncio.Event()
        self._event_waiters[target_event] = evt
        if error_event:
            self._event_waiters[error_event] = evt

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        finally:
            self._event_waiters.pop(target_event, None)
            if error_event:
                self._event_waiters.pop(error_event, None)

        return self._last_waited_frame

    # --- Event dispatch ---

    async def _dispatch_doubao_event(self, frame: dict) -> None:
        event = frame.get("event")
        payload = frame.get("payload_msg")
        msg_type = frame.get("message_type")

        # Log all non-audio events for debugging
        if event != EVENT_TTS_RESPONSE:
            log.info(f"doubao event={event} msg_type={msg_type} payload={str(payload)[:200] if payload else None}")

        # TTS audio → forward to browser
        if event == EVENT_TTS_RESPONSE and isinstance(payload, bytes):
            if not self.is_sending_custom_tts:
                await self.browser_ws.send_bytes(payload)
            return

        # TTS sentence start → track tts_type
        if event == EVENT_TTS_SENTENCE_START and isinstance(payload, dict):
            tts_type = payload.get("tts_type", "")
            if tts_type in ("chat_tts_text", "external_rag"):
                self.is_sending_custom_tts = False
                if payload.get("text"):
                    await self.send_transcript("bot", payload["text"], False)
            return

        # ASR text
        if event == EVENT_ASR_RESPONSE and isinstance(payload, dict):
            results = payload.get("results", [])
            if results:
                last = results[-1]
                text = last.get("text", "")
                is_interim = last.get("is_interim", True)
                if not is_interim:
                    self.asr_text = text
                await self.send_transcript("user", text, is_interim)
            return

        # ASR info → user interruption
        if event == EVENT_ASR_INFO:
            self.is_user_querying = True
            self.is_sending_custom_tts = False
            if self._query_task and not self._query_task.done():
                self._query_task.cancel()
            await self.browser_ws.send_json({"type": "clear_audio"})
            return

        # ASR ended → trigger query
        if event == EVENT_ASR_ENDED:
            self.is_user_querying = False
            if self.asr_text:
                self._query_task = asyncio.create_task(self._run_query(self.asr_text))
                self.asr_text = ""
            return

        # ChatResponse → accumulate bot text (suppress during custom TTS)
        if event == EVENT_CHAT_RESPONSE and isinstance(payload, dict):
            if self.is_sending_custom_tts:
                return
            token = payload.get("content", "")
            if token:
                self.bot_text_accumulator += token
                await self.send_transcript("bot", self.bot_text_accumulator, True)
            return

        # ChatEnded → finalize bot text (suppress during custom TTS)
        if event == EVENT_CHAT_ENDED:
            if self.is_sending_custom_tts:
                self.bot_text_accumulator = ""
                return
            if self.bot_text_accumulator:
                await self.send_transcript("bot", self.bot_text_accumulator, False)
            self.bot_text_accumulator = ""
            return

        # Error events
        if event in (EVENT_CONNECTION_FAILED, EVENT_SESSION_FAILED):
            error_msg = payload.get("error", f"Event {event} failed") if isinstance(payload, dict) else str(payload)
            await self.send_error(error_msg)
            return

        if msg_type == "SERVER_ERROR":
            await self.send_error(f"Server error {frame.get('code')}: {payload}")
            return

    # --- Query orchestration ---

    async def _run_query(self, text: str) -> None:
        """Comfort text FIRST (suppress E2E default), then CC session via bridge, then RAG."""
        try:
            # Send comfort text IMMEDIATELY to suppress E2E's default LLM response
            self.is_sending_custom_tts = True
            await self.doubao.send_chat_tts_text(self._comfort_text, start=True, end=False)
            await self.doubao.send_chat_tts_text("", start=False, end=True)
            log.info(f"Comfort text sent, querying CC session for: {text}")

            if self.is_user_querying:
                self.is_sending_custom_tts = False
                return

            # Query CC session via actor model (may take 5-30s)
            result = await self._bridge.query(text, timeout=60.0)

            if self.is_user_querying:
                self.is_sending_custom_tts = False
                return

            await self.doubao.send_chat_rag_text(result)
            log.info(f"RAG injected for: {text}")

        except asyncio.CancelledError:
            self.is_sending_custom_tts = False
            log.info("Query cancelled (user interrupted)")
        except Exception as e:
            self.is_sending_custom_tts = False
            log.error(f"Query error: {e}")

    async def _cleanup(self) -> None:
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        if self._doubao_task and not self._doubao_task.done():
            self._doubao_task.cancel()
            try:
                await self._doubao_task
            except asyncio.CancelledError:
                pass
        try:
            await self.doubao.close()
        except Exception:
            pass
        try:
            await self._bridge.close()
        except Exception:
            pass
