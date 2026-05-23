#!/usr/bin/env bash
# Stop hook — every ≥10 min, send a progress recap to Allen's Feishu.
#
# Reads recent entries from .claude/state/progress.jsonl (one JSON
# object per line, written by the agent when meaningful work happens)
# and POSTs a summary via tools/feishu-notify.py. Updates a last-sent
# timestamp so we don't spam the chat.
#
# Fire-and-forget: ANY failure → exit 0 (we never want the hook to
# block Claude). Silent stdout/stderr per the time-display hook
# convention.

set -uo pipefail

PROJECT="/Users/h2oslabs/cc-openclaw"
STATE_DIR="${PROJECT}/.claude/state"
LAST="${STATE_DIR}/last_feishu_progress.ts"
LOG="${STATE_DIR}/progress.jsonl"
CHAT_ID="oc_d9b47511b085e9d5b66c4595b3ef9bb9"
THRESHOLD_SEC=600   # 10 min
RECAP_N=3           # how many recent entries to include (Allen 2026-05-23: 3, reverse-chrono)

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
[ -f "$LAST" ] || echo 0 > "$LAST"
[ -f "$LOG" ]  || touch "$LOG"

NOW=$(date +%s)
PREV=$(cat "$LAST" 2>/dev/null || echo 0)
SINCE=$((NOW - PREV))

# Bootstrap: first ever run → set LAST=now, do not send
if [ "$PREV" -eq 0 ]; then
  echo "$NOW" > "$LAST"
  exit 0
fi

if [ "$SINCE" -lt "$THRESHOLD_SEC" ]; then
  exit 0  # silent, too soon
fi

ELAPSED_MIN=$((SINCE / 60))

# Build recap: last N lines of progress.jsonl IN REVERSE CHRONOLOGICAL
# ORDER (newest first), formatted as bullets. Tolerant: lines may or
# may not be valid JSON; if jq fails on a line we fall back to the raw
# line. Allen 2026-05-23: 3 entries max, newest at top.
if [ -s "$LOG" ]; then
  RECAP=$(tail -n "$RECAP_N" "$LOG" | tail -r 2>/dev/null || tail -n "$RECAP_N" "$LOG" | tac)
  RECAP=$(echo "$RECAP" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    if echo "$line" | jq -e . >/dev/null 2>&1; then
      ts=$(echo "$line" | jq -r '.ts // empty')
      type=$(echo "$line" | jq -r '.type // "note"')
      summary=$(echo "$line" | jq -r '.summary // empty')
      if [ -n "$summary" ]; then
        if [ -n "$ts" ]; then
          printf '• [%s] %s — %s\n' "$ts" "$type" "$summary"
        else
          printf '• %s — %s\n' "$type" "$summary"
        fi
      fi
    else
      printf '• %s\n' "$line"
    fi
  done)
else
  RECAP=""
fi

if [ -z "$RECAP" ]; then
  RECAP="(没有最近 progress 条目 — 可能在等待 / 长任务进行中)"
fi

TEXT="⏰ 已 ${ELAPSED_MIN} 分钟没有 Feishu 进度更新 —— 最近活动:

${RECAP}

(自动 hook · 每 ≥10 分钟 1 次 · /Users/h2oslabs/cc-openclaw/.claude/hooks/feishu-progress-check.sh)"

# Fire-and-forget send (never block Claude)
uv run --script "${PROJECT}/tools/feishu-notify.py" \
  --chat-id "$CHAT_ID" \
  --text "$TEXT" >/dev/null 2>&1 || true

# Update LAST even if send failed — we don't want to keep retrying on
# a broken send every Stop. Allen can `rm` the file to reset.
echo "$NOW" > "$LAST"
exit 0
