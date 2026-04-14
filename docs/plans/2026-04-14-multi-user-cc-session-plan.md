# Multi-User CC Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform cc-openclaw from a single CC session to a multi-user system where each admin has an independent CC session with role-based permissions, managed via tmux windows.

**Architecture:** `roles/roles.yaml` defines users and roles. `cc-openclaw.sh` reads it to launch per-user CC sessions in tmux windows with role-specific settings. `channel_server.py` records DM chat_id mappings and replies to unrouted users. `channel.py` already reads `OPENCLAW_CHAT_ID` env for exact routing.

**Tech Stack:** Bash (cc-openclaw.sh), Python (channel_server.py), YAML (roles config), tmux

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `roles/roles.yaml` | Create | User-to-role mapping + role definitions |
| `roles/superadmin/settings.json` | Create | CC permissions for superadmin |
| `roles/superadmin/CLAUDE.md` | Create | Role instructions injected into CC |
| `roles/operator/settings.json` | Create | CC permissions for operator (restricted) |
| `roles/operator/CLAUDE.md` | Create | Role instructions for operator |
| `roles/monitor/settings.json` | Create | CC permissions for monitor (minimal) |
| `roles/monitor/CLAUDE.md` | Create | Role instructions for group monitor |
| `cc-openclaw.sh` | Modify | Add --user, --group, --list, --status, --stop, --help |
| `feishu/channel_server.py` | Modify | Chat_id mapping + unrouted DM reply |
| `.gitignore` | Modify | Add `.workspace/` |

---

### Task 1: Create roles directory and configuration files

**Files:**
- Create: `roles/roles.yaml`
- Create: `roles/superadmin/settings.json`
- Create: `roles/superadmin/CLAUDE.md`
- Create: `roles/operator/settings.json`
- Create: `roles/operator/CLAUDE.md`
- Create: `roles/monitor/settings.json`
- Create: `roles/monitor/CLAUDE.md`
- Modify: `.gitignore`

- [ ] **Step 1: Create roles/roles.yaml**

```yaml
# Role definitions and user-to-role mapping for 管理小龙虾 CC sessions.
# cc-openclaw.sh reads this file to determine permissions and chat_id routing.

roles:
  superadmin:
    description: "总管理员，完整权限"

  operator:
    description: "运营管理员，只读+数据查询"

  monitor:
    description: "管理群监控 agent，只读"

users:
  linyilun:
    role: superadmin
    open_id: "ou_6b11faf8e93aedfb9d3857b9cc23b9e7"
    display_name: "林懿伦"

# To add a new user:
# username:
#   role: operator
#   open_id: "ou_xxx"
#   display_name: "Name"

# Group agent (monitor) config
group:
  chat_id: "oc_e75a27e1cb30a93a700014dd7d014b6c"
  role: monitor
```

- [ ] **Step 2: Create roles/superadmin/settings.json**

```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true
}
```

Note: superadmin uses `--permission-mode bypassPermissions` (set in cc-openclaw.sh), so no tool restrictions needed in settings.

- [ ] **Step 3: Create roles/superadmin/CLAUDE.md**

```markdown
# Role: 总管理员 (superadmin)

You are the primary administrator for the cc-openclaw project. You have full access to all tools, code, services, and configurations.

Your responsibilities include:
- Code development and deployment
- Service management (channel_server, sidecar, OpenClaw Gateway)
- User and role management
- System monitoring and troubleshooting

You are operating in the cc-openclaw workspace at /Users/h2oslabs/cc-openclaw.
```

- [ ] **Step 4: Create roles/operator/settings.json**

```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true
}
```

Note: operator restrictions are enforced via CLAUDE.md instructions + `--permission-mode bypassPermissions` with hook-based guardrails. Claude Code's settings.json `permissions` field is for managed/team plans only.

- [ ] **Step 5: Create roles/operator/CLAUDE.md**

