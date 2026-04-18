"""WebSocket bridge to channel_server actor model. Replaces pseudo_llm."""
import asyncio
import json
import logging

import websockets

log = logging.getLogger(__name__)


class ActorBridge:
    def __init__(self):
        self.ws = None
        self.address = ""
        self._pending: asyncio.Future | None = None
        self._reader_task: asyncio.Task | None = None

    async def connect(self, url: str, instance_id: str) -> None:
        """Connect to channel_server WS and register as voice actor."""
        self.ws = await websockets.connect(url, additional_headers={}, ping_interval=None)

        await self.ws.send(json.dumps({
            "action": "register",
            "instance_id": instance_id,
            "tag_name": "Voice Gateway",
        }))

        raw = await self.ws.recv()
        msg = json.loads(raw)
        if msg.get("action") != "registered":
            raise ConnectionError(f"Registration failed: {msg}")
        self.address = msg.get("address", "")
        log.info(f"ActorBridge registered as {self.address}")

        self._reader_task = asyncio.create_task(self._read_loop())

    async def query(self, text: str, timeout: float = 60.0) -> str:
        """Send text to CC session via actor model, wait for response."""
        if not self.ws:
            raise RuntimeError("ActorBridge not connected")

        # Cancel any stale pending future
        if self._pending and not self._pending.done():
            self._pending.cancel()

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending = future

        await self.ws.send(json.dumps({"action": "query", "text": text}))
        log.info(f"ActorBridge query sent: {text[:80]}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            log.info(f"ActorBridge response received: {result[:80]}")
            return result
        except asyncio.TimeoutError:
            log.warning(f"ActorBridge query timed out after {timeout}s")
            self._pending = None
            raise

    async def _read_loop(self) -> None:
        """Background reader: resolve pending future on response."""
        try:
            async for raw in self.ws:
                if isinstance(raw, str):
                    msg = json.loads(raw)
                    action = msg.get("action", "")

                    if action in ("response", "message") and self._pending and not self._pending.done():
                        self._pending.set_result(msg.get("text", ""))
                        self._pending = None
                    else:
                        log.debug(f"ActorBridge received: action={action}")
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            log.warning("ActorBridge WS connection closed")
        except Exception as e:
            log.error(f"ActorBridge read error: {e}")

    async def close(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.ws:
            await self.ws.close()
            log.info("ActorBridge closed")
