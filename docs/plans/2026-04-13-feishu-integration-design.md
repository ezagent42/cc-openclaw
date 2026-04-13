# Feishu Integration Design for cc-openclaw

Date: 2026-04-13

## Goal

Add Feishu (飞书) messaging to cc-openclaw so Claude Code instances can receive
and reply to Feishu messages. Ported from AutoService-Cinnox with
Cinnox-specific logic removed.

## Architecture

```
┌──────────────┐     WebSocket      ┌───────────────┐     Feishu WS     ┌──────────┐
│  Claude Code  │◄──────────────────►│ channel-server │◄──────────────────►│  飞书 API │
│  (N instances)│   (random port)    │  (standalone)  │    (lark_oapi)    └──────────┘
│  + channel.py │                    └───────────────┘
│    (MCP)      │
└──────────────┘
```

Three-process model:

1. **channel_server.py** — standalone WebSocket daemon. Binds `port=0` (OS
   picks a free port), writes `PID:PORT` to `.channel-server.pid`. Connects to
   Feishu via `lark_oapi` SDK, routes messages to registered CC instances.
2. **channel.py** — MCP server loaded by each CC instance. Reads
   `.channel-server.pid` to discover the port, connects via WebSocket, bridges
   Feishu messages into Claude Code as MCP notifications.
3. **cc-openclaw.sh** — launcher. Checks `.channel-server.pid` for a running
   channel-server; if not running, prints the start command and exits.

## Port Discovery

- channel-server binds `port=0`, then writes `{pid}:{port}` to
  `$SCRIPT_DIR/.channel-server.pid`.
- cc-openclaw.sh reads the pidfile, verifies the PID is alive (`kill -0`),
  extracts the port. If pidfile missing or PID dead → error + exit.
- channel.py reads the same pidfile at startup via `CHANNEL_SERVER_PIDFILE`
  env var or defaults to `$PROJECT_ROOT/.channel-server.pid`.

## File Structure

```
cc-openclaw/
├── cc-openclaw.sh                  # modified
├── cc-openclaw.local.sh            # modified (proxy, ADMIN_CHAT_ID)
├── .feishu-credentials.json        # new (gitignored)
├── .channel-server.pid             # runtime, gitignored
├── .claude/
│   ├── mcp.json                    # new: register autoservice-channel
│   ├── settings.json               # existing
│   └── skills/
│       ├── openclaw-*/             # existing
│       ├── feishu-configure/SKILL.md   # new (copied from plugin)
│       └── feishu-access/SKILL.md      # new (copied from plugin)
├── feishu/
│   ├── __init__.py
│   ├── channel_server.py           # new (ported, stripped)
│   ├── channel.py                  # new (ported, stripped)
│   └── channel-instructions.md     # new (adapted for discussion/admin)
├── pyproject.toml                  # new
├── Makefile                        # new
└── .gitignore                      # modified
```

## cc-openclaw.sh Changes

1. Source `cc-openclaw.local.sh` instead of `autoservice.local.sh`.
2. Add channel-server pre-flight check: read `.channel-server.pid`, verify PID
   alive. If not running, print instructions and `exit 1`.
3. Export `CHANNEL_SERVER_PORT` from pidfile so channel.py can connect.
4. Keep `--dangerously-load-development-channels server:autoservice-channel`.
5. Keep MCP config pointing to `.claude/mcp.json`.

## cc-openclaw.local.sh Changes

Keep existing proxy and ADMIN_CHAT_ID. No credential duplication needed —
channel_server.py reads `.feishu-credentials.json` directly.

## .feishu-credentials.json

```json
{
  "app_id": "cli_a9563cc03d399cc9",
  "app_secret": "nKCtKpxgd6u9YBMfJHub2y2SJGrjWlPc"
}
```

Gitignored. Each developer creates their own.

## channel_server.py — Ported from AutoService-Cinnox

### Removed

- `autoservice.crm` imports (upsert_contact, log_message, increment_message_count)
- `autoservice.plugin_loader` imports
- `web_*` chat_id routing and UX event handling
- `business_mode` sales/support semantics

### Retained and adapted

- Feishu WebSocket connection + message receiving
- `.feishu-credentials.json` loading (env var fallback)
- Multi-instance WebSocket routing (exact/wildcard)
- Modes: `discussion` (default) / `admin` — switched via `/admin` and
  `/discussion` commands in chat
- Admin group commands: `/status`, `/help`, `/inject`, `/explain`
- ACK reaction mechanism (track + remove on reply)
- File download (image, file, audio, media)
- Startup notification to users in scope
- Identity system (`identity.yaml`)
- Random port binding with pidfile

### Port binding change

```python
# Instead of fixed port:
self._server = await websockets.serve(..., port=0, ...)
actual_port = self._server.sockets[0].getsockname()[1]
# Write pidfile
pidfile.write_text(f"{os.getpid()}:{actual_port}")
```

## channel.py — Ported from AutoService-Cinnox

### Removed

- Plugin loader and dynamic tool registration

### Retained

- `ChannelClient` WebSocket client (auto-reconnect)
- `reply` / `react` MCP tools
- `inject_message` notification injection
- Instructions hot-reload (channel-instructions.md + identity.yaml)
- Port discovery from pidfile

## channel-instructions.md

Adapted for cc-openclaw modes:

- `discussion` mode (default): standard conversation, limited permissions
- `admin` mode: full permissions, system access
- `routed_to` set: observation mode, do not reply

## Feishu Skills

Copied (not symlinked) from `~/.claude/plugins/cache/ezagent42/feishu/0.0.1/skills/`:

- `feishu-configure/SKILL.md` — credential setup and access policy status
- `feishu-access/SKILL.md` — pairing, allowlists, DM/group policy management

These are git-tracked so all developers get them on clone.

## pyproject.toml

Minimal dependencies for feishu integration:

```toml
[project]
name = "cc-openclaw"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "lark-oapi>=1.3.0",
    "mcp>=1.0.0",
    "anyio>=4.0",
    "websockets>=13.0",
    "pyyaml>=6.0",
]
```

## Makefile

```makefile
run-server:
    uv run python3 feishu/channel_server.py

run-channel:
    uv run python3 feishu/channel.py
```

## .claude/mcp.json

```json
{
  "mcpServers": {
    "autoservice-channel": {
      "command": "uv",
      "args": ["run", "python3", "feishu/channel.py"]
    }
  }
}
```

## .gitignore additions

```
.feishu-credentials.json
.channel-server.pid
.autoservice/
```