```markdown
# Role: 运营管理员 (operator)

You are an operations administrator with **read-only** access. You can query data and view status, but you MUST NOT modify code, push to git, or restart services.

## Allowed
- Read files, search code, view logs
- Query Sidecar API (agents, audit-log)
- Query Feishu API (group members, user info)
- Write files ONLY in `.workspace/{{username}}/`

## NOT Allowed
- Do NOT modify any source code files
- Do NOT run git push, git commit, or any git write operations
- Do NOT restart services (launchctl, make run-*)
- Do NOT modify openclaw.json or sidecar-config.yaml
- Do NOT run destructive commands (rm, kill, etc.)

If asked to do something outside your permissions, reply: "这个操作需要总管理员权限，请联系林懿伦。"
```

- [ ] **Step 6: Create roles/monitor/settings.json**

```json
{
  "enableAllProjectMcpServers": true,
  "channelsEnabled": true
}
```

- [ ] **Step 7: Create roles/monitor/CLAUDE.md**

```markdown
# Role: 管理群监控 (monitor)

You are the group agent for 龙虾管理群. You observe all messages in the admin group and provide:
- Status summaries when asked
- Task tracking and progress reporting
- Alert forwarding to relevant administrators

## Behavior
- Be concise and factual in group messages
- Summarize, don't repeat full content
- When someone asks about system status, query the Sidecar API
- Do NOT modify any files or run any commands that change state
- Do NOT respond to messages not directed at you (no @ mention)

## NOT Allowed
- Do NOT modify any files
- Do NOT run any shell commands that change state
- Do NOT push to git
```

- [ ] **Step 8: Add .workspace/ to .gitignore**

Append to `.gitignore`:
```
.workspace/
```

- [ ] **Step 9: Create .workspace/ directory**

```bash
mkdir -p .workspace
```

- [ ] **Step 10: Commit**

```bash
git add roles/ .gitignore
git commit -m "feat: add roles directory with superadmin/operator/monitor configurations"
```

---

### Task 2: Add chat_id mapping to channel_server.py

**Files:**
- Modify: `feishu/channel_server.py`

The channel_server needs to:
1. Record `open_id → chat_id` mapping when it sees a DM for the first time
2. Reply to unrouted DMs from admin group members with "session 未启动" message
3. Silently drop unrouted DMs from non-members

- [ ] **Step 1: Add chat_id mapping persistence**

In `feishu/channel_server.py`, add a method to the `ChannelServer` class after the `__init__` method. The mapping file is `.workspace/chat_id_map.json`.

```python
# Add to ChannelServer class

CHAT_ID_MAP_PATH = Path(__file__).resolve().parent.parent / ".workspace" / "chat_id_map.json"

def _load_chat_id_map(self) -> dict[str, str]:
    """Load open_id → chat_id mapping from disk."""
    if self.CHAT_ID_MAP_PATH.exists():
        try:
            return json.loads(self.CHAT_ID_MAP_PATH.read_text())
        except Exception:
            pass
    return {}

def _save_chat_id_map(self):
    """Persist open_id → chat_id mapping to disk."""
    self.CHAT_ID_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    self.CHAT_ID_MAP_PATH.write_text(json.dumps(self._chat_id_map, indent=2))

def _record_chat_id(self, open_id: str, chat_id: str):
    """Record a DM chat_id for an open_id (first-time discovery)."""
    if open_id and chat_id and open_id not in self._chat_id_map:
        self._chat_id_map[open_id] = chat_id
        self._save_chat_id_map()
        log.info("Recorded chat_id mapping: %s → %s", open_id, chat_id)
```

- [ ] **Step 2: Initialize mapping in __init__ and record on message receipt**

In `ChannelServer.__init__`, add:
```python
self._chat_id_map = self._load_chat_id_map()
```

