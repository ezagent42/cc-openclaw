#!/bin/bash

# ============================================
# Claude Code launcher
#
# All modes run inside tmux for:
#   - SSH disconnect resilience (session keeps running)
#   - Seamless reattach from any terminal (./autoservice.sh)
#
# Modes:
#   [1] Interactive          — standard claude session
#   [2] Interactive+Worktree — isolated git branch for feature work
#   [3] Remote Control       — continue from phone/browser
# ============================================

# Source shell configuration for proper PATH setup
if [ -f "$HOME/.zprofile" ]; then
    source "$HOME/.zprofile" 2>/dev/null
fi
if [ -f "$HOME/.zshrc" ]; then
    export ZDOTDIR_BACKUP="$ZDOTDIR"
    source "$HOME/.zshrc" 2>/dev/null
fi
if [ -f "$HOME/.bash_profile" ]; then
    source "$HOME/.bash_profile" 2>/dev/null
fi
if [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc" 2>/dev/null
fi

# Ensure common paths are included as fallback (Homebrew on Apple Silicon / Intel, npm global)
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source user-local overrides (proxy, API keys, etc.)
[ -f "$SCRIPT_DIR/cc-openclaw.local.sh" ] && source "$SCRIPT_DIR/cc-openclaw.local.sh"

# Source MCP server secrets (API keys, tokens)
[ -f "$SCRIPT_DIR/.mcp.env" ] && set -a && source "$SCRIPT_DIR/.mcp.env" && set +a

# Feishu credentials for channel.py (loaded from .feishu-credentials.json)
# No lark-cli needed — uses lark_oapi SDK directly

# Get project name from directory (sanitize for tmux session name)
PROJECT_NAME=$(basename "$SCRIPT_DIR" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')

# Session prefix for this project
SESSION_PREFIX="claude-${PROJECT_NAME}"

# ============================================
# Pre-flight checks
# ============================================

if ! command -v claude &> /dev/null; then
    echo "❌ claude command not found!"
    echo ""
    echo "PATH: $PATH"
    echo ""
    echo "Please install Claude Code first:"
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi

if ! command -v tmux &> /dev/null; then
    echo "❌ tmux not found!"
    echo ""
    echo "Please install tmux first:"
    echo "  brew install tmux"
    exit 1
fi

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

# ============================================
# Agent Setup plugin bootstrap
# ============================================

AGENT_SETUP_MARKETPLACE="https://github.com/ezagent42/agent-setup"

# Register marketplace if not already registered
if ! grep -q '"agent-setup"' "$HOME/.claude/plugins/known_marketplaces.json" 2>/dev/null; then
    echo "📦 Registering agent-setup marketplace..."
    claude plugin marketplace add "$AGENT_SETUP_MARKETPLACE" 2>/dev/null || {
        echo "⚠️  Could not register marketplace. Run manually:"
        echo "  claude plugin marketplace add $AGENT_SETUP_MARKETPLACE"
    }
fi

# Install plugin if not installed for this project
PLUGIN_INSTALLED=false
if [ -f "$HOME/.claude/plugins/installed_plugins.json" ]; then
    PLUGIN_INSTALLED=$(python3 -c "
import json, sys
d = json.load(open('$HOME/.claude/plugins/installed_plugins.json'))
entries = d.get('plugins', {}).get('agent-setup@agent-setup', [])
print('true' if any(e.get('projectPath') == '$SCRIPT_DIR' for e in entries) else 'false')
" 2>/dev/null || echo "false")
fi

if [ "$PLUGIN_INSTALLED" != "true" ]; then
    echo "📦 Installing agent-setup plugin..."
    claude plugin install agent-setup@agent-setup --scope project 2>/dev/null || {
        echo "⚠️  Could not install agent-setup plugin. Run manually:"
        echo "  claude plugin marketplace add $AGENT_SETUP_MARKETPLACE"
        echo "  claude plugin install agent-setup@agent-setup --scope project"
    }
    echo ""
fi

# ============================================
# Flags per mode
#
# Interactive: supports --permission-mode, --mcp-config, --worktree
# Remote Control: only supports --verbose, --sandbox, --no-sandbox
# Permission mode is also set in .claude/settings.json (defaultMode)
# so remote-control sessions inherit it without needing a CLI flag.
# ============================================

INTERACTIVE_FLAGS="--permission-mode bypassPermissions"

# Load Feishu channel as development channel (MCP server name from .mcp.json)
INTERACTIVE_FLAGS="$INTERACTIVE_FLAGS --dangerously-load-development-channels server:autoservice-channel"

if [ -f "$SCRIPT_DIR/.claude/mcp.json" ]; then
    INTERACTIVE_FLAGS="$INTERACTIVE_FLAGS --mcp-config .claude/mcp.json"
fi

RC_FLAGS=""

# ============================================
# iTerm2 detection → tmux -CC (native integration)
#
# tmux -CC makes iTerm2 render tmux windows/panes as native
# tabs and splits. Scrolling, copy/paste, resizing all work
# natively. Over SSH, set SendEnv LC_TERMINAL in ~/.ssh/config
# on the client, and AcceptEnv LC_* in sshd_config on the server.
#
# Override: CLAUDE_TMUX_CLASSIC=1 ./autoservice.sh  (force plain tmux)
# ============================================

TMUX_CC=""
if [ "${CLAUDE_TMUX_CLASSIC:-}" != "1" ]; then
    if [ "$TERM_PROGRAM" = "iTerm.app" ] || \
       [ "$LC_TERMINAL" = "iTerm2" ] || \
       [ -n "$ITERM_SESSION_ID" ]; then
        TMUX_CC="-CC"
    fi
fi

# ============================================
# Shared: mode selection menu
# ============================================

show_mode_menu() {
    echo "  [1] Interactive (Recommended)"
    echo "  [2] Interactive + Worktree — isolated git branch"
    echo "  [3] Remote Control — continue from phone/browser"
}

# ============================================
# Outside tmux: session management + mode selection
# ============================================

if [ -z "$TMUX" ]; then

    echo "📂 Project: $(basename "$SCRIPT_DIR")"
    echo "📍 Directory: $SCRIPT_DIR"
    if [ -n "$TMUX_CC" ]; then
        echo "🍎 iTerm2 detected — using native tmux integration (tmux -CC)"
    fi
    echo ""

    # --- Check for existing sessions first ---
    EXISTING_SESSIONS=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E "^${SESSION_PREFIX}" || true)

    # Helper: attach to a session, detaching other clients by default
    # so window size adapts to the current terminal (not the old one).
    # tmux -CC (iTerm2) handles sizing independently, so skip -d.
    tmux_attach() {
        local target="$1"
        if [ -n "$TMUX_CC" ]; then
            exec tmux -CC attach -t "$target"
        else
            exec tmux attach -dt "$target"
        fi
    }

    if [ -n "$EXISTING_SESSIONS" ]; then
        SESSION_COUNT=$(echo "$EXISTING_SESSIONS" | wc -l | tr -d ' ')

        if [ "$SESSION_COUNT" -eq 1 ]; then
            INFO=$(tmux list-sessions -F '#{session_name}: #{session_windows} windows (created #{t:session_created})' 2>/dev/null | grep "^$EXISTING_SESSIONS:")
            CLIENTS=$(tmux list-clients -t "$EXISTING_SESSIONS" -F '#{client_name}' 2>/dev/null | wc -l | tr -d ' ')

            echo "📌 Found existing session: $INFO"
            if [ "$CLIENTS" -gt 0 ]; then
                echo "   ⚡ $CLIENTS client(s) currently attached (will be detached)"
            fi
            echo ""
            read -p "Attach to this session? [Y/n] " -n 1 -r
            echo ""

            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                tmux_attach "$EXISTING_SESSIONS"
            fi
            echo ""
        else
            echo "📌 Found multiple sessions for project '$PROJECT_NAME':"
            echo ""
            i=1
            declare -a SESSION_ARRAY
            while IFS= read -r session; do
                INFO=$(tmux list-sessions -F '#{session_name}: #{session_windows} windows (created #{t:session_created})' 2>/dev/null | grep "^$session:")
                CLIENTS=$(tmux list-clients -t "$session" -F '#{client_name}' 2>/dev/null | wc -l | tr -d ' ')
                ATTACHED=""
                if [ "$CLIENTS" -gt 0 ]; then
                    ATTACHED=" ⚡${CLIENTS} attached"
                fi
                echo "  [$i] $INFO$ATTACHED"
                SESSION_ARRAY[$i]="$session"
                ((i++))
            done <<< "$EXISTING_SESSIONS"
            echo "  [n] Create new session"
            echo ""
            read -p "Select session [1]: " -r CHOICE

            if [[ "$CHOICE" =~ ^[Nn]$ ]]; then
                : # Fall through to create new session
            elif [ -z "$CHOICE" ] || [ "$CHOICE" = "1" ]; then
                tmux_attach "${SESSION_ARRAY[1]}"
            elif [[ "$CHOICE" =~ ^[0-9]+$ ]] && [ "$CHOICE" -le "${#SESSION_ARRAY[@]}" ]; then
                tmux_attach "${SESSION_ARRAY[$CHOICE]}"
            else
                echo "Invalid choice, attaching to first session"
                tmux_attach "${SESSION_ARRAY[1]}"
            fi
            echo ""
        fi
    fi

    # --- Mode selection ---
    echo "Create new session:"
    show_mode_menu
    echo ""
    read -p "Mode [1]: " -r MODE
    MODE=${MODE:-1}

    if [ "$MODE" != "1" ] && [ "$MODE" != "2" ] && [ "$MODE" != "3" ]; then
        echo "❌ Invalid mode: $MODE"
        exit 1
    fi

    # --- Prompt for session name ---
    UUID_SHORT=$(uuidgen | cut -d'-' -f1 | tr '[:upper:]' '[:lower:]')
    case "$MODE" in
        1)
            DEFAULT_NAME="${SESSION_PREFIX}-$(date +%m%d)-${UUID_SHORT}"
            echo ""
            echo "Enter a session name."
            ;;
        2)
            DEFAULT_NAME="${SESSION_PREFIX}-$(date +%m%d)-${UUID_SHORT}"
            echo ""
            echo "Enter a session name (also used as worktree branch name)."
            echo "Examples: ${SESSION_PREFIX}-feat-auth, ${SESSION_PREFIX}-bugfix-login"
            ;;
        3)
            DEFAULT_NAME="${SESSION_PREFIX}-rc-$(date +%m%d)-${UUID_SHORT}"
            echo ""
            echo "Enter a session name for the remote control session."
            ;;
    esac
    echo ""
    read -p "Session name [$DEFAULT_NAME]: " -r SESSION_NAME
    SESSION_NAME=${SESSION_NAME:-$DEFAULT_NAME}
    echo ""

    # --- Enable tmux passthrough for iTerm2 escape sequences ---
    # Allows notifications (e.g., zchat notify_command) to reach
    # the outer terminal (iTerm2) through tmux, even over SSH.
    tmux set-option -g allow-passthrough on 2>/dev/null

    # --- Create tmux session (all modes go through tmux) ---
    echo "🚀 Creating tmux session: $SESSION_NAME"
    exec tmux $TMUX_CC new-session -s "$SESSION_NAME" "cd '$SCRIPT_DIR' && '$0' --_internal '$MODE' '$SESSION_NAME'"
