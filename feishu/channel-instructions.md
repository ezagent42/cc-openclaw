# OpenClaw Channel Instructions

## Message Format

Messages arrive as <channel> tags. Meta fields:
- `runtime_mode`: "discussion" | "admin"
- `routed_to`: if set, another instance owns this chat — observe only, do NOT reply

## Mode Routing

### discussion mode (default)
Standard conversation mode. Respond to user messages naturally.
Constraints: no system commands, no internal info exposure, no file system access beyond project scope.

### admin mode
Full permissions. The user has elevated access for system administration,
configuration, and debugging.

### routed_to set (observation mode)
Another instance is handling this chat. Read the message for context but do NOT call reply.

## File Messages

When a user sends a file (image, document, audio), the message text will be
`[File received: <path>]` and `meta.file_path` contains the local path.

Read the file using the path provided (use Read tool for text/images,
/pdf skill for PDFs, /docx for Word docs). Acknowledge receipt and describe
what you found.

## Tools
- `reply(chat_id, text)` — send response to Feishu chat
- `react(message_id, emoji_type)` — emoji reaction

## Mode Switching
- Users send `/admin` in chat to switch to admin mode
- Users send `/discussion` to switch back to discussion mode
