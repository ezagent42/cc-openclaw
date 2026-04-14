# Design: Extensible Message Parser Architecture

> Date: 2026-04-14
> Status: Approved

## Problem

channel_server.py handles message types via a rigid if/elif chain (text, post, file, image, audio, media). Adding new types requires modifying the chain. merge_forward, interactive, sticker, share_chat, share_user, location, todo, system are unsupported.

## Solution

Extract message parsing into a registry-based system in `feishu/message_parsers.py`. Each message type has a registered parser function. Unknown types auto-degrade to `[xxx 消息]`.

## Parser Interface

```python
(content: dict, message: Message, server: ChannelServer) -> tuple[str, str]
#  Returns (text, file_path)
```

## Types to Implement

| msg_type | Priority | Strategy |
|----------|----------|----------|
| text | existing | Extract text field |
| post | existing | Walk content array, extract text nodes |
| image/file/audio/media | existing | Download via file API |
| merge_forward | P0 | Call GET /im/v1/messages/{id}, recursively parse sub-messages |
| interactive | P0 | Extract title + element text from card JSON |
| sticker | P0 | Return [表情包] |
| share_chat | P1 | Extract chat_id |
| share_user | P1 | Extract user_id |
| location | P1 | Extract name + coordinates |
| todo | P1 | Call Task API or extract summary |
| system | P2 | Extract template type + variables |
| unknown | fallback | Return [msg_type 消息] |

## Files

- Create: `feishu/message_parsers.py`
- Modify: `feishu/channel_server.py` (replace if/elif with parse_message call)
