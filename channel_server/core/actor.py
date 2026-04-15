"""Core actor model data types for the channel server."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Union


@dataclass
class Transport:
    """Describes how an actor receives/sends messages externally."""
    type: str  # "websocket" | "feishu_chat" | "feishu_thread"
    config: dict


@dataclass
class Actor:
    """An actor in the channel server — the fundamental addressable unit."""
    address: str
    tag: str
    handler: str
    state: str = "active"
    parent: str | None = None
    downstream: list[str] = field(default_factory=list)
    transport: Transport | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        """Serialize actor for persistence. Transport stored as flat transport_config dict."""
        if self.transport is not None:
            transport_config: dict | None = {"type": self.transport.type, **self.transport.config}
        else:
            transport_config = None

        return {
            "address": self.address,
            "tag": self.tag,
            "handler": self.handler,
            "state": self.state,
            "parent": self.parent,
            "downstream": list(self.downstream),
            "transport_config": transport_config,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Actor:
        """Restore an Actor from a dict produced by to_dict."""
        transport_config = d.get("transport_config")
        transport: Transport | None = None
        if transport_config is not None:
            config = dict(transport_config)
            transport_type = config.pop("type")
            transport = Transport(type=transport_type, config=config)

        return cls(
            address=d["address"],
            tag=d["tag"],
            handler=d["handler"],
            state=d.get("state", "active"),
            parent=d.get("parent"),
            downstream=list(d.get("downstream", [])),
            transport=transport,
            metadata=dict(d.get("metadata", {})),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )


@dataclass
class Message:
    """A message routed between actors."""
    sender: str
    type: str
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Actions — emitted by handlers to effect state changes or send messages
# ---------------------------------------------------------------------------

@dataclass
class Send:
    """Route a message to another actor by address."""
    to: str
    message: Message


@dataclass
class TransportSend:
    """Send a payload via the actor's external transport."""
    payload: dict


@dataclass
class UpdateActor:
    """Apply a partial update to the current actor's fields."""
    changes: dict


@dataclass
class SpawnActor:
    """Spawn a new child actor."""
    address: str
    handler: str
    kwargs: dict = field(default_factory=dict)


@dataclass
class StopActor:
    """Stop an actor by address."""
    address: str


Action = Union[Send, TransportSend, UpdateActor, SpawnActor, StopActor]
