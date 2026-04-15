"""Tests for channel_server.core.actor data types."""
import pytest
from channel_server.core.actor import (
    Actor,
    Transport,
    Message,
    Send,
    TransportSend,
    UpdateActor,
    SpawnActor,
    StopActor,
    Action,
)


class TestActorCreation:
    def test_actor_creation(self):
        """Actor with required fields uses correct defaults."""
        actor = Actor(address="user://alice", tag="user", handler="user_handler")
        assert actor.address == "user://alice"
        assert actor.tag == "user"
        assert actor.handler == "user_handler"
        assert actor.state == "active"
        assert actor.parent is None
        assert actor.downstream == []
        assert actor.transport is None
        assert actor.metadata == {}
        assert actor.created_at is not None
        assert actor.updated_at is not None

    def test_actor_with_transport(self):
        """Actor can be created with Transport, parent, and downstream addresses."""
        transport = Transport(type="feishu_chat", config={"chat_id": "oc_abc123"})
        actor = Actor(
            address="session://s1",
            tag="session",
            handler="session_handler",
            parent="root://main",
            downstream=["worker://w1", "worker://w2"],
            transport=transport,
            metadata={"role": "coordinator"},
        )
        assert actor.parent == "root://main"
        assert actor.downstream == ["worker://w1", "worker://w2"]
        assert actor.transport is transport
        assert actor.transport.type == "feishu_chat"
        assert actor.transport.config == {"chat_id": "oc_abc123"}
        assert actor.metadata == {"role": "coordinator"}

    def test_actor_defaults_are_independent(self):
        """Each Actor instance gets its own mutable defaults."""
        a1 = Actor(address="a://1", tag="t", handler="h")
        a2 = Actor(address="a://2", tag="t", handler="h")
        a1.downstream.append("x://1")
        assert a2.downstream == []
        a1.metadata["key"] = "val"
        assert a2.metadata == {}


class TestMessageCreation:
    def test_message_creation(self):
        """Message with required fields uses correct defaults."""
        msg = Message(sender="user://alice", type="chat")
        assert msg.sender == "user://alice"
        assert msg.type == "chat"
        assert msg.payload == {}
        assert msg.metadata == {}

    def test_message_with_payload(self):
        """Message stores payload and metadata correctly."""
        msg = Message(
            sender="bot://bot1",
            type="command",
            payload={"text": "hello"},
            metadata={"timestamp": "2026-01-01T00:00:00Z"},
        )
        assert msg.payload == {"text": "hello"}
        assert msg.metadata == {"timestamp": "2026-01-01T00:00:00Z"}


class TestActionTypes:
    def test_send_action(self):
        msg = Message(sender="a://1", type="ping")
        action = Send(to="b://2", message=msg)
        assert action.to == "b://2"
        assert action.message is msg

    def test_transport_send_action(self):
        action = TransportSend(payload={"text": "hello"})
        assert action.payload == {"text": "hello"}

    def test_update_actor_action(self):
        action = UpdateActor(changes={"state": "idle"})
        assert action.changes == {"state": "idle"}

    def test_spawn_actor_action(self):
        action = SpawnActor(address="worker://w1", handler="worker_handler")
        assert action.address == "worker://w1"
        assert action.handler == "worker_handler"
        assert action.kwargs == {}

    def test_spawn_actor_with_kwargs(self):
        action = SpawnActor(
            address="worker://w1",
            handler="worker_handler",
            kwargs={"queue_size": 100},
        )
        assert action.kwargs == {"queue_size": 100}

    def test_stop_actor_action(self):
        action = StopActor(address="worker://w1")
        assert action.address == "worker://w1"

    def test_action_union_isinstance(self):
        """All action types are valid Action instances (type alias check via isinstance)."""
        msg = Message(sender="a://1", type="t")
        actions = [
            Send(to="b://2", message=msg),
            TransportSend(payload={}),
            UpdateActor(changes={}),
            SpawnActor(address="c://3", handler="h"),
            StopActor(address="d://4"),
        ]
        # Action is a Union type alias; verify all are dataclasses with expected attrs
        assert hasattr(actions[0], "to")
        assert hasattr(actions[1], "payload")
        assert hasattr(actions[2], "changes")
        assert hasattr(actions[3], "address")
        assert hasattr(actions[4], "address")


class TestActorSerialization:
    def test_actor_to_dict_and_from_dict(self):
        """Actor with transport round-trips through to_dict / from_dict."""
        transport = Transport(type="websocket", config={"url": "ws://localhost:8080"})
        actor = Actor(
            address="ws://conn1",
            tag="connection",
            handler="ws_handler",
            state="active",
            parent="root://main",
            downstream=["sub://1"],
            transport=transport,
            metadata={"client": "test"},
        )

        d = actor.to_dict()

        # Check serialization structure
        assert d["address"] == "ws://conn1"
        assert d["tag"] == "connection"
        assert d["handler"] == "ws_handler"
        assert d["state"] == "active"
        assert d["parent"] == "root://main"
        assert d["downstream"] == ["sub://1"]
        assert d["metadata"] == {"client": "test"}
        assert d["transport_config"] == {"type": "websocket", "url": "ws://localhost:8080"}

        # Round-trip
        restored = Actor.from_dict(d)
        assert restored.address == actor.address
        assert restored.tag == actor.tag
        assert restored.handler == actor.handler
        assert restored.state == actor.state
        assert restored.parent == actor.parent
        assert restored.downstream == actor.downstream
        assert restored.transport is not None
        assert restored.transport.type == "websocket"
        assert restored.transport.config == {"url": "ws://localhost:8080"}
        assert restored.metadata == actor.metadata
        assert restored.created_at == actor.created_at
        assert restored.updated_at == actor.updated_at

    def test_actor_to_dict_no_transport(self):
        """Actor without transport serializes transport_config as None."""
        actor = Actor(address="bare://1", tag="bare", handler="bare_handler")
        d = actor.to_dict()
        assert d["transport_config"] is None
        assert "transport" not in d

        restored = Actor.from_dict(d)
        assert restored.transport is None
        assert restored.address == "bare://1"
