# tests/test_asr_route.py
import asyncio
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from asr_route import asr_handler


class FakeASRClient:
    def __init__(self):
        self.connected = False
        self.audio_chunks = []
        self._events = []

    async def connect(self):
        self.connected = True

    async def send_audio(self, data: bytes):
        self.audio_chunks.append(data)

    async def receive(self):
        for ev in self._events:
            yield ev

    async def close(self):
        self.connected = False

    def enqueue(self, event: dict):
        self._events.append(event)


@pytest.mark.asyncio
async def test_asr_route_forwards_final_event_as_simple_frame():
    fake = FakeASRClient()
    fake.enqueue({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "hello world",
    })

    with patch("asr_route.ASRClient", return_value=fake):
        app = web.Application()
        app.router.add_get("/asr", asr_handler)

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/asr") as ws:
                await ws.send_json({"type": "start"})
                msg = await ws.receive(timeout=2.0)
                data = json.loads(msg.data)
                assert data == {"type": "final", "text": "hello world"}
