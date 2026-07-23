"""Tests for FeishuAdapter — inbound routing, dedup, echo prevention, auto-spawn, file download."""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from channel_server.core.actor import Message, Transport
from channel_server.core.runtime import ActorRuntime
from channel_server.adapters.feishu.adapter import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_APP_ID = "test_app"


def make_adapter() -> tuple[FeishuAdapter, ActorRuntime]:
    rt = ActorRuntime()
    client = MagicMock()
    adapter = FeishuAdapter(rt, client)
    adapter.app_id = TEST_APP_ID
    return adapter, rt


def feishu_event(
    chat_id: str = "oc_abc123",
    message_id: str = "msg_001",
    text: str = "hello",
    user: str = "Alice",
    user_id: str = "ou_alice",
    root_id: str | None = None,
    msg_type: str = "text",
    file_path: str = "",
    chat_type: str = "",
) -> dict:
    """Build a minimal Feishu message event dict."""
    evt: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "file_path": file_path,
        "user": user,
        "user_id": user_id,
        "msg_type": msg_type,
        "chat_type": chat_type,
    }
    if root_id is not None:
        evt["root_id"] = root_id
    return evt


# ---------------------------------------------------------------------------
# 1. resolve_actor_address — main chat
# ---------------------------------------------------------------------------

def test_resolve_actor_address_main_chat():
    adapter, _ = make_adapter()
    addr = adapter.resolve_actor_address("oc_abc123", None)
    assert addr == "feishu:test_app:oc_abc123"


# ---------------------------------------------------------------------------
# 2. resolve_actor_address — thread
# ---------------------------------------------------------------------------

def test_resolve_actor_address_thread():
    adapter, _ = make_adapter()
    addr = adapter.resolve_actor_address("oc_abc123", "om_root456")
    assert addr == "feishu:test_app:oc_abc123:om_root456"


# ---------------------------------------------------------------------------
# 3. on_feishu_event auto-spawns actor
# ---------------------------------------------------------------------------

def test_on_feishu_event_auto_spawns_actor():
    adapter, rt = make_adapter()
    evt = feishu_event()
    adapter.on_feishu_event(evt)

    addr = "feishu:test_app:oc_abc123"
    actor = rt.lookup(addr)
    assert actor is not None
    assert actor.handler == "feishu_inbound"
    assert actor.transport is not None
    assert actor.transport.type == "feishu_chat"


# ---------------------------------------------------------------------------
# 4. on_feishu_event delivers message to actor mailbox
# ---------------------------------------------------------------------------

def test_on_feishu_event_sends_message():
    adapter, rt = make_adapter()
    evt = feishu_event()
    adapter.on_feishu_event(evt)

    addr = "feishu:test_app:oc_abc123"
    mailbox = rt.mailboxes.get(addr)
    assert mailbox is not None
    assert not mailbox.empty()
    msg = mailbox.get_nowait()
    assert isinstance(msg, Message)
    # Content fields in payload
    assert msg.payload["text"] == "hello"
    assert msg.payload["msg_type"] == "text"
    assert msg.payload["file_path"] == ""
    assert msg.payload["chat_id"] == "oc_abc123"
    assert msg.payload["message_id"] == "msg_001"
    # Metadata preserved
    assert msg.metadata["user"] == "Alice"
    assert msg.metadata["user_id"] == "ou_alice"


# ---------------------------------------------------------------------------
# 5. on_feishu_event dedup — same message_id only delivered once (via runtime)
# ---------------------------------------------------------------------------

def test_on_feishu_event_dedup():
    adapter, rt = make_adapter()
    evt = feishu_event(message_id="msg_dup")
    adapter.on_feishu_event(evt)
    adapter.on_feishu_event(evt)

    addr = "feishu:test_app:oc_abc123"
    mailbox = rt.mailboxes[addr]
    # Only one message should have been delivered — runtime deduplicates by message_id
    assert mailbox.qsize() == 1


# ---------------------------------------------------------------------------
# 6. on_feishu_event — echo prevention is now handler-level (FeishuInboundHandler)
# ---------------------------------------------------------------------------

