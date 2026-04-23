# tests/test_tts_route.py
import pytest
import json
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tts_route import tts_handler


class FakeTTSClient:
    def __init__(self):
        self.connected = False

    async def connect(self):
        self.connected = True

    async def synthesize(self, text: str):
        # Yield two small PCM chunks then stop
        yield b"\x00\x01" * 100
        yield b"\x02\x03" * 100

    async def close(self):
        self.connected = False


@pytest.mark.asyncio
async def test_tts_route_streams_audio_then_done():
    with patch("tts_route.TTSClient", return_value=FakeTTSClient()):
        app = web.Application()
        app.router.add_get("/tts", tts_handler)

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/tts") as ws:
                await ws.send_json({"type": "speak", "text": "hi"})

                binary_chunks = 0
                got_done = False
                for _ in range(10):
                    msg = await ws.receive(timeout=2.0)
                    if msg.type.name == "BINARY":
                        binary_chunks += 1
                    elif msg.type.name == "TEXT":
                        data = json.loads(msg.data)
                        if data.get("type") == "done":
                            got_done = True
                            break

                assert binary_chunks == 2
                assert got_done