fi

# ============================================
# Inside tmux: run Claude Code
# ============================================

cd "$SCRIPT_DIR"

CURRENT_SESSION=$(tmux display-message -p '#S')
echo "✅ Running in tmux session: $CURRENT_SESSION"
echo "📂 Working directory: $(pwd)"
echo ""

# Parse chat_id argument (first non-internal, non-mode arg)
# Usage: ./autoservice.sh oc_xxx  or  ./autoservice.sh  (defaults to wildcard)
if [ "$1" != "--_internal" ] && [ -n "$1" ] && [[ "$1" != [123] ]]; then
    export AUTOSERVICE_CHAT_ID="$1"
    shift
else
    export AUTOSERVICE_CHAT_ID="${AUTOSERVICE_CHAT_ID:-*}"
fi

# Parse internal arguments (passed from outer invocation)
RUN_MODE=""
SESSION_NAME=""
if [ "$1" = "--_internal" ]; then
    RUN_MODE="${2:-}"
    SESSION_NAME="${3:-}"
fi

# If no internal args, user ran ./autoservice.sh from a new tmux window — ask interactively
if [ -z "$RUN_MODE" ]; then
    echo "New session in existing tmux:"
    show_mode_menu
    echo ""
    read -p "Mode [1]: " -r RUN_MODE
    RUN_MODE=${RUN_MODE:-1}

    if [ "$RUN_MODE" != "1" ] && [ "$RUN_MODE" != "2" ] && [ "$RUN_MODE" != "3" ]; then
        echo "❌ Invalid mode: $RUN_MODE"
        exit 1
    fi

    UUID_SHORT=$(uuidgen | cut -d'-' -f1 | tr '[:upper:]' '[:lower:]')
    case "$RUN_MODE" in
        1)
            # No extra name needed for simple interactive
            SESSION_NAME=""
            ;;
        2)
            DEFAULT_NAME="${SESSION_PREFIX}-$(date +%m%d)-${UUID_SHORT}"
            echo ""
            echo "Enter a worktree/branch name."
            echo "Examples: feat-auth, bugfix-login, refactor-api"
            echo ""
            read -p "Name [$DEFAULT_NAME]: " -r SESSION_NAME
            SESSION_NAME=${SESSION_NAME:-$DEFAULT_NAME}
            ;;
        3)
            DEFAULT_NAME="rc-$(date +%m%d)-${UUID_SHORT}"
            echo ""
            echo "Enter a session label."
            echo ""
            read -p "Name [$DEFAULT_NAME]: " -r SESSION_NAME
            SESSION_NAME=${SESSION_NAME:-$DEFAULT_NAME}
            ;;
    esac
    echo ""
