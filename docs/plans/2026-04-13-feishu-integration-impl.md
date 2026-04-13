# Feishu Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Feishu messaging to cc-openclaw ��� a channel-server daemon receives Feishu messages and routes them to Claude Code instances via a WebSocket+MCP bridge.

**Architecture:** Three-process model — `channel_server.py` (standalone daemon, random port, pidfile discovery) connects to Feishu via `lark_oapi` and routes messages; `channel.py` (MCP server per CC instance) bridges channel-server to Claude Code; `cc-openclaw.sh` validates channel-server is running before launching CC.

**Tech Stack:** Python 3.11+, lark-oapi, mcp SDK, websockets, anyio, uv

---

### Task 1: Project scaffolding — pyproject.toml, Makefile, .gitignore

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `feishu/__init__.py`
- Modify: `.gitignore` (create if missing)

**Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

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

**Step 2: Create Makefile**

```makefile
.PHONY: run-server run-channel setup

run-server:
	uv run python3 feishu/channel_server.py

run-channel:
	uv run python3 feishu/channel.py

setup:
	uv sync
```

**Step 3: Create feishu/__init__.py**

Empty file.

**Step 4: Create or update .gitignore**

Add these lines:

```
.feishu-credentials.json
.channel-server.pid
.autoservice/
__pycache__/
*.pyc
.venv/
uv.lock
```

**Step 5: Run uv sync to verify dependencies resolve**

Run: `uv sync`
Expected: lock file created, dependencies installed

**Step 6: Commit**

```bash
git add pyproject.toml Makefile feishu/__init__.py .gitignore
git commit -m "feat: add project scaffolding for Feishu integration"
```

---

### Task 2: Create .feishu-credentials.json and .claude/mcp.json

**Files:**
- Create: `.feishu-credentials.json`
- Create: `.claude/mcp.json`

**Step 1: Create .feishu-credentials.json**

```json
{
  "app_id": "cli_a9563cc03d399cc9",
  "app_secret": "nKCtKpxgd6u9YBMfJHub2y2SJGrjWlPc"
}
```

This file is gitignored. Each developer creates their own.

**Step 2: Create .claude/mcp.json**

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

**Step 3: Commit mcp.json only (credentials are gitignored)**

```bash
git add .claude/mcp.json
git commit -m "feat: add MCP config for autoservice-channel"
```

---

### Task 3: Copy Feishu skills into .claude/skills/

**Files:**
- Create: `.claude/skills/feishu-configure/SKILL.md`
- Create: `.claude/skills/feishu-access/SKILL.md`

**Step 1: Copy configure skill**

Copy content from `~/.claude/plugins/cache/ezagent42/feishu/0.0.1/skills/configure/SKILL.md`
into `.claude/skills/feishu-configure/SKILL.md`. Exact content (no modifications).

**Step 2: Copy access skill**

Copy content from `~/.claude/plugins/cache/ezagent42/feishu/0.0.1/skills/access/SKILL.md`
into `.claude/skills/feishu-access/SKILL.md`. Exact content (no modifications).

**Step 3: Commit**

```bash
git add .claude/skills/feishu-configure/ .claude/skills/feishu-access/
git commit -m "feat: add feishu configure and access skills"
```

---

### Task 4: Port channel_server.py

Port from `/Users/h2oslabs/Workspace/AutoService-Cinnox/feishu/channel_server.py`.

**Files:**
- Create: `feishu/channel_server.py`

**Step 1: Write channel_server.py**

Port the entire file with these changes:

1. **Random port + pidfile**: Change `__init__` default `port` to `0`. After `websockets.serve()` in `start()`, read actual port from `self._server.sockets[0].getsockname()[1]`, write `{pid}:{port}` to `PROJECT_ROOT / ".channel-server.pid"`. In `stop()`, delete the pidfile.

2. **Remove CRM imports**: Delete all `from autoservice.crm import ...` lines and their call sites (`upsert_contact`, `log_message`, `increment_message_count`). In `_resolve_user`, remove the CRM upsert try/except block but keep the Feishu API user lookup.

