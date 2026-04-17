"""Handler for voice gateway actors. Routes messages between voice gateway and CC session."""
import logging
from channel_server.core.actor import Actor, Message, Action, Send, TransportSend

log = logging.getLogger(__name__)


class VoiceSessionHandler:
    def handle(self, actor: Actor, msg: Message, runtime=None) -> list[Action]:
        if msg.sender == actor.address:
            # From voice gateway: route query to paired CC session
            cc_target = actor.metadata.get("cc_target")
            if not cc_target:
                log.warning("Voice actor %s has no cc_target", actor.address)
                return []
            log.info("Voice %s → CC %s: %s", actor.address, cc_target,
                     msg.payload.get("text", "")[:80])
            return [Send(to=cc_target, message=Message(
                sender=actor.address, payload=msg.payload, metadata=msg.metadata,
            ))]
        else:
            # From CC session (response): forward to voice gateway via transport
            log.info("CC → Voice %s: %s", actor.address,
                     msg.payload.get("text", "")[:80])
            return [TransportSend(payload={"action": "response", **msg.payload})]

    def on_spawn(self, actor: Actor) -> list[Action]:
        return []

    def on_stop(self, actor: Actor) -> list[Action]:
        return []
