"""Voice agent configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class VoiceConfig:
    """Configuration for voice agent and token server."""

    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # Deepgram STT
    deepgram_api_key: str

    # FishAudio TTS
    fish_api_key: str
    fish_model_id: str = "default"

    # Feishu app (for JSSDK config + auth code verification)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Network
    host: str = "0.0.0.0"
    token_port: int = 8089
    language: str = "zh"

    @classmethod
    def from_env(cls) -> VoiceConfig:
        """Load from environment. Raises ValueError for missing required vars."""
        required = [
            "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
            "DEEPGRAM_API_KEY", "FISH_API_KEY",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")

        return cls(
            livekit_url=os.environ["LIVEKIT_URL"],
            livekit_api_key=os.environ["LIVEKIT_API_KEY"],
            livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
            deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
            fish_api_key=os.environ["FISH_API_KEY"],
            fish_model_id=os.environ.get("FISH_MODEL_ID", "default"),
            feishu_app_id=os.environ.get("FEISHU_APP_ID", ""),
            feishu_app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
            host=os.environ.get("VOICE_HOST", "0.0.0.0"),
            token_port=int(os.environ.get("TOKEN_PORT", "8089")),
            language=os.environ.get("VOICE_LANGUAGE", "zh"),
        )