def test_on_feishu_event_skip_own_messages():
    """Echo prevention is now checked in FeishuInboundHandler via actor.metadata["sent_msg_ids"].
    The adapter delivers the message; the handler drops it if it's in sent_msg_ids.
    This test verifies the adapter itself no longer holds _recent_sent state.
    """
    adapter, rt = make_adapter()
    assert not hasattr(adapter, "_recent_sent"), "adapter should not have _recent_sent"
    # Adapter always delivers — handler does the echo check
    evt = feishu_event(message_id="msg_self")
    adapter.on_feishu_event(evt)

    addr = "feishu:test_app:oc_abc123"
    # Message IS delivered to the mailbox; handler will drop it if msg_id is in sent_msg_ids
    mailbox = rt.mailboxes.get(addr)
    assert mailbox is not None
    assert not mailbox.empty()


# ---------------------------------------------------------------------------
# 7. on_feishu_event — thread events spawn with feishu_thread transport
# ---------------------------------------------------------------------------

def test_on_feishu_event_thread_without_session_falls_back_to_chat():
    """Thread messages without an active thread session go to main chat actor."""
    adapter, rt = make_adapter()
    evt = feishu_event(root_id="om_root789")
    adapter.on_feishu_event(evt)

    # Should route to main chat, not create a thread actor
    addr = "feishu:test_app:oc_abc123"
    actor = rt.lookup(addr)
    assert actor is not None
    assert actor.transport.type == "feishu_chat"

    # Thread actor should NOT be created
    thread_addr = "feishu:test_app:oc_abc123:om_root789"
    assert rt.lookup(thread_addr) is None


def test_on_feishu_event_thread_with_session_routes_to_thread():
    """Thread messages with an active thread session (downstream has transport) go to that thread actor."""
    adapter, rt = make_adapter()

    # Pre-spawn a thread actor with downstream (simulating a spawned session)
    rt.spawn(
        "feishu:test_app:oc_abc123:om_root789",
        "feishu_inbound",
        tag="session",
        transport=Transport(type="feishu_thread", config={"chat_id": "oc_abc123", "root_id": "om_root789"}),
        downstream=["cc:user.child"],
    )
    # The downstream CC actor must be active with transport
    rt.spawn(
        "cc:user.child",
        "cc_session",
        tag="child",
        transport=Transport(type="websocket", config={"instance_id": "user.child"}),
    )

    evt = feishu_event(root_id="om_root789")
    adapter.on_feishu_event(evt)

    # Message should go to thread actor
    mailbox = rt.mailboxes.get("feishu:test_app:oc_abc123:om_root789")
    assert mailbox is not None
    assert not mailbox.empty()


# ---------------------------------------------------------------------------
# 8. dedup set is bounded (max 10K entries)
# ---------------------------------------------------------------------------

def test_on_feishu_event_includes_file_path():
    adapter, rt = make_adapter()
    evt = feishu_event(msg_type="image", file_path="/tmp/downloads/photo.png")
    adapter.on_feishu_event(evt)

    addr = "feishu:test_app:oc_abc123"
    msg = rt.mailboxes[addr].get_nowait()
    assert msg.payload["msg_type"] == "image"
    assert msg.payload["file_path"] == "/tmp/downloads/photo.png"


# ---------------------------------------------------------------------------
# 9. dedup set is bounded (max 10K entries) — now in runtime._dedup
# ---------------------------------------------------------------------------

def test_dedup_set_bounded():
    adapter, rt = make_adapter()
    # Push 10001 events with unique message_ids — dedup now lives in runtime
    for i in range(10_001):
        evt = feishu_event(message_id=f"msg_{i}")
        adapter.on_feishu_event(evt)

    assert len(rt._dedup) <= 10_000


# ---------------------------------------------------------------------------
# 10. download_file uses typed SDK and returns file path
# ---------------------------------------------------------------------------

def test_download_file_success(tmp_path):
    adapter, _ = make_adapter()

    # Mock message
    msg = MagicMock()
    msg.message_type = "file"
    msg.chat_id = "oc_test"
    msg.content = json.dumps({"file_key": "fk_123", "file_name": "test.txt"})

    # Mock typed SDK response
    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.file = io.BytesIO(b"file content here")
    mock_resp.raw = None

    adapter.feishu_client.im.v1.message_resource.get.return_value = mock_resp

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        result = adapter.download_file("msg_001", msg)

    assert result != ""
    assert "test.txt" in result
    assert Path(result).exists()
    assert Path(result).read_bytes() == b"file content here"


def test_download_file_api_failure():
    adapter, _ = make_adapter()

    msg = MagicMock()
    msg.message_type = "image"
    msg.chat_id = "oc_test"
    msg.content = json.dumps({"image_key": "img_bad"})

    mock_resp = MagicMock()
    mock_resp.success.return_value = False
    mock_resp.code = 99991
    mock_resp.msg = "permission denied"

    adapter.feishu_client.im.v1.message_resource.get.return_value = mock_resp

    result = adapter.download_file("msg_002", msg)
    assert result == ""


