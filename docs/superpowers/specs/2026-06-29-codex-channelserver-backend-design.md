# Codex ChannelServer Backend Design

Date: 2026-06-29

## Goal

Add a Codex-backed execution path to the existing channel-server so a user can run:

```bash
./codex-openclaw.sh --user linyilun
```

The command should start or reuse a Codex app-server, open a Codex TUI attached to that app-server, and connect Feishu messages for that user to the corresponding Codex thread.

The integration must keep the current Feishu/channel-server actor pipeline as the source of truth. "openclaw" remains only a historical repository/script name; the product-facing integration name for Codex and Claude Code tools should be ChannelServer.

## Non-Goals

- Do not support tmux for Codex sessions.
- Do not inject messages into Codex TUI through a Claude Code-style channel notification protocol.
- Do not automatically send every Codex assistant response back to Feishu.
- Do not allow Claude Code and Codex to receive the same Feishu message concurrently from one channel-server process.
- Do not rename the whole repository or remove Claude Code support in this change.

## Current System

The current Claude Code path is:

1. `FeishuAdapter` receives Feishu events and routes them through the command dispatcher, then the actor pipeline.
2. Root sessions are represented by `cc:{user}.root`; child sessions by `cc:{user}.{session}`.
3. `CCSessionHandler` turns Feishu-originated actor messages into `TransportSend(action="message")`.
4. `CCAdapter` delivers those payloads over WebSocket to `channel_server/adapters/cc/channel.py`.
5. `channel.py` is a Claude Code MCP channel server that sends `notifications/claude/channel`.
6. Claude Code returns messages by calling MCP tools such as `reply`.
7. `/spawn` and `/kill` are currently tied to CC actor addresses and tmux process lifecycle.

The existing duplicate protection only prevents duplicate tmux windows, duplicate actor addresses, or duplicate downstream edges. It does not prevent `cc:{user}.root` and `codex:{user}.root` from both being wired to the same Feishu chat.

## Recommended Approach

Implement Codex as a second agent backend while enforcing one active backend per channel-server process.

The channel-server process has an `active_backend` value:

- `null` before any agent backend claims it.
- `"cc"` after a Claude Code session registers.
- `"codex"` after a Codex bridge registers.

When a backend registers:

- If `active_backend` is empty, claim it.
- If it matches the requested backend, allow registration.
- If it differs, reject registration with a clear error.

This keeps the first version simple: a running channel-server can be owned by CC or Codex, not both. That avoids per-chat routing complexity and prevents double delivery through Feishu actor downstream broadcast.

## Components

### `codex-openclaw.sh`

New launcher script mirroring the useful parts of `cc-openclaw.sh --user`:

- Source shell profile and local overrides.
- Parse `--user`, `--group`, `--session`, `--tag`, `--list`, `--status`, `--stop`, and `--help` where practical.
- Resolve user role and Feishu `chat_id` from `roles/roles.yaml` and `.workspace/chat_id_map.json`.
- Create `.workspace/{user}/{session}`.
- Render or generate Codex-readable instructions for the session.
- Ensure channel-server is running.
- Start or reuse Codex app-server.
- Start or reuse the Codex inbound bridge.
- Open Codex TUI against the app-server using `codex --remote ...`, or `codex resume <threadId> --remote ...` when a thread already exists.

The script should not start tmux.

### Codex Inbound Bridge

Add `channel_server/adapters/codex/bridge.py` as a process-level bridge.

Responsibilities:

- Connect to channel-server using the existing local WebSocket registration style, with `backend=codex`.
- Register an instance such as `codex:linyilun.root`.
- Connect to Codex app-server through its JSON-RPC app-server API.
- Maintain the mapping from `{user, session, chat_id}` to Codex `threadId`.
- On Feishu-originated channel-server messages, call Codex app-server `thread/start` or `thread/resume`, then `turn/start`.
- Persist the mapping under the session workspace, for example:

```json
{
  "backend": "codex",
  "user": "linyilun",
  "session": "root",
  "chat_id": "oc_xxx",
  "thread_id": "019...",
  "app_server": "unix:///path/to/socket"
}
```

The bridge does not automatically forward Codex assistant output to Feishu.

### ChannelServer MCP

Add a Codex-compatible MCP tool server named ChannelServer MCP.

Canonical tool:

- `call-channelserver`

Purpose:

- Let Codex explicitly send useful information back to the current Feishu chat/session.
- Use launcher-provided environment variables such as `OC_USER`, `OC_SESSION`, `OC_CHAT_ID`, and `OC_BACKEND`.
- Do not let the model choose arbitrary Feishu `chat_id` values.
- Send text to channel-server, which routes it through the existing Feishu adapter.

