"""Tests for Feishu message parsers — downloadable files, text, post, etc."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from channel_server.adapters.feishu.parsers import parse_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_message(msg_type: str, content: dict, message_id: str = "msg_001",
                 chat_id: str = "oc_abc123") -> MagicMock:
    """Create a mock Feishu message object."""
    msg = MagicMock()
    msg.message_type = msg_type
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.content = json.dumps(content)
    return msg


def make_server(download_returns: str = "") -> MagicMock:
    """Create a mock FeishuAdapter (server)."""
    server = MagicMock()
    server.download_file = MagicMock(return_value=download_returns)
    server.download_image_by_key = MagicMock(return_value=download_returns)
    server.feishu_client = MagicMock()
    return server


# ---------------------------------------------------------------------------
# 1. Text messages
# ---------------------------------------------------------------------------

def test_parse_text():
    msg = make_message("text", {"text": "hello world"})
    text, file_path = parse_message("text", {"text": "hello world"}, msg, make_server())
    assert text == "hello world"
    assert file_path == ""


def test_parse_text_empty():
    msg = make_message("text", {})
    text, _ = parse_message("text", {}, msg, make_server())
    assert text == ""


# ---------------------------------------------------------------------------
# 2. File / image / audio / media — parsers no longer download
# ---------------------------------------------------------------------------

def test_parse_file_download_success():
    """Parsers no longer download — file_path is always "", file_name in text."""
    msg = make_message("file", {"file_key": "fk_123", "file_name": "doc.pdf"})
    server = make_server(download_returns="/tmp/uploads/doc.pdf")
    text, file_path = parse_message("file", {"file_key": "fk_123", "file_name": "doc.pdf"}, msg, server)
    assert file_path == ""
    assert "doc.pdf" in text
    server.download_file.assert_not_called()


def test_parse_image_download_success():
    """Parsers no longer download — file_path is always "", image_key in text."""
    msg = make_message("image", {"image_key": "img_abc"})
    server = make_server(download_returns="/tmp/uploads/img_abc.png")
    text, file_path = parse_message("image", {"image_key": "img_abc"}, msg, server)
    assert file_path == ""
    assert "img_abc" in text
    server.download_file.assert_not_called()


def test_parse_audio_download_success():
    """Parsers no longer download — file_path is always ""."""
    msg = make_message("audio", {"file_key": "audio_001"})
    server = make_server(download_returns="/tmp/uploads/audio.bin")
    text, file_path = parse_message("audio", {"file_key": "audio_001"}, msg, server)
    assert file_path == ""
    assert "audio_001" in text


def test_parse_media_download_success():
    """Parsers no longer download — file_path is always "", file_name in text."""
    msg = make_message("media", {"file_key": "media_001", "file_name": "video.mp4"})
    server = make_server(download_returns="/tmp/uploads/video.mp4")
    text, file_path = parse_message("media", {"file_key": "media_001", "file_name": "video.mp4"}, msg, server)
    assert file_path == ""
    assert "video.mp4" in text


def test_parse_file_no_download_attempt():
    """Parsers never attempt download regardless of server state."""
    msg = make_message("file", {"file_key": "fk_bad"})
    server = make_server(download_returns="")
    text, file_path = parse_message("file", {"file_key": "fk_bad"}, msg, server)
    assert file_path == ""
    assert "fk_bad" in text
    server.download_file.assert_not_called()


def test_parse_image_no_download_attempt():
    """Image parsers never attempt download."""
    msg = make_message("image", {"image_key": "img_bad"})
    server = make_server(download_returns="")
    text, file_path = parse_message("image", {"image_key": "img_bad"}, msg, server)
    assert file_path == ""
    assert "img_bad" in text
    server.download_file.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Post messages with inline images
# ---------------------------------------------------------------------------

def test_parse_post_with_text():
    content = {
        "title": "Title",
        "content": [[{"tag": "text", "text": "paragraph one"}]],
    }
    msg = make_message("post", content)
    text, _ = parse_message("post", content, msg, make_server())
    assert "Title" in text
    assert "paragraph one" in text


def test_parse_post_with_inline_image_success():
    """Post parsers no longer download — image_key reference appears in text."""
    content = {
        "title": "",
        "content": [[{"tag": "img", "image_key": "img_inline"}]],
    }
    msg = make_message("post", content)
    server = make_server(download_returns="/tmp/inline.png")
    text, file_path = parse_message("post", content, msg, server)
    assert file_path == ""
    assert "img_inline" in text
    server.download_image_by_key.assert_not_called()


def test_parse_post_with_inline_image_reference():
    """Post with inline image always yields image_key in text, never downloads."""
    content = {
        "title": "",
        "content": [[{"tag": "img", "image_key": "img_fail"}]],
    }
    msg = make_message("post", content)
    server = make_server(download_returns="")
    text, file_path = parse_message("post", content, msg, server)
    assert file_path == ""
    assert "图片: img_fail" in text
    server.download_image_by_key.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Unknown message types — graceful fallback
# ---------------------------------------------------------------------------

def test_parse_unknown_type():
    msg = make_message("unknown_type", {})
    text, file_path = parse_message("unknown_type", {}, msg, make_server())
    assert "unknown_type" in text
    assert file_path == ""


# ---------------------------------------------------------------------------
# 6. Sticker, share_chat, location, etc.
# ---------------------------------------------------------------------------

def test_parse_sticker():
    msg = make_message("sticker", {})
    text, _ = parse_message("sticker", {}, msg, make_server())
    assert "表情包" in text


def test_parse_share_chat():
    msg = make_message("share_chat", {"chat_id": "oc_xyz"})
    text, _ = parse_message("share_chat", {"chat_id": "oc_xyz"}, msg, make_server())
    assert "oc_xyz" in text


def test_parse_location():
    content = {"name": "北京", "latitude": "39.9", "longitude": "116.4"}
    msg = make_message("location", content)
    text, _ = parse_message("location", content, msg, make_server())
    assert "北京" in text
    assert "39.9" in text


# ---------------------------------------------------------------------------
# 7. Interactive card messages
# ---------------------------------------------------------------------------

def test_parse_interactive_card():
    content = {
        "header": {"title": {"content": "Card Title"}},
        "elements": [{"tag": "div", "text": {"content": "Body text"}}],
    }
    msg = make_message("interactive", content)
    text, _ = parse_message("interactive", content, msg, make_server())
    assert "Card Title" in text
    assert "Body text" in text


def test_parse_interactive_markdown():
    content = {
        "elements": [{"tag": "markdown", "content": "**bold** text"}],
    }
    msg = make_message("interactive", content)
    text, _ = parse_message("interactive", content, msg, make_server())
    assert "**bold** text" in text