In the `on_message` callback inside `_run_feishu` (around where `chat_id` and `sender_id` are available), add after the sender_id/chat_id extraction:
```python
# Record DM chat_id mapping (for cc-openclaw.sh --user)
if chat_type == "p2p" and sender_id:
    self._record_chat_id(sender_id, chat_id)
```

Note: `chat_type` comes from `message.chat_type` — check the existing code for how to determine if a message is a DM (p2p) vs group.

- [ ] **Step 3: Add unrouted DM handling with admin group check**

Modify the message routing section in `_route_message`. Currently (around line 944):
```python
elif routed_instance is None and not self.wildcard_instances:
    log.warning("No route for chat_id=%s, message dropped", chat_id)
```

Replace with:
```python
elif routed_instance is None and not self.wildcard_instances:
    user = message.get("user", "")
    open_id = message.get("user_id", "")
    # Check if this is a DM from an admin group member
    if open_id and open_id in self._admin_group_members:
        log.info("Unrouted DM from admin member %s — session not started", user)
        text = "您的管理 session 尚未启动，请联系总管理员开启。"
        threading.Thread(
            target=self._reply_feishu,
            args=(chat_id, text),
            daemon=True,
        ).start()
    else:
        log.warning("No route for chat_id=%s, message dropped", chat_id)
```

- [ ] **Step 4: Load admin group members on startup**

In `_run_feishu`, after the Feishu client is created, add admin group member loading:

```python
# Load admin group members for unrouted DM handling
self._admin_group_members = set()
if self.admin_chat_id:
    try:
        admin_members_req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/im/v1/chats/{self.admin_chat_id}/members")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        resp = self._feishu_client.request(admin_members_req)
        if resp.success():
            data = json.loads(resp.raw.content)
            for m in data.get("data", {}).get("items", []):
                self._admin_group_members.add(m.get("member_id", ""))
            log.info("Loaded %d admin group members", len(self._admin_group_members))
    except Exception as e:
        log.warning("Failed to load admin group members: %s", e)
```

Note: The admin_chat_id here is the management group `oc_e75a27e1cb30a93a700014dd7d014b6c`, but the current `ADMIN_CHAT_ID` env var is set to 林懿伦's DM `oc_d9b47511...`. We need a separate env var `ADMIN_GROUP_CHAT_ID` for the group, or load it from roles.yaml. Simplest: add `ADMIN_GROUP_CHAT_ID` env var.

- [ ] **Step 5: Commit**

```bash
git add feishu/channel_server.py
git commit -m "feat(feishu): chat_id mapping + unrouted DM handling for admin members"
```

---

### Task 3: Rewrite cc-openclaw.sh with --user/--group/--help modes

**Files:**
- Modify: `cc-openclaw.sh`

This is the main change. The script needs to support new CLI modes while keeping the existing interactive mode as default.

- [ ] **Step 1: Add YAML parser helper function**

Bash can't parse YAML natively. Add a minimal parser using Python:

```bash
# Near the top of cc-openclaw.sh, after sourcing shell config

parse_roles_yaml() {
    # Reads roles/roles.yaml and outputs key=value pairs for a given user
    local user="$1"
    uv run python3 -c "
import yaml, sys, json
with open('roles/roles.yaml') as f:
    data = yaml.safe_load(f)
user = data.get('users', {}).get('$user')
if user:
    print(f'ROLE={user[\"role\"]}')
    print(f'OPEN_ID={user[\"open_id\"]}')
    print(f'DISPLAY_NAME={user.get(\"display_name\", \"$user\")}')
else:
    sys.exit(1)
" 2>/dev/null
}

get_group_config() {
    uv run python3 -c "
import yaml
with open('roles/roles.yaml') as f:
    data = yaml.safe_load(f)
group = data.get('group', {})
print(f'GROUP_CHAT_ID={group.get(\"chat_id\", \"\")}')
print(f'GROUP_ROLE={group.get(\"role\", \"monitor\")}')
" 2>/dev/null
}

list_users() {
    uv run python3 -c "
import yaml
with open('roles/roles.yaml') as f:
    data = yaml.safe_load(f)
users = data.get('users', {})
for name, info in users.items():
    print(f'  {name:20s} role={info[\"role\"]:15s} {info.get(\"display_name\", \"\")}')
" 2>/dev/null
}

get_chat_id_for_user() {
    local open_id="$1"
    uv run python3 -c "
import json, sys
try:
    with open('.workspace/chat_id_map.json') as f:
        m = json.load(f)
    cid = m.get('$open_id', '')
    if cid:
        print(cid)
    else:
        sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null
}
```