Claude Code should eventually expose the same canonical name. Existing CC tools such as `reply` can remain as compatibility aliases, but new docs and instructions should prefer `call-channelserver`.

### Codex Actor Handler

Add `codex_session` as a handler alongside `cc_session`.

The behavior should mirror the useful message routing semantics of `CCSessionHandler`, without tmux lifecycle behavior:

- External message from Feishu or another actor: emit `TransportSend(action="message", ...)` to the Codex bridge transport.
- Message from the Codex actor itself with plain text: route to downstream Feishu actors.
- `forward`, `send_summary`, `send_file`, `react`, and related actions may follow the CC handler behavior where needed.
- `on_spawn` must not start tmux.
- `on_stop` should stop downstream Feishu thread actors like the CC handler does.

### Backend-Aware Commands

Update commands that currently assume CC:

- `/sessions`: list sessions for the active backend, including `codex:` actors.
- `/kill <name>`: for Codex, stop actors and Feishu thread state only; do not kill tmux.
- `/spawn <name>`: for Codex, create a Feishu thread anchor and a `codex:{user}.{session}` actor; do not start tmux.

The command context should stop treating `cc` as the only agent adapter. A narrow first step is to add backend-aware helpers while leaving existing CC behavior intact.

## Data Flow

### Feishu to Codex

1. Feishu user sends a message.
2. `FeishuAdapter` parses the event and sends it into the actor pipeline.
3. Feishu actor forwards to its single active backend downstream.
4. `codex_session` emits a transport payload.
5. Codex bridge receives the payload.
6. Codex bridge starts or resumes the mapped Codex thread.
7. Codex bridge calls `turn/start` with the Feishu message text.
8. Codex TUI connected to the same app-server can observe or resume the thread.

### Codex to Feishu

1. Codex decides a message should be sent back.
2. Codex calls `call-channelserver`.
3. ChannelServer MCP sends a bounded payload to channel-server.
4. The corresponding `codex:{user}.{session}` actor routes the text to downstream Feishu actor.
5. `FeishuAdapter` sends the message to Feishu.

## Backend Claiming

Add a backend claim check in channel-server registration.

Suggested fields:

```json
{
  "active_backend": "codex",
  "active_backend_claimed_by": "codex:linyilun.root"
}
```

Expose this state in `.channel-server.pid` or an adjacent state file so launchers can fail early before connecting.

Registration rejection should be explicit and actionable:

```text
channel-server is already claimed by backend=cc; stop that session or restart channel-server before starting backend=codex
```

## Error Handling

- If channel-server is not running, `codex-openclaw.sh` exits with the same kind of clear message as `cc-openclaw.sh`.
- If channel-server is claimed by CC, `codex-openclaw.sh` exits before starting Codex app-server when possible.
- If app-server cannot start, do not register the Codex backend.
- If Codex bridge loses app-server connection, keep channel-server actor suspended or mark the bridge disconnected; do not silently drop Feishu messages as successful.
- If `call-channelserver` is called without a valid current binding, return an MCP tool error and do not send to Feishu.
- If `call-channelserver` payload is empty or too large, reject locally before channel-server delivery.

## Testing

Unit tests:

- Backend claim allows first backend and repeated same backend registration.
- Backend claim rejects a different backend.
- `codex_session` forwards Feishu-originated messages to transport.
- `codex_session` routes explicit Codex-originated text to Feishu downstream.
- `/sessions` includes Codex actors under Codex backend.
- `/kill` for Codex stops actors without tmux calls.
- `/spawn` for Codex creates thread actor and Codex actor without tmux calls.
- `call-channelserver` refuses arbitrary chat IDs and uses the launcher binding.

Integration tests:

- Start fake channel-server runtime, fake Feishu actor, and fake Codex bridge; verify one Feishu message results in one Codex `turn/start`.
- Verify CC-claimed channel-server rejects Codex bridge registration.
- Verify Codex-claimed channel-server rejects CC channel registration.
- Verify `call-channelserver` produces a Feishu outbound transport send.

Manual smoke:

1. Start channel-server.
2. Run `./codex-openclaw.sh --user linyilun`.
3. Send a Feishu DM to the bot.
4. Confirm the Codex thread receives the message.
5. Ask Codex to call `call-channelserver`.
6. Confirm Feishu receives the tool-sent reply.
7. Try starting `./cc-openclaw.sh --user linyilun` while Codex owns the channel-server; confirm it is rejected.

## Migration Notes

- Keep `cc-openclaw.sh` working.
- Keep existing CC MCP tools as compatibility aliases.
- Document `call-channelserver` as the canonical name for both CC and Codex.
- Avoid new references to "openclaw" in user-facing tool names except where legacy filenames require it.

