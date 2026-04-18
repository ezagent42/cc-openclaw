import gzip
import json
import pytest
from protocol import (
    generate_header, build_client_frame, parse_server_frame,
    CLIENT_FULL_REQUEST, CLIENT_AUDIO_ONLY_REQUEST,
    SERVER_FULL_RESPONSE, SERVER_ACK, SERVER_ERROR_RESPONSE,
    JSON_SERIAL, NO_SERIALIZATION, GZIP_COMPRESSION, MSG_WITH_EVENT,
    EVENT_START_CONNECTION, EVENT_START_SESSION, EVENT_TASK_REQUEST,
)


def test_build_start_connection_frame_has_no_session_id():
    """Event 1 (StartConnection) should NOT include session_id fields."""
    frame = build_client_frame(EVENT_START_CONNECTION, session_id=None, payload={})
    assert frame[0] == 0x11
    assert (frame[1] >> 4) == CLIENT_FULL_REQUEST
    assert int.from_bytes(frame[4:8], "big") == 1
    payload_size = int.from_bytes(frame[8:12], "big")
    payload = gzip.decompress(frame[12:12 + payload_size])
    assert json.loads(payload) == {}


def test_build_start_session_frame_has_session_id():
    """Event 100 (StartSession) should include session_id fields."""
    sid = "test-session-abc"
    config = {"tts": {"speaker": "test"}}
    frame = build_client_frame(EVENT_START_SESSION, session_id=sid, payload=config)
    assert int.from_bytes(frame[4:8], "big") == 100
    sid_len = int.from_bytes(frame[8:12], "big")
    assert sid_len == len(sid)
    assert frame[12:12 + sid_len].decode() == sid
    offset = 12 + sid_len
    payload_size = int.from_bytes(frame[offset:offset + 4], "big")
    payload = gzip.decompress(frame[offset + 4:offset + 4 + payload_size])
    assert json.loads(payload) == config


def test_build_audio_frame_uses_audio_only_request():
    """Event 200 (TaskRequest) should use CLIENT_AUDIO_ONLY_REQUEST + NO_SERIALIZATION + GZIP."""
    audio = b"\x00\x01\x02\x03"
    frame = build_client_frame(EVENT_TASK_REQUEST, session_id="sid", payload=audio, is_audio=True)
    assert (frame[1] >> 4) == CLIENT_AUDIO_ONLY_REQUEST
    assert (frame[2] >> 4) == NO_SERIALIZATION
    assert (frame[2] & 0x0f) == GZIP_COMPRESSION


def _build_mock_server_frame(msg_type, event_id, session_id, payload_obj, is_audio=False):
    buf = bytearray()
    buf.append(0x11)
    buf.append((msg_type << 4) | MSG_WITH_EVENT)
    if is_audio:
        buf.append((NO_SERIALIZATION << 4) | GZIP_COMPRESSION)
    else:
        buf.append((JSON_SERIAL << 4) | GZIP_COMPRESSION)
    buf.append(0x00)
    buf.extend(event_id.to_bytes(4, "big"))
    sid_bytes = session_id.encode()
    buf.extend(len(sid_bytes).to_bytes(4, "big"))
    buf.extend(sid_bytes)
    if is_audio:
        compressed = gzip.compress(payload_obj)
    else:
        compressed = gzip.compress(json.dumps(payload_obj).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)
    return bytes(buf)


def test_parse_server_full_response():
    data = _build_mock_server_frame(SERVER_FULL_RESPONSE, 451, "s1",
                                     {"results": [{"text": "hello", "is_interim": True}]})
    result = parse_server_frame(data)
    assert result["message_type"] == "SERVER_FULL_RESPONSE"
    assert result["event"] == 451
    assert result["payload_msg"]["results"][0]["text"] == "hello"


def test_parse_server_ack_audio():
    audio = b"\x10\x20\x30\x40"
    data = _build_mock_server_frame(SERVER_ACK, 352, "s1", audio, is_audio=True)
    result = parse_server_frame(data)
    assert result["message_type"] == "SERVER_ACK"
    assert result["event"] == 352
    assert isinstance(result["payload_msg"], bytes)


def test_parse_server_error():
    buf = bytearray()
    buf.append(0x11)
    buf.append((SERVER_ERROR_RESPONSE << 4) | 0)
    buf.append((JSON_SERIAL << 4) | GZIP_COMPRESSION)
    buf.append(0x00)
    buf.extend((1001).to_bytes(4, "big"))
    compressed = gzip.compress(json.dumps({"error": "bad"}).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)
    result = parse_server_frame(bytes(buf))
    assert result["message_type"] == "SERVER_ERROR"
    assert result["code"] == 1001
    assert result["payload_msg"]["error"] == "bad"