- [ ] **Step 2: Add argument parsing**

Replace the existing mode selection logic at the top of cc-openclaw.sh. Keep the existing interactive mode as the default when no arguments are given.

```bash
# --- Argument parsing ---
ACTION=""
TARGET_USER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            ACTION="user"
            TARGET_USER="$2"
            shift 2
            ;;
        --group)
            ACTION="group"
            shift
            ;;
        --list)
            ACTION="list"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --stop)
            ACTION="stop"
            TARGET_USER="$2"
            shift 2
            ;;
        --help|-h)
            ACTION="help"
            shift
            ;;
        *)
            # Unknown arg — fall through to interactive mode
            shift
            ;;
    esac
done
```

- [ ] **Step 3: Implement --help**

```bash
if [ "$ACTION" = "help" ]; then
    cat <<'HELP'
Usage: cc-openclaw.sh [OPTIONS]

Multi-user CC session manager for 管理小龙虾.

Options:
  --user <name>       启动指定用户的 CC session (从 roles/roles.yaml 查找角色)
  --group             启动管理群 monitor session
  --list              列出所有已配置的用户和角色
  --status            查看当前运行中的 CC session
  --stop <name>       停止指定用户的 CC session
  --help              显示此帮助信息

  (no arguments)      交互式选择模式 (现有行为)

Examples:
  cc-openclaw.sh --user linyilun    # 启动林懿伦的 superadmin session
  cc-openclaw.sh --group            # 启动管理群 monitor session
  cc-openclaw.sh --list             # 列出所有用户和角色
  cc-openclaw.sh --status           # 查看运行中的 session
  cc-openclaw.sh --stop linyilun    # 停止林懿伦的 session
HELP
    exit 0
fi
```

- [ ] **Step 4: Implement --list**

```bash
if [ "$ACTION" = "list" ]; then
    echo "📋 Configured users (roles/roles.yaml):"
    echo ""
    list_users
    echo ""
    echo "Group monitor:"
    eval "$(get_group_config)"
    echo "  chat_id=$GROUP_CHAT_ID  role=$GROUP_ROLE"
    exit 0
fi
```

- [ ] **Step 5: Implement --status**

```bash
if [ "$ACTION" = "status" ]; then
    SESSION_NAME="cc-openclaw"
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "📊 Running CC sessions (tmux session: $SESSION_NAME):"
        echo ""
        tmux list-windows -t "$SESSION_NAME" -F "  #{window_name} — #{window_activity_string}"
    else
        echo "No active tmux session '$SESSION_NAME'"
    fi
    exit 0
fi
```

- [ ] **Step 6: Implement --stop**

```bash
if [ "$ACTION" = "stop" ]; then
    if [ -z "$TARGET_USER" ]; then
        echo "❌ Usage: cc-openclaw.sh --stop <username>"
        exit 1
    fi
    SESSION_NAME="cc-openclaw"
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-window -t "$SESSION_NAME:$TARGET_USER" 2>/dev/null && \
            echo "✓ Stopped session for $TARGET_USER" || \
            echo "❌ No active session for $TARGET_USER"
    else
        echo "❌ No active tmux session"
    fi
    exit 0
fi
```

- [ ] **Step 7: Implement --user**