def test_download_file_no_file_key():
    adapter, _ = make_adapter()

    msg = MagicMock()
    msg.message_type = "file"
    msg.chat_id = "oc_test"
    msg.content = json.dumps({})

    result = adapter.download_file("msg_003", msg)
    assert result == ""


# ---------------------------------------------------------------------------
# 11. download_image_by_key
# ---------------------------------------------------------------------------

def test_download_image_by_key_success(tmp_path):
    adapter, _ = make_adapter()

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    mock_resp.file = io.BytesIO(b"\x89PNG...")
    mock_resp.raw = None

    adapter.feishu_client.im.v1.message_resource.get.return_value = mock_resp

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        result = adapter.download_image_by_key("msg_010", "img_key_abc")

    assert result != ""
    assert "img_key_abc.png" in result
    assert Path(result).exists()


# ---------------------------------------------------------------------------
# 12. ACK reaction uses ROCKET emoji
# ---------------------------------------------------------------------------

def test_ack_reaction_uses_onit():
    adapter, _ = make_adapter()
    # Verify the default emoji_type parameter
    import inspect
    sig = inspect.signature(adapter._send_reaction)
    emoji_default = sig.parameters["emoji_type"].default
    assert emoji_default == "Typing"


# ---------------------------------------------------------------------------
# 13. Inbound attachment download — _resolve_inbound_attachment
# ---------------------------------------------------------------------------

def _mock_lark_message(msg_type: str, content: dict, message_id: str = "msg_att",
                       chat_id: str = "oc_test", parent_id: str = "") -> MagicMock:
    """Build a MagicMock lark message with the fields the adapter reads."""
    m = MagicMock()
    m.message_id = message_id
    m.message_type = msg_type
    m.chat_id = chat_id
    m.root_id = ""
    m.chat_type = "group"
    m.parent_id = parent_id
    m.content = json.dumps(content)
    return m


def _mock_resource_success(adapter, body: bytes = b"payload") -> None:
    """Wire feishu_client so message_resource.get returns a successful download."""
    resp = MagicMock()
    resp.success.return_value = True
    resp.file = io.BytesIO(body)
    resp.raw = None
    adapter.feishu_client.im.v1.message_resource.get.return_value = resp


def test_resolve_inbound_attachment_file_success(tmp_path):
    """A file message downloads and yields the documented [File received:] text."""
    adapter, _ = make_adapter()
    _mock_resource_success(adapter, b"hello")
    msg = _mock_lark_message("file", {"file_key": "fk_1", "file_name": "doc.pdf"})

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        text, file_path = adapter._resolve_inbound_attachment(
            "msg_att", "file", msg, "[file: doc.pdf]"
        )

    assert file_path != ""
    assert file_path.endswith("doc.pdf")
    assert Path(file_path).exists()
    assert text == f"[File received: {file_path}]"


def test_download_file_sanitizes_traversal_filename(tmp_path):
    """A crafted file_name ('../../') cannot escape the uploads dir."""
    adapter, _ = make_adapter()
    _mock_resource_success(adapter, b"x")
    msg = _mock_lark_message("file", {"file_key": "fk_evil", "file_name": "../../../evil.sh"})

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        result = adapter.download_file("msg_att", msg)

    uploads_root = (tmp_path / ".openclaw" / "uploads").resolve()
    assert result != ""
    assert Path(result).resolve().is_relative_to(uploads_root)
    assert Path(result).name == "evil.sh"
    assert not (tmp_path / "evil.sh").exists()


def test_resolve_inbound_attachment_download_failure():
    """API failure yields a clear 下载失败 note and no path."""
    adapter, _ = make_adapter()
    resp = MagicMock()
    resp.success.return_value = False
    resp.code = 99991
    resp.msg = "permission denied"
    adapter.feishu_client.im.v1.message_resource.get.return_value = resp
    msg = _mock_lark_message("file", {"file_key": "fk_bad", "file_name": "x.bin"})

    text, file_path = adapter._resolve_inbound_attachment(
        "msg_att", "file", msg, "[file: x.bin]"
    )

    assert file_path == ""
    assert "下载失败" in text


