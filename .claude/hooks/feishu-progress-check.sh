#!/usr/bin/env bash
# Feishu progress HEARTBEAT — launchd/cron-driven, fires every 30 min.
#
# v3 (2026-07-22, Allen): STOP depending on the flaky Claude Code Stop-hook
# (it "sometimes didn't fire, sometimes fired with a stale recap"). This
# script is now driven by a launchd timer — deploy/ai.openclaw.feishu-heartbeat.plist,
# StartInterval 1800 — so the 30-min tick is reliable on its own, independent
# of any Claude session lifecycle. Activate once with `make install-heartbeat`.
#
# A launchd timer has NO Claude session env, so this script resolves both the
# chat_id (hard-coded, same as ADMIN_CHAT_ID) and the current transcript itself:
# the newest *.jsonl under the project's transcript dir. The recap is derived
# from the LIVE transcript TAIL (the file can be >700 MB — never read it whole),
# so it can never go stale. Every message carries the wall-clock trigger time.
#
# Still tolerates a Stop-hook: if stdin pipes a transcript_path, that wins.
#
# Guards (all skip-and-log; bypass every one with FORCE=1 for a manual test):
#   - session ended     : transcript untouched > STALE_SESSION_SEC   → skip
#   - recent progress    : last Feishu reply younger than SILENCE_SEC → skip
#   - idempotency        : posted within MIN_INTERVAL_SEC             → skip
# Each run writes one line to StandardOutPath so "did it fire / why not" is
# always answerable (tail -f ~/.openclaw/logs/feishu-heartbeat.log).
#
# Fire-and-forget: ANY failure → exit 0 (never block anything).

set -uo pipefail

PROJECT="/Users/h2oslabs/cc-openclaw"
STATE_DIR="${PROJECT}/.claude/state"
LAST="${STATE_DIR}/last_feishu_progress.ts"
CHAT_ID="oc_d9b47511b085e9d5b66c4595b3ef9bb9"
PROJECTS_DIR="/Users/h2oslabs/.claude/projects/-Users-h2oslabs-cc-openclaw"

SILENCE_SEC=1200        # 20 min: last real reply newer than this → recent progress → skip
STALE_SESSION_SEC=7200  # 2 h: transcript untouched longer → session ended → skip.
                        #   Generous on purpose: this repo runs 60-90 min silent
                        #   tool dispatches (kimi/codex) that write nothing to the
                        #   transcript mid-run — a tighter bound would kill the
                        #   heartbeat exactly when a long task is running.
MIN_INTERVAL_SEC=1500   # 25 min: idempotency — never post twice within this window
                        #   (double-load / manual-test overlap can't double-post).
RECAP_CHARS=280         # how much of the last reply to echo back
TAIL_BYTES=6000000      # read only the last ~6 MB of the transcript for the recap

FORCE="${FORCE:-0}"

log() { printf '[%s] %s\n' "$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST')" "$*"; }

command -v jq >/dev/null 2>&1 || { log "abort: jq not on PATH ($PATH)"; exit 0; }
command -v uv >/dev/null 2>&1 || { log "abort: uv not on PATH ($PATH)"; exit 0; }
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
[ -f "$LAST" ] || echo 0 > "$LAST"

NOW=$(date +%s)