```bash
if [ "$ACTION" = "user" ]; then
    if [ -z "$TARGET_USER" ]; then
        echo "❌ Usage: cc-openclaw.sh --user <username>"
        exit 1
    fi

    # Parse role config
    ROLE_INFO=$(parse_roles_yaml "$TARGET_USER")
    if [ $? -ne 0 ]; then
        echo "❌ User '$TARGET_USER' not found in roles/roles.yaml"
        exit 1
    fi
    eval "$ROLE_INFO"

    echo "🦞 Starting CC session for $DISPLAY_NAME ($TARGET_USER)"
    echo "   Role: $ROLE"

    # Look up chat_id from mapping
    CHAT_ID=$(get_chat_id_for_user "$OPEN_ID")
    if [ -z "$CHAT_ID" ]; then
        echo "⚠️  No chat_id mapping found for $TARGET_USER."
        echo "   The user needs to DM 管理小龙虾 first to register their chat_id."
        echo "   Then retry this command."
        exit 1
    fi
    echo "   Chat ID: $CHAT_ID"

    # Ensure workspace directory
    mkdir -p ".workspace/$TARGET_USER"

    # Ensure tmux session exists
    SESSION_NAME="cc-openclaw"
    tmux has-session -t "$SESSION_NAME" 2>/dev/null || tmux new-session -d -s "$SESSION_NAME"

    # Check if window already exists
    if tmux list-windows -t "$SESSION_NAME" -F "#{window_name}" | grep -q "^${TARGET_USER}$"; then
        echo "⚠️  Session '$TARGET_USER' already running. Attaching..."
        tmux select-window -t "$SESSION_NAME:$TARGET_USER"
        tmux attach-session -t "$SESSION_NAME"
        exit 0
    fi

    # Ensure channel_server is running
    PIDFILE="$SCRIPT_DIR/.channel-server.pid"
    if [ ! -f "$PIDFILE" ]; then
        echo "❌ channel-server not running!"
        exit 1
    fi

    # Source local config (proxy, env vars)
    LOCAL_SH="$SCRIPT_DIR/cc-openclaw.local.sh"
    if [ -f "$LOCAL_SH" ]; then
        source "$LOCAL_SH"
    fi

    # Build claude command
    CLAUDE_CMD="OPENCLAW_CHAT_ID=$CHAT_ID OPENCLAW_USER=$TARGET_USER OPENCLAW_ROLE=$ROLE"
    CLAUDE_CMD="$CLAUDE_CMD claude --permission-mode bypassPermissions"
    CLAUDE_CMD="$CLAUDE_CMD --dangerously-load-development-channels server:openclaw-channel"
    CLAUDE_CMD="$CLAUDE_CMD --mcp-config .mcp.json"

    # Role-specific settings
    SETTINGS_FILE="roles/$ROLE/settings.json"
    if [ -f "$SETTINGS_FILE" ]; then
        CLAUDE_CMD="$CLAUDE_CMD --settings $SETTINGS_FILE"
    fi

    # Launch in tmux window with auto-Enter for channel confirmation
    tmux new-window -t "$SESSION_NAME" -n "$TARGET_USER" "$CLAUDE_CMD" \; \
        run-shell "sleep 3" \; \
        send-keys Enter

    echo "✓ Session started in tmux window '$TARGET_USER'"
    echo "   Attach: tmux attach -t $SESSION_NAME"
    exit 0
fi
```

- [ ] **Step 8: Implement --group**

