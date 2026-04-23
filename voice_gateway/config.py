"""Voice gateway configuration. Loads doubao credentials from environment."""
import os
import uuid

DOUBAO_WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"


def get_ws_headers() -> dict:
    return {
        "X-Api-App-ID": os.environ.get("DOUBAO_APP_ID", ""),
        "X-Api-Access-Key": os.environ.get("DOUBAO_ACCESS_TOKEN", ""),
        "X-Api-Resource-Id": "volc.speech.dialog",
        "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }


START_SESSION_CONFIG = {
    "tts": {
        "audio_config": {
            "format": "pcm_s16le",
            "sample_rate": 24000,
            "channel": 1,
        },
        "speaker": "zh_female_vv_jupiter_bigtts",
    },
    "asr": {
        "audio_info": {
            "format": "pcm",
            "sample_rate": 16000,
            "channel": 1,
        },
        "extra": {
            "end_smooth_window_ms": 1500,
        },
    },
    "dialog": {
        "bot_name": "OpenClaw助手",
        "system_role": "你是OpenClaw智能助手。当用户询问商品信息时，请基于提供的知识回答。如果没有相关知识，请如实告知。保持简洁友好。",
        "speaking_style": "语速适中，语调自然，简洁明了。",
        "extra": {
            "input_mod": "keep_alive",
            "recv_timeout": 60,
        },
    },
    "extra": {
        "model": "1.2.1.1",
    },
}

GREETING_TEXT = "你好，请问有什么可以帮你？"
COMFORT_TEXT = "稍等，我帮你查一下。"

# Realtime API (split mode: separate ASR + TTS)
REALTIME_ASR_URL = "wss://ai-gateway.vei.volces.com/v1/realtime?model=bigmodel"
REALTIME_TTS_URL = "wss://ai-gateway.vei.volces.com/v1/realtime?model=doubao-tts"
REALTIME_TTS_VOICE = "zh_female_vv_jupiter_bigtts"
REALTIME_TTS_SAMPLE_RATE = 24000


def get_realtime_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('VOLCENGINE_API_KEY', '')}",
    }


# Channel server connection (actor model bridge)
CHANNEL_SERVER_WS_URL = os.environ.get("CHANNEL_SERVER_WS_URL", "ws://127.0.0.1:8765/ws/cc")
VOICE_INSTANCE_PREFIX = "voice:user"

# CORS allowlist (comma-separated origins) for /asr and /tts browser clients.
# Empty string → disabled (reject all cross-origin). Use "*" only in dev.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]
