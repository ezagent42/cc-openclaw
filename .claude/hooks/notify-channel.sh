#!/usr/bin/env bash
# Hook: forward PreToolUse / PostToolUse notifications to channel-server via websocat.
# Runs without triggering the LLM — pure shell side-effect.

set -euo pipefail

# Require websocat + jq
command -v websocat &>/dev/null || exit 0
command -v jq       &>/dev/null || exit 0

# Read channel-server port from pidfile
PIDFILE="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/.channel-server.pid"
[ -f "$PIDFILE" ] || exit 0
PORT=$(cut -d: -f2 < "$PIDFILE")

CHAT_ID="${OC_CHAT_ID:-}"
[ -n "$CHAT_ID" ] || exit 0

INPUT=$(cat)

EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
TOOL=$(echo "$INPUT"  | jq -r '.tool_name // empty')

# Only notify for Bash and Agent tools
case "$TOOL" in
  Bash|Agent) ;;
  *) exit 0 ;;
esac

MAX=140

if [ "$EVENT" = "PreToolUse" ]; then
  if [ "$TOOL" = "Bash" ]; then
    CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
    MSG="⚙️ Running: ${CMD:0:$MAX}"
  elif [ "$TOOL" = "Agent" ]; then
    DESC=$(echo "$INPUT" | jq -r '.tool_input.description // empty')
    MSG="🔍 Agent: ${DESC:0:$MAX}"
  fi
elif [ "$EVENT" = "PostToolUse" ]; then
  if [ "$TOOL" = "Bash" ]; then
    # Extract stdout, truncate
    OUT=$(echo "$INPUT" | jq -r '.tool_response.stdout // .tool_response // empty' 2>/dev/null | head -c "$MAX")
    EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exitCode // .tool_response.exit_code // "?"' 2>/dev/null)
    if [ "$EXIT_CODE" = "0" ] || [ "$EXIT_CODE" = "?" ]; then
      MSG="✅ Done (exit ${EXIT_CODE}): ${OUT}"
    else
      MSG="❌ Failed (exit ${EXIT_CODE}): ${OUT}"
    fi
  elif [ "$TOOL" = "Agent" ]; then
    MSG="✅ Agent complete"
  fi
else
  exit 0
fi

[ -n "${MSG:-}" ] || exit 0

# Truncate final message
MSG="${MSG:0:$MAX}"

# Fire-and-forget: send to channel-server, 2s timeout
PAYLOAD=$(jq -nc --arg cid "$CHAT_ID" --arg txt "$MSG" '{"type":"reply","chat_id":$cid,"text":$txt}')
echo "$PAYLOAD" | websocat -n1 --no-close "ws://localhost:${PORT}" &>/dev/null &

exit 0
