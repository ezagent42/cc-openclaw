"""Extensible message type parsers for Feishu messages.

Each parser is a function registered via @register_parser("msg_type").
New message types can be supported by adding a decorated function.

Parser signature:
    (content: dict, message, adapter) -> tuple[str, str]
    Returns (text_representation, file_path)
    - text_representation: human-readable text for the message
    - file_path: local file path if a file was downloaded, else ""
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from channel_server.adapters.feishu.adapter import FeishuAdapter

log = logging.getLogger("channel-server.parsers")

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_parsers: dict[str, Callable] = {}


def register_parser(*msg_types: str):
    """Decorator to register a parser for one or more message types."""
    def decorator(fn):
        for mt in msg_types:
            _parsers[mt] = fn
        return fn
    return decorator


def parse_message(msg_type: str, content: dict, message, server: FeishuAdapter) -> tuple[str, str]:
    """Parse a Feishu message into (text, file_path).

    Falls back to a descriptive label for unregistered types.
    """
    parser = _parsers.get(msg_type)
    if parser:
        try:
            return parser(content, message, server)
        except Exception as e:
            log.warning("Parser for %s failed: %s", msg_type, e)
            return f"[{msg_type} 消息 — 解析失败]", ""
    return f"[{msg_type} 消息]", ""


# ---------------------------------------------------------------------------
# P0: text, post, downloadable files
# ---------------------------------------------------------------------------

@register_parser("text")
def _parse_text(content: dict, message, server) -> tuple[str, str]:
    return content.get("text", ""), ""


@register_parser("post")
def _parse_post(content: dict, message, server) -> tuple[str, str]:
    parts = [content.get("title", "")]
    image_keys: list[str] = []
    for para in content.get("content", []):
        for node in para or []:
            tag = node.get("tag", "")
            if tag == "text" or node.get("text"):
                parts.append(node.get("text", ""))
            elif tag == "img":
                image_keys.append(node.get("image_key", ""))

    # Don't download inline images — include image_key reference for deferred download
    for ik in image_keys:
        if ik:
            parts.append(f"[图片: {ik}]")

    return " ".join(p for p in parts if p), ""


@register_parser("image", "file", "audio", "media")
def _parse_downloadable(content: dict, message, server) -> tuple[str, str]:
    msg_type = message.message_type or "file"
    if msg_type == "image":
        file_key = content.get("image_key", "")
    else:
        file_key = content.get("file_key", "")
    file_name = content.get("file_name", file_key or "unknown")
    # Don't download here — pass file_key for deferred download
    return f"[{msg_type}: {file_name}]", ""


# ---------------------------------------------------------------------------
# P0: merge_forward, interactive, sticker
# ---------------------------------------------------------------------------

@register_parser("merge_forward")
def _parse_merge_forward(content: dict, message, server) -> tuple[str, str]:
    """Fetch sub-messages via GET /im/v1/messages/{message_id}."""
    msg_id = message.message_id or ""
    if not msg_id or not server.feishu_client:
        return "[合并转发消息]", ""

    try:
        from lark_oapi.api.im.v1 import GetMessageRequest

        req = GetMessageRequest.builder().message_id(msg_id).build()
        resp = server.feishu_client.im.v1.message.get(req)

        if not resp.success() or not resp.data or not resp.data.items:
            log.warning("Failed to fetch merge_forward sub-messages: %s", resp.msg if resp else "no response")
            return "[合并转发消息 — 获取失败]", ""

        lines = ["--- 合并转发 ---"]
        for item in resp.data.items:
            sub_type = item.msg_type or ""
            if sub_type == "merge_forward":
                continue  # skip the wrapper itself

            # Parse sub-message content
            raw_content = item.body.content if item.body and item.body.content else ""
            try:
                sub_content = json.loads(raw_content) if raw_content else {}
            except Exception:
                sub_content = {}

            sub_text, _ = parse_message(sub_type, sub_content, item, server)

            # Get sender name
            sender_name = ""
            if item.sender and item.sender.id:
                sender_name = server._resolve_user(item.sender.id) if hasattr(server, '_resolve_user') else item.sender.id

            if sub_text:
                prefix = f"{sender_name}: " if sender_name else ""
                lines.append(f"{prefix}{sub_text}")

        lines.append("--- 合并转发结束 ---")
        return "\n".join(lines), ""

    except Exception as e:
        log.warning("merge_forward parse error: %s", e)
        return "[合并转发消息 — 解析失败]", ""


@register_parser("interactive")
def _parse_interactive(content: dict, message, server) -> tuple[str, str]:
    """Extract text from interactive card messages.

    Card content can be either:
    - A dict with header/elements (standard card)
    - A list of elements (simplified card or sub-message in merge_forward)
    """
    parts = []

    # Handle case where content is wrapped differently
    if isinstance(content, list):
        # Simplified format: content is directly a list of elements
        elements = content
    else:
        # Standard card format
        header = content.get("header", {})
        if isinstance(header, dict):
            title = header.get("title", {})
            if isinstance(title, dict):
                parts.append(title.get("content", ""))
            elif isinstance(title, str):
                parts.append(title)
        elements = content.get("elements", [])
        if not isinstance(elements, list):
            elements = []

    # Extract text from elements
    # elements can be: [dict, dict, ...] or [[dict, dict], [dict, dict], ...] (nested)
    def _extract_from_nodes(nodes: list):
        """Recursively extract text from a list of element nodes."""
        for node in nodes:
            if isinstance(node, list):
                # Nested array — recurse (common in merge_forward cards)
                _extract_from_nodes(node)
                continue
            if not isinstance(node, dict):
                continue
            tag = node.get("tag", "")
            if tag == "text":
                text = node.get("text", "")
                if text:
                    parts.append(text)
            elif tag == "div":
                text_obj = node.get("text", {})
                if isinstance(text_obj, dict):
                    parts.append(text_obj.get("content", ""))
                elif isinstance(text_obj, str):
                    parts.append(text_obj)
                # div may contain fields (sub-elements)
                fields = node.get("fields", [])
                if fields:
                    _extract_from_nodes(fields)
            elif tag == "markdown":
                parts.append(node.get("content", ""))
            elif tag == "note":
                _extract_from_nodes(node.get("elements", []))
            elif tag == "action":
                for action in node.get("actions", []):
                    if not isinstance(action, dict):
                        continue
                    action_text = action.get("text", {})
                    if isinstance(action_text, dict):
                        parts.append(f"[按钮: {action_text.get('content', '')}]")
                    elif isinstance(action_text, str):
                        parts.append(f"[按钮: {action_text}]")
            elif tag == "hr":
                parts.append("---")
            elif tag == "a":
                parts.append(node.get("text", "") or node.get("href", ""))
            elif tag == "at":
                parts.append(f"@{node.get('user_name', node.get('user_id', ''))}")
            elif tag == "img":
                parts.append(f"[图片: {node.get('image_key', 'unknown')}]")
            elif tag:
                parts.append(f"[{tag}]")

    _extract_from_nodes(elements)

    text = "\n".join(p for p in parts if p)
    # Detect Feishu's fallback text for cards that can't be rendered via API
    if text and "请升级至最新版本客户端" in text:
        return "[消息卡片 — 内容不可通过 API 获取]", ""
    return text or "[消息卡片]", ""


@register_parser("sticker")
def _parse_sticker(content: dict, message, server) -> tuple[str, str]:
    return "[表情包]", ""


# ---------------------------------------------------------------------------
# P1: share_chat, share_user, location, todo
# ---------------------------------------------------------------------------

@register_parser("share_chat")
def _parse_share_chat(content: dict, message, server) -> tuple[str, str]:
    chat_id = content.get("chat_id", "")
    return f"[群名片: {chat_id}]", ""


@register_parser("share_user")
def _parse_share_user(content: dict, message, server) -> tuple[str, str]:
    user_id = content.get("user_id", "")
    return f"[用户名片: {user_id}]", ""


@register_parser("location")
def _parse_location(content: dict, message, server) -> tuple[str, str]:
    name = content.get("name", "未知位置")
    lat = content.get("latitude", "")
    lng = content.get("longitude", "")
    coords = f" ({lat}, {lng})" if lat and lng else ""
    return f"[位置: {name}{coords}]", ""


@register_parser("todo")
def _parse_todo(content: dict, message, server) -> tuple[str, str]:
    """Parse todo/task message. Try to extract summary from content."""
    task_id = content.get("task_id", "")
    summary = content.get("summary", "")

    # summary may be in post/rich-text format
    if isinstance(summary, dict):
        # Try to extract text from post-like structure
        parts = []
        for para in summary.get("content", []):
            for node in para or []:
                if node.get("text"):
                    parts.append(node["text"])
        summary = " ".join(parts)
    elif isinstance(summary, str) and summary.startswith("{"):
        try:
            parsed = json.loads(summary)
            parts = []
            for para in parsed.get("content", []):
                for node in para or []:
                    if node.get("text"):
                        parts.append(node["text"])
            summary = " ".join(parts)
        except Exception:
            pass

    if summary:
        return f"[任务: {summary}]", ""
    if task_id:
        return f"[任务: task_id={task_id}]", ""
    return "[任务消息]", ""


# ---------------------------------------------------------------------------
# P2: system
# ---------------------------------------------------------------------------

@register_parser("system")
def _parse_system(content: dict, message, server) -> tuple[str, str]:
    """Parse system messages (join/leave/rename/etc)."""
    template = content.get("template", "")

    # Common templates
    if "add_member" in template or "join" in template:
        return "[系统: 新成员加入]", ""
    if "remove_member" in template or "leave" in template:
        return "[系统: 成员退出]", ""
    if "rename" in template:
        return "[系统: 群名变更]", ""
    if "divider" in template:
        divider = content.get("divider_text", {})
        if isinstance(divider, dict):
            # i18n structure
            text = divider.get("zh_cn", "") or divider.get("en_us", "") or str(divider)
        else:
            text = str(divider)
        return f"[系统: {text}]" if text else "[系统消息]", ""

    return f"[系统消息: {template}]" if template else "[系统消息]", ""


# ---------------------------------------------------------------------------
# P2: other rare types (descriptive fallback)
# ---------------------------------------------------------------------------

@register_parser("hongbao")
def _parse_hongbao(content: dict, message, server) -> tuple[str, str]:
    return "[红包]", ""


@register_parser("vote")
def _parse_vote(content: dict, message, server) -> tuple[str, str]:
    topic = content.get("topic", "")
    options = content.get("options", [])
    if topic:
        opt_text = " / ".join(options) if options else ""
        return f"[投票: {topic}] {opt_text}".strip(), ""
    return "[投票]", ""


@register_parser("video_chat")
def _parse_video_chat(content: dict, message, server) -> tuple[str, str]:
    topic = content.get("topic", "视频通话")
    return f"[视频通话: {topic}]", ""


@register_parser("share_calendar_event", "calendar", "general_calendar")
def _parse_calendar(content: dict, message, server) -> tuple[str, str]:
    summary = content.get("summary", "")
    return f"[日历: {summary}]" if summary else "[日历事件]", ""


@register_parser("folder")
def _parse_folder(content: dict, message, server) -> tuple[str, str]:
    file_name = content.get("file_name", "")
    return f"[文件夹: {file_name}]" if file_name else "[文件夹]", ""