```bash
if [ "$ACTION" = "group" ]; then
    eval "$(get_group_config)"

    if [ -z "$GROUP_CHAT_ID" ]; then
        echo "❌ No group.chat_id configured in roles/roles.yaml"
        exit 1
    fi

    TARGET_USER="monitor"
    ROLE="$GROUP_ROLE"
    CHAT_ID="$GROUP_CHAT_ID"

    echo "🦞 Starting group monitor session"
    echo "   Role: $ROLE"
    echo "   Chat ID: $CHAT_ID"

    mkdir -p ".workspace/$TARGET_USER"

    SESSION_NAME="cc-openclaw"
    tmux has-session -t "$SESSION_NAME" 2>/dev/null || tmux new-session -d -s "$SESSION_NAME"

    if tmux list-windows -t "$SESSION_NAME" -F "#{window_name}" | grep -q "^${TARGET_USER}$"; then
        echo "⚠️  Monitor session already running."
        exit 0
    fi

    PIDFILE="$SCRIPT_DIR/.channel-server.pid"
    if [ ! -f "$PIDFILE" ]; then
        echo "❌ channel-server not running!"
        exit 1
    fi

    LOCAL_SH="$SCRIPT_DIR/cc-openclaw.local.sh"
    if [ -f "$LOCAL_SH" ]; then
        source "$LOCAL_SH"
    fi

    CLAUDE_CMD="OPENCLAW_CHAT_ID=$CHAT_ID OPENCLAW_USER=$TARGET_USER OPENCLAW_ROLE=$ROLE"
    CLAUDE_CMD="$CLAUDE_CMD claude --permission-mode bypassPermissions"
    CLAUDE_CMD="$CLAUDE_CMD --dangerously-load-development-channels server:openclaw-channel"
    CLAUDE_CMD="$CLAUDE_CMD --mcp-config .mcp.json"

    SETTINGS_FILE="roles/$ROLE/settings.json"
    if [ -f "$SETTINGS_FILE" ]; then
        CLAUDE_CMD="$CLAUDE_CMD --settings $SETTINGS_FILE"
    fi

    tmux new-window -t "$SESSION_NAME" -n "$TARGET_USER" "$CLAUDE_CMD" \; \
        run-shell "sleep 3" \; \
        send-keys Enter

    echo "✓ Monitor session started in tmux window '$TARGET_USER'"
    exit 0
fi
```

- [ ] **Step 9: Keep existing interactive mode as default**

After all the `if [ "$ACTION" = ... ]` blocks, the existing interactive mode code continues to run when no `ACTION` is set (no arguments given). No changes needed to the existing interactive flow — it remains the default behavior.

- [ ] **Step 10: Commit**

```bash
git add cc-openclaw.sh
git commit -m "feat: add --user/--group/--list/--status/--stop/--help to cc-openclaw.sh"
```

---

### Task 4: Integration test

- [ ] **Step 1: Verify roles config**

```bash
# Should list linyilun
./cc-openclaw.sh --list
```

Expected:
```
📋 Configured users (roles/roles.yaml):

  linyilun             role=superadmin      林懿伦

Group monitor:
  chat_id=oc_e75a27e1cb30a93a700014dd7d014b6c  role=monitor
```

- [ ] **Step 2: Verify --help**

```bash
./cc-openclaw.sh --help
```

Expected: help text with all options.

- [ ] **Step 3: Verify --status**

```bash
./cc-openclaw.sh --status
```

Expected: lists current tmux windows (or "No active tmux session").

- [ ] **Step 4: Verify chat_id mapping**

```bash
cat .workspace/chat_id_map.json
```

Expected: should contain `ou_6b11faf8e... → oc_d9b47511...` (recorded by channel_server from existing DMs).

- [ ] **Step 5: Test --user launch**

```bash
./cc-openclaw.sh --user linyilun
```

Expected: creates tmux window "linyilun", launches claude with correct env vars, auto-presses Enter for channel confirmation.

- [ ] **Step 6: Verify routing**

After launching, DM 管理小龙虾 from 林懿伦 → should route to the "linyilun" CC session (not wildcard).

- [ ] **Step 7: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test adjustments for multi-user CC sessions"
```

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Roles directory + config files | roles/*, .gitignore |
| 2 | chat_id mapping + unrouted DM handling | feishu/channel_server.py |
| 3 | cc-openclaw.sh CLI modes | cc-openclaw.sh |
| 4 | Integration testing | (verification only) |