3. **Remove web routing**: Delete `_handle_ux_event` method. In `_handle_client`, remove the `ux_event` branch. In `route_message`, remove the prefix_routes matching (used for `web_*`). In `_handle_reply`, remove the `web_*` branch. Remove `_find_prefix_instance`. In `_handle_register`, remove prefix pattern (`cid.endswith("*")`) handling. Remove `prefix_routes` from `__init__`, `_handle_register`, `_unregister`.

4. **Adapt modes**: Replace `"production"` → `"discussion"`, `"improve"` → `"admin"` throughout. In `on_message` callback, change `/improve` command to `/admin` and `/production` to `/discussion`. Change mode switch messages to Chinese equivalents for the new modes. Default `runtime_mode` is `"discussion"`.

5. **Remove business_mode**: Remove `business_mode` field from `Instance` dataclass. Remove `business_mode` from message dicts. Remove from `_handle_register`.

6. **Keep everything else**: Admin commands (`/status`, `/help`, `/inject`, `/explain`), startup notification, ACK reactions, file download, identity system, user resolution (without CRM), `_reply_feishu`, `_notify_admin`.

7. **Entry point**: Update banner text from "AutoService" to "OpenClaw". Read port from `0` not env var (the pidfile is the discovery mechanism). Keep `ADMIN_CHAT_ID` from env.

**Step 2: Verify syntax**

Run: `uv run python3 -c "import ast; ast.parse(open('feishu/channel_server.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add feishu/channel_server.py
git commit -m "feat: add channel_server.py — Feishu WebSocket daemon with random port"
```

---

### Task 5: Port channel.py

Port from `/Users/h2oslabs/Workspace/AutoService-Cinnox/feishu/channel.py`.

**Files:**
- Create: `feishu/channel.py`

**Step 1: Write channel.py**

Port the entire file with these changes:

1. **Remove plugin loader**: Delete `from autoservice.plugin_loader import discover` and all plugin-related code in `main()`. In `register_tools`, remove `plugin_tools` parameter — only register `reply` and `react` tools. Remove dynamic tool loop from `handle_call_tool`.

2. **Port discovery from pidfile**: In `main()`, instead of reading `CHANNEL_SERVER_PORT` env var, read `PROJECT_ROOT / ".channel-server.pid"`. Parse `pid:port` format. If pidfile missing or PID not alive, log error and exit. Construct `server_url` from discovered port.

   ```python
   pidfile = PROJECT_ROOT / ".channel-server.pid"
   if not pidfile.exists():
       log.error("channel-server not running — .channel-server.pid not found")
       sys.exit(1)
   parts = pidfile.read_text().strip().split(":")
   pid, port = int(parts[0]), int(parts[1])
   try:
       os.kill(pid, 0)
   except OSError:
       log.error("channel-server PID %d not alive", pid)
       sys.exit(1)
   server_url = f"ws://localhost:{port}"
   ```

3. **Adapt paths**: `PROJECT_ROOT` should point to the cc-openclaw root (parent of `feishu/`). `LOG_FILE` → `PROJECT_ROOT / ".autoservice" / "logs" / "channel.log"`. `INSTRUCTIONS_PATH` → `Path(__file__).parent / "channel-instructions.md"`. `IDENTITY_PATH` → `PROJECT_ROOT / ".autoservice" / "identity.yaml"`.

4. **Keep everything else**: `ChannelClient`, `inject_message`, `create_server`, `register_tools` (minus plugins), `_handle_reply`, `_handle_react`, instructions hot-reload.

5. **Adapt mode defaults**: Change `"production"` to `"discussion"` in `ChannelClient` default `runtime_mode`.

**Step 2: Verify syntax**

Run: `uv run python3 -c "import ast; ast.parse(open('feishu/channel.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add feishu/channel.py
git commit -m "feat: add channel.py — MCP bridge with pidfile port discovery"
```

---

### Task 6: Create channel-instructions.md

**Files:**
- Create: `feishu/channel-instructions.md`

**Step 1: Write channel-instructions.md**

```markdown
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
```

**Step 2: Commit**

```bash
git add feishu/channel-instructions.md
git commit -m "feat: add channel-instructions.md for discussion/admin modes"
```

