"""Split mode session: ASR → pseudo_llm → TTS (no E2E dialog)."""
import asyncio
import json
import logging

import aiohttp
import aiohttp.web

from config import GREETING_TEXT, COMFORT_TEXT
from asr_client import ASRClient
from tts_client import TTSClient
from actor_bridge import ActorBridge
from config import CHANNEL_SERVER_WS_URL, VOICE_INSTANCE_PREFIX

log = logging.getLogger(__name__)


class SplitSession:
    def __init__(self, browser_ws: aiohttp.web.WebSocketResponse, start_config: dict | None = None):
        self.browser_ws = browser_ws
        self.asr = ASRClient()
        self.tts = TTSClient()
        self.state = "idle"
        self.asr_text = ""
        self._asr_task: asyncio.Task | None = None
        self._query_task: asyncio.Task | None = None
        self._bridge = ActorBridge()

        start_config = start_config or {}
        self._greeting_text = start_config.get("greeting") or GREETING_TEXT
        self._comfort_text = start_config.get("comfortText") or COMFORT_TEXT
        self._system_role = start_config.get("systemRole") or ""

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
        try:
            await self.send_state("connecting")
            import uuid
            voice_id = f"{VOICE_INSTANCE_PREFIX}.split-{uuid.uuid4().hex[:8]}"
            await self._bridge.connect(CHANNEL_SERVER_WS_URL, voice_id)
            await self.asr.connect()
            await self.tts.connect()

            # Greeting via TTS
            await self.send_state("greeting")
            await self._speak(self._greeting_text)
            await self.send_transcript("bot", self._greeting_text, False)

            # Start ASR reader
            await self.send_state("talking")
            self._asr_task = asyncio.create_task(self._read_asr())

            # Main loop: read browser audio
            await self._browser_loop()
        except Exception as e:
            log.exception("SplitSession error")
            await self.send_error(str(e))
        finally:
            await self._cleanup()

    async def _speak(self, text: str) -> None:
        """Synthesize text and send PCM audio to browser."""
        async for audio_chunk in self.tts.synthesize(text):
            await self.browser_ws.send_bytes(audio_chunk)

    async def _read_asr(self) -> None:
        """Single consumer of ASR events."""
        try:
            async for event in self.asr.receive():
                evt_type = event.get("type", "")

                if evt_type == "conversation.item.input_audio_transcription.result":
                    text = event.get("transcript", "")
                    if text:
                        self.asr_text = text
                        await self.send_transcript("user", text, True)

                elif evt_type == "conversation.item.input_audio_transcription.completed":
                    text = event.get("transcript", "")
                    if text:
                        self.asr_text = text
                        await self.send_transcript("user", text, False)
                        # Trigger LLM + TTS
                        if self._query_task and not self._query_task.done():
                            self._query_task.cancel()
                        self._query_task = asyncio.create_task(self._run_query(text))
                        self.asr_text = ""

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"ASR read error: {e}")

    async def _browser_loop(self) -> None:
        """Read browser messages: binary audio or JSON control."""
        async for msg in self.browser_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self.asr.send_audio(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "stop":
                    await self._stop()
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    async def _run_query(self, text: str) -> None:
        """CC session via bridge → TTS pipeline."""
        try:
            log.info(f"Split query: '{text}'")
            result = await self._bridge.query(text, timeout=60.0)
            log.info(f"Split CC result: {result[:100]}")

            # Send LLM response as bot transcript
            await self.send_transcript("bot", result, False)

            # Speak the result via TTS
            await self._speak(result)

        except asyncio.CancelledError:
            log.info("Split query cancelled")
        except Exception as e:
            log.error(f"Split query error: {e}")

    async def _stop(self) -> None:
        await self.send_state("ending")
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        await self.send_state("idle")

    async def _cleanup(self) -> None:
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
        if self._asr_task and not self._asr_task.done():
            self._asr_task.cancel()
            try:
                await self._asr_task
            except asyncio.CancelledError:
                pass
        try:
            await self.asr.close()
        except Exception:
            pass
        try:
            await self.tts.close()
        except Exception:
            pass
        try:
            await self._bridge.close()
        except Exception:
            pass