# --- resolve transcript: stdin transcript_path (Stop-hook) wins, else newest jsonl ---
INPUT=$(cat 2>/dev/null || echo '{}')
TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // .transcript // empty' 2>/dev/null)
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  TRANSCRIPT=$(ls -t "$PROJECTS_DIR"/*.jsonl 2>/dev/null | head -1)
fi
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  log "skip: no transcript under $PROJECTS_DIR"
  exit 0
fi

# --- guard: session ended? (transcript untouched for a long time) ---
MTIME=$(stat -f %m "$TRANSCRIPT" 2>/dev/null || echo 0)
SESSION_IDLE=$((NOW - MTIME))
if [ "$FORCE" != "1" ] && [ "$SESSION_IDLE" -gt "$STALE_SESSION_SEC" ]; then
  log "skip: session idle ${SESSION_IDLE}s (> ${STALE_SESSION_SEC}s) — no active session"
  exit 0
fi

# --- guard: idempotency (never post twice within MIN_INTERVAL) ---
PREV=$(cat "$LAST" 2>/dev/null || echo 0)
SINCE_POST=$((NOW - PREV))
if [ "$FORCE" != "1" ] && [ "$SINCE_POST" -lt "$MIN_INTERVAL_SEC" ]; then
  log "skip: last heartbeat ${SINCE_POST}s ago (< ${MIN_INTERVAL_SEC}s)"
  exit 0
fi

# --- recap = last channel reply {text, ts} from the transcript TAIL ---
REPLY=$(
  tail -c "$TAIL_BYTES" "$TRANSCRIPT" 2>/dev/null | jq -Rrs '
    [ split("\n")[] | fromjson? // empty
      | select(.type == "assistant" and .message and .message.content)
      | . as $l
      | .message.content[]?
      | select(.type == "tool_use" and .name == "mcp__openclaw-channel__reply")
      | {ts: ($l.timestamp // ""), text: (.input.text // "")}
    ] | last // empty
  ' 2>/dev/null
)
RECAP=$(printf '%s' "$REPLY" | jq -r '.text // empty' 2>/dev/null)
REPLY_TS=$(printf '%s' "$REPLY" | jq -r '.ts // empty' 2>/dev/null)

# Fallback recap: last substantive assistant text block in the tail.
if [ -z "$RECAP" ]; then
  RECAP=$(
    tail -c "$TAIL_BYTES" "$TRANSCRIPT" 2>/dev/null | jq -Rrs '
      [ split("\n")[] | fromjson? // empty
        | select(.type == "assistant" and .message and .message.content)
        | .message.content[]?
        | select(.type == "text" and (.text | length) >= 40)
        | .text
      ] | last // empty
    ' 2>/dev/null
  )
fi

# Parse the reply timestamp (ISO-8601 UTC, e.g. 2026-07-22T07:39:35.420Z) → epoch.
REPLY_EPOCH=0
if [ -n "$REPLY_TS" ]; then
  CLEAN_TS="${REPLY_TS%%.*}"   # drop .fractional
  CLEAN_TS="${CLEAN_TS%Z}"     # drop trailing Z (no-fraction case)
  REPLY_EPOCH=$(date -u -j -f "%Y-%m-%dT%H:%M:%S" "$CLEAN_TS" +%s 2>/dev/null || echo 0)
fi

# --- guard: recent real progress? (last reply younger than SILENCE) → skip ---
if [ "$FORCE" != "1" ] && [ "$REPLY_EPOCH" -gt 0 ]; then
  SINCE_REPLY=$((NOW - REPLY_EPOCH))
  if [ "$SINCE_REPLY" -lt "$SILENCE_SEC" ]; then
    log "skip: last Feishu reply ${SINCE_REPLY}s ago (< ${SILENCE_SEC}s) — recent progress"
    exit 0
  fi
fi

# --- compose ---
[ -n "$RECAP" ] || RECAP="(长任务进行中 / 等待外部结果 — 无最新回复文本)"
RECAP=$(printf '%s' "$RECAP" | head -c "$RECAP_CHARS")

TRIGGER_TIME=$(TZ='Asia/Shanghai' date '+%m-%d %H:%M CST')

if [ "$REPLY_EPOCH" -gt 0 ]; then
  ELAPSED_MIN=$(( (NOW - REPLY_EPOCH) / 60 ))
  HEAD_LINE="⏰ [${TRIGGER_TIME}] 已 ${ELAPSED_MIN} 分钟没有新的 Feishu 进度 —— 最近一次汇报:"
else
  HEAD_LINE="⏰ [${TRIGGER_TIME}] 长时间没有新的 Feishu 进度 —— 最近一次汇报:"
fi

TEXT="${HEAD_LINE}

${RECAP}…

(自动心跳 · 触发于 ${TRIGGER_TIME} · 30 分钟定时 launchd · 读实时 transcript,不会 stale)"

# --- send ---
REPLY_AGE=-1
[ "$REPLY_EPOCH" -gt 0 ] && REPLY_AGE=$((NOW - REPLY_EPOCH))
if uv run --script "${PROJECT}/tools/feishu-notify.py" \
     --chat-id "$CHAT_ID" --text "$TEXT" >/dev/null 2>&1; then
  echo "$NOW" > "$LAST"
  log "sent: heartbeat (recap ${#RECAP}B, last_reply_age ${REPLY_AGE}s, force=${FORCE})"
else
  # Advance LAST even on failure so a transient error doesn't retry-spam next tick.
  echo "$NOW" > "$LAST"
  log "error: feishu-notify failed (advanced last_post to avoid retry-spam)"
fi
exit 0