---

### Task 7: Modify cc-openclaw.sh

**Files:**
- Modify: `cc-openclaw.sh`

**Step 1: Fix local.sh source path (line 38)**

Change:
```bash
[ -f "$SCRIPT_DIR/autoservice.local.sh" ] && source "$SCRIPT_DIR/autoservice.local.sh"
```
To:
```bash
[ -f "$SCRIPT_DIR/cc-openclaw.local.sh" ] && source "$SCRIPT_DIR/cc-openclaw.local.sh"
```

**Step 2: Add channel-server pre-flight check**

After the `tmux` pre-flight check (after line 72), add:

```bash
# ============================================
# Channel-server check
# ============================================

PIDFILE="$SCRIPT_DIR/.channel-server.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "❌ channel-server not running!"
    echo ""
    echo "No .channel-server.pid found."
    echo ""
    echo "Start it first:"
    echo "  cd $SCRIPT_DIR && make run-server"
    exit 1
fi

CS_PID=$(cut -d: -f1 "$PIDFILE")
CS_PORT=$(cut -d: -f2 "$PIDFILE")

if ! kill -0 "$CS_PID" 2>/dev/null; then
    echo "❌ channel-server not running!"
    echo ""
    echo "PID $CS_PID from .channel-server.pid is not alive."
    echo "The pidfile may be stale."
    echo ""
    echo "Start it first:"
    echo "  cd $SCRIPT_DIR && make run-server"
    rm -f "$PIDFILE"
    exit 1
fi

echo "✅ channel-server running (PID=$CS_PID, port=$CS_PORT)"
export CHANNEL_SERVER_PORT="$CS_PORT"
```

**Step 3: Commit**

```bash
git add cc-openclaw.sh
git commit -m "feat: cc-openclaw.sh loads local.sh, checks channel-server"
```

---

### Task 8: Smoke test — start channel-server

**Step 1: Start channel-server in background**

Run: `cd /Users/h2oslabs/cc-openclaw && make run-server &`

Verify:
- `.channel-server.pid` is created
- Contains `PID:PORT` format
- Banner prints with the random port

**Step 2: Verify pidfile**

Run: `cat .channel-server.pid`
Expected: something like `12345:54321`

**Step 3: Kill the server**

Run: `kill $(cut -d: -f1 .channel-server.pid)`
Verify: pidfile is cleaned up (deleted by server on graceful shutdown)

**Step 4: Test cc-openclaw.sh rejection when server not running**

Run: `./cc-openclaw.sh`
Expected: prints "channel-server not running" and exits with code 1

---

### Task 9: End-to-end verification

**Step 1: Start channel-server**

Run: `make run-server` (in a separate terminal/tmux pane)

Verify: Feishu WebSocket connects (check logs for "Feishu WS thread started")

**Step 2: Run cc-openclaw.sh**

Run: `./cc-openclaw.sh` and select mode 1 (Interactive)

Verify:
- "channel-server running" message appears with PID and port
- Claude Code starts with the autoservice-channel MCP server loaded
- No errors in channel.py logs (`.autoservice/logs/channel.log`)

**Step 3: Send a test message from Feishu**

Send a message to the bot from Feishu. Verify:
- ACK reaction appears on the message
- Message appears in Claude Code as a `<channel>` notification
- Claude Code can use `reply` tool to send a response back
- Response appears in Feishu chat
- ACK reaction is removed after reply

---

## Summary of commits

1. `feat: add project scaffolding for Feishu integration` — pyproject.toml, Makefile, feishu/__init__.py, .gitignore
2. `feat: add MCP config for autoservice-channel` — .claude/mcp.json
3. `feat: add feishu configure and access skills` — .claude/skills/feishu-*
4. `feat: add channel_server.py — Feishu WebSocket daemon with random port` — core server
5. `feat: add channel.py — MCP bridge with pidfile port discovery` — MCP bridge
6. `feat: add channel-instructions.md for discussion/admin modes` — instructions
7. `feat: cc-openclaw.sh loads local.sh, checks channel-server` — launcher fix
