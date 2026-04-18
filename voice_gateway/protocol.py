"""
Doubao E2E binary protocol encode/decode.
Adapted from realtime_dialog_example/python3.7/protocol.py with bug fixes.
"""
import gzip
import json
from typing import Any

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message types
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Flags
NO_SEQUENCE = 0b0000
NEG_SEQUENCE = 0b0010
MSG_WITH_EVENT = 0b0100

# Serialization
NO_SERIALIZATION = 0b0000
JSON_SERIAL = 0b0001

# Compression
NO_COMPRESSION = 0b0000
GZIP_COMPRESSION = 0b0001

# Client event IDs
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200
EVENT_SAY_HELLO = 300
EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CHAT_RAG_TEXT = 502
EVENT_CLIENT_INTERRUPT = 515

# Server event IDs
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_USAGE_RESPONSE = 154
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559

# Connection-level events (no session_id in frame)
CONNECTION_EVENTS = {EVENT_START_CONNECTION, EVENT_FINISH_CONNECTION}


def generate_header(
    message_type=CLIENT_FULL_REQUEST,
    serial_method=JSON_SERIAL,
    compression_type=GZIP_COMPRESSION,
) -> bytearray:
    header = bytearray(4)
    header[0] = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    header[1] = (message_type << 4) | MSG_WITH_EVENT
    header[2] = (serial_method << 4) | compression_type
    header[3] = 0x00
    return header


def build_client_frame(
    event_id: int,
    session_id: str | None = None,
    payload: Any = None,
    is_audio: bool = False,
) -> bytes:
    msg_type = CLIENT_AUDIO_ONLY_REQUEST if is_audio else CLIENT_FULL_REQUEST
    serial = NO_SERIALIZATION if is_audio else JSON_SERIAL

    buf = bytearray(generate_header(msg_type, serial, GZIP_COMPRESSION))

    # Event ID (4 bytes big-endian)
    buf.extend(event_id.to_bytes(4, "big"))

    # Session ID (only for session-level events)
    if event_id not in CONNECTION_EVENTS and session_id is not None:
        sid_bytes = session_id.encode()
        buf.extend(len(sid_bytes).to_bytes(4, "big"))
        buf.extend(sid_bytes)

    # Payload — always gzip compressed
    if is_audio and isinstance(payload, (bytes, bytearray)):
        compressed = gzip.compress(payload)
    else:
        compressed = gzip.compress(json.dumps(payload or {}).encode())
    buf.extend(len(compressed).to_bytes(4, "big"))
    buf.extend(compressed)

    return bytes(buf)


def parse_server_frame(data: bytes) -> dict:
    if len(data) < 4:
        return {}

    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F

    cursor = header_size * 4
    result: dict[str, Any] = {}

    if message_type in (SERVER_FULL_RESPONSE, SERVER_ACK):
        result["message_type"] = "SERVER_ACK" if message_type == SERVER_ACK else "SERVER_FULL_RESPONSE"

        # FIXED: use advancing cursor (official code has bug here)
        if flags & NEG_SEQUENCE:
            result["seq"] = int.from_bytes(data[cursor:cursor + 4], "big")
            cursor += 4
        if flags & MSG_WITH_EVENT:
            result["event"] = int.from_bytes(data[cursor:cursor + 4], "big")
            cursor += 4

        # Session ID
        sid_len = int.from_bytes(data[cursor:cursor + 4], "big", signed=True)
        cursor += 4
        if sid_len > 0:
            result["session_id"] = data[cursor:cursor + sid_len].decode()
            cursor += sid_len

        # Payload
        payload_size = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_msg = data[cursor:cursor + payload_size]

        if compression == GZIP_COMPRESSION and payload_size > 0:
            payload_msg = gzip.decompress(payload_msg)
        if serialization == JSON_SERIAL and payload_size > 0:
            payload_msg = json.loads(payload_msg.decode("utf-8"))

        result["payload_msg"] = payload_msg
        result["payload_size"] = payload_size

    elif message_type == SERVER_ERROR_RESPONSE:
        result["message_type"] = "SERVER_ERROR"
        result["code"] = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_size = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        payload_msg = data[cursor:cursor + payload_size]

        if compression == GZIP_COMPRESSION and payload_size > 0:
            payload_msg = gzip.decompress(payload_msg)
        if serialization == JSON_SERIAL and payload_size > 0:
            payload_msg = json.loads(payload_msg.decode("utf-8"))

        result["payload_msg"] = payload_msg
        result["payload_size"] = payload_size

    return result