def test_resolve_inbound_attachment_non_downloadable_passthrough():
    """Text / non-attachment types pass through unchanged, never download."""
    adapter, _ = make_adapter()
    msg = _mock_lark_message("text", {"text": "hi"})

    text, file_path = adapter._resolve_inbound_attachment("msg_att", "text", msg, "hi")

    assert (text, file_path) == ("hi", "")
    adapter.feishu_client.im.v1.message_resource.get.assert_not_called()


def test_resolve_inbound_attachment_no_client_passthrough():
    """Without a Feishu client we cannot download — keep the parser label."""
    adapter, _ = make_adapter()
    adapter.feishu_client = None
    msg = _mock_lark_message("image", {"image_key": "img_1"})

    text, file_path = adapter._resolve_inbound_attachment(
        "msg_att", "image", msg, "[image: img_1]"
    )

    assert (text, file_path) == ("[image: img_1]", "")


# ---------------------------------------------------------------------------
# 14. Inbound wiring — _build_inbound_event actually invokes the download
# (guards against the regression where the download call site was deleted)
# ---------------------------------------------------------------------------

def test_build_inbound_event_downloads_file(tmp_path):
    """A file message flows through _build_inbound_event with file_path populated
    and the [File received:] text — proving the download is wired into inbound."""
    adapter, _ = make_adapter()
    _mock_resource_success(adapter, b"binary")
    msg = _mock_lark_message("file", {"file_key": "fk_9", "file_name": "report.csv"})

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        evt = adapter._build_inbound_event(msg, "ou_bob", "Bob")

    assert evt["file_path"].endswith("report.csv")
    assert Path(evt["file_path"]).exists()
    assert evt["text"] == f"[File received: {evt['file_path']}]"
    assert evt["file_key"] == "fk_9"
    assert evt["msg_type"] == "file"
    assert evt["user"] == "Bob"
    adapter.feishu_client.im.v1.message_resource.get.assert_called_once()


def test_build_inbound_event_image_downloads(tmp_path):
    """An image message downloads via image_key and carries the local path."""
    adapter, _ = make_adapter()
    _mock_resource_success(adapter, b"\x89PNG")
    msg = _mock_lark_message("image", {"image_key": "img_z"})

    with patch("channel_server.adapters.feishu.adapter.PROJECT_ROOT", tmp_path):
        evt = adapter._build_inbound_event(msg, "ou_bob", "Bob")

    assert evt["file_path"].endswith("img_z.png")
    assert Path(evt["file_path"]).exists()
    assert evt["text"] == f"[File received: {evt['file_path']}]"


def test_build_inbound_event_text_no_download():
    """A plain text message never triggers a download and carries no file_path."""
    adapter, _ = make_adapter()
    msg = _mock_lark_message("text", {"text": "just text"})

    evt = adapter._build_inbound_event(msg, "ou_bob", "Bob")

    assert evt["file_path"] == ""
    assert evt["text"] == "just text"
    adapter.feishu_client.im.v1.message_resource.get.assert_not_called()


def test_build_inbound_event_download_failure_notes_error(tmp_path):
    """Download failure never crashes — event carries a clear note, empty path."""
    adapter, _ = make_adapter()
    resp = MagicMock()
    resp.success.return_value = False
    resp.code = 99991
    resp.msg = "no permission"
    adapter.feishu_client.im.v1.message_resource.get.return_value = resp
    msg = _mock_lark_message("file", {"file_key": "fk_bad", "file_name": "y.bin"})

    evt = adapter._build_inbound_event(msg, "ou_bob", "Bob")

    assert evt["file_path"] == ""
    assert "下载失败" in evt["text"]


def test_inbound_event_file_path_propagates_to_downstream(tmp_path):
    """End-to-end: an event carrying file_path lands in the downstream message
    payload (so channel.py inject_message can surface meta.file_path)."""
    adapter, rt = make_adapter()
    captured: list = []
    original_send = rt.send

    def _capture(address, message, message_id=None):
        captured.append((address, message))
        return original_send(address, message, message_id=message_id)

    rt.send = _capture

    evt = feishu_event(msg_type="file", file_path="/abs/uploads/oc_abc123/doc.pdf",
                       text="[File received: /abs/uploads/oc_abc123/doc.pdf]")
    adapter.on_feishu_event(evt)

    assert captured, "expected a downstream send"
    _, msg = captured[0]
    assert msg.payload.get("file_path") == "/abs/uploads/oc_abc123/doc.pdf"
    assert msg.metadata.get("msg_type") == "file"