fi

echo "💡 Tips:"
echo "   - Reattach after disconnect: ./autoservice.sh"
if [ -n "$TMUX_CC" ]; then
    echo "   (iTerm2 native integration active)"
    echo "   - New tab:     Cmd+T"
    echo "   - Split horiz: Cmd+Shift+D"
    echo "   - Split vert:  Cmd+D"
    echo "   - Switch pane: Cmd+[ / Cmd+]"
    echo "   - Close pane:  Cmd+W"
    echo "   - Scroll:      mouse/trackpad (native)"
    echo "   - Copy/paste:  Cmd+C / Cmd+V"
    echo "   - Detach:      close iTerm2 window (session keeps running)"
else
    echo "   - Detach (keep running): Ctrl+b, d"
    echo "   - New window: Ctrl+b, c  →  ./autoservice.sh"
    echo "   - Split pane: Ctrl+b, %  or  Ctrl+b, \""
    echo "   - Switch pane/window: Ctrl+b, arrow / Ctrl+b, n/p"
    echo "   - Zoom pane:  Ctrl+b, z"
    echo "   - Scroll mode: Ctrl+b, [  (exit: q)"
fi
echo ""

# --- Mode 1: Interactive (standard) ---
if [ "$RUN_MODE" = "1" ]; then
    echo "🔧 Mode: Interactive"
    echo ""
    claude $INTERACTIVE_FLAGS

# --- Mode 2: Interactive + Worktree ---
elif [ "$RUN_MODE" = "2" ]; then
    echo "🔧 Mode: Interactive + Worktree"
    echo ""
    if [ -n "$SESSION_NAME" ]; then
        claude --worktree "$SESSION_NAME" $INTERACTIVE_FLAGS
    else
        claude --worktree $INTERACTIVE_FLAGS
    fi

# --- Mode 3: Remote Control ---
elif [ "$RUN_MODE" = "3" ]; then
    echo "🌐 Mode: Remote Control"
    echo "   Connect from: claude.ai/code or Claude mobile app"
    echo "   Press spacebar to show QR code for mobile"
    echo ""
    claude remote-control $RC_FLAGS
fi

EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "⚠️  Claude exited with code: $EXIT_CODE"
fi
echo ""
echo "Session ended. Press any key to close, or Ctrl+b d to keep tmux session."
read -n 1
