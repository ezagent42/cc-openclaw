#!/usr/bin/env bash
# Stop hook — every ≥10 min of silence, ping Allen's Feishu with a recap
# of the most recent real activity.
#
# v2 (2026-07-02, Allen): STOP depending on a hand-fed progress.jsonl
# (which went stale the moment the agent stopped calling log-progress.sh —
# the hook then parroted a 05-23 entry for weeks). The recap is now derived
# from the LIVE session transcript: the last mcp__openclaw-channel__reply
# text(s) the agent actually sent. Self-sufficient — cannot go stale.
#
# Fire-and-forget: ANY failure → exit 0 (never block Claude). Silent.

set -uo pipefail

PROJECT="/Users/h2oslabs/cc-openclaw"
STATE_DIR="${PROJECT}/.claude/state"
LAST="${STATE_DIR}/last_feishu_progress.ts"
CHAT_ID="oc_d9b47511b085e9d5b66c4595b3ef9bb9"
THRESHOLD_SEC=600   # 10 min
RECAP_CHARS=280     # how much of the last reply to echo back

command -v jq >/dev/null 2>&1 || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
[ -f "$LAST" ] || echo 0 > "$LAST"

# Stop-hook stdin carries transcript_path — the live session log.
INPUT=$(cat 2>/dev/null || echo '{}')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // .transcript // empty' 2>/dev/null)
[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || exit 0

NOW=$(date +%s)
PREV=$(cat "$LAST" 2>/dev/null || echo 0)
SINCE=$((NOW - PREV))

# Bootstrap: first ever run → set LAST=now, do not send.
if [ "$PREV" -eq 0 ]; then
  echo "$NOW" > "$LAST"
  exit 0
fi

# Too soon since last ping → stay silent.
[ "$SINCE" -lt "$THRESHOLD_SEC" ] && exit 0

ELAPSED_MIN=$((SINCE / 60))

# Recap = the LAST mcp__openclaw-channel__reply text the agent sent this
# session (the real, freshest thing Allen was told). Tolerant line parse.
RECAP=$(
  jq -Rrs '
    [ split("\n")[]
      | fromjson? // empty
      | select(.type == "assistant" and .message and .message.content)
      | .message.content[]?
      | select(.type == "tool_use" and .name == "mcp__openclaw-channel__reply")
      | (.input.text // empty)
    ] | last // empty
  ' "$TRANSCRIPT" 2>/dev/null
)

# Fallback: last substantive assistant text block if no reply tool found.
if [ -z "$RECAP" ]; then
  RECAP=$(
    jq -Rrs '
      [ split("\n")[]
        | fromjson? // empty
        | select(.type == "assistant" and .message and .message.content)
        | .message.content[]?
        | select(.type == "text" and (.text | length) >= 40)
        | .text
      ] | last // empty
    ' "$TRANSCRIPT" 2>/dev/null
  )
fi

[ -n "$RECAP" ] || RECAP="(长任务进行中 / 等待外部结果 — 无最新回复文本)"

# Truncate the recap to keep the ping short.
RECAP=$(printf '%s' "$RECAP" | head -c "$RECAP_CHARS")

TEXT="⏰ 已 ${ELAPSED_MIN} 分钟没有新的 Feishu 进度 —— 最近一次汇报:

${RECAP}…

(自动 hook · 静默 ≥10 分钟才触发 · 读实时 transcript,不会 stale)"

uv run --script "${PROJECT}/tools/feishu-notify.py" \
  --chat-id "$CHAT_ID" \
  --text "$TEXT" >/dev/null 2>&1 || true

# Update LAST even if send failed — don't retry-spam on every Stop.
echo "$NOW" > "$LAST"
exit 0
