"""Standalone LiveKit voice agent — echo mode for validation."""
from __future__ import annotations

import logging

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, UserInputTranscribedEvent, cli
from livekit.plugins import deepgram, fishaudio, silero

from voice.config import VoiceConfig

log = logging.getLogger("voice-agent")


class EchoAgent(Agent):
    """Simple echo agent: repeats back what the user says via TTS (no LLM)."""

    def __init__(self) -> None:
        super().__init__(
            instructions="Echo agent — no LLM required.",
        )

    async def on_enter(self) -> None:
        await self.session.say("你好，我是语音测试助手，请说话")

        @self.session.on("user_input_transcribed")
        def _on_transcript(ev: UserInputTranscribedEvent) -> None:
            if ev.is_final and ev.transcript.strip():
                self.session.say(f"你说了：{ev.transcript}")


def create_server(config: VoiceConfig) -> AgentServer:
    """Create and configure the LiveKit AgentServer."""

    def prewarm(proc: JobProcess) -> None:
        proc.userdata["vad"] = silero.VAD.load()
        proc.userdata["config"] = config

    server = AgentServer(setup_fnc=prewarm)

    @server.rtc_session()
    async def entrypoint(ctx: JobContext) -> None:
        cfg: VoiceConfig = ctx.proc.userdata["config"]

        stt = deepgram.STT(
            api_key=cfg.deepgram_api_key,
            language=cfg.language,
        )
        tts = fishaudio.TTS(
            api_key=cfg.fish_api_key,
            model_id=cfg.fish_model_id,
        )

        session = AgentSession(
            stt=stt,
            tts=tts,
            vad=ctx.proc.userdata["vad"],
        )

        await session.start(agent=EchoAgent(), room=ctx.room)

    return server


def main() -> None:
    """CLI entry point."""
    config = VoiceConfig.from_env()
    server = create_server(config)
    cli.run_app(server)


if __name__ == "__main__":
    main()
