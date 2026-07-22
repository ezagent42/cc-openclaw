#!/usr/bin/env bash
# pr-auto-record — Stop hook. Auto-records PRs FORWARDED via the Feishu channel
# into the day's `together` inbox, so no forwarded PR is ever silently dropped.
#
# WHY a Stop hook (not a prompt hook): channel messages are MCP-INJECTED into the
# session (`channel_server/adapters/cc/channel.py:inject_message`), NOT typed
# prompts — so `UserPromptSubmit` never sees them. The transcript DOES contain
# them (as inbound `openclaw-channel` lines), so a Stop hook that scans the
# transcript tail is the reliable trigger — same pattern as channel-reply-check
# / feishu-progress-check.
#
# It captures a github `.../pull/<N>` URL that appears in an INBOUND channel line
# (filtered by the `openclaw-channel` source marker, so it records PRs Allen
# forwards — NOT every PR the assistant merely mentions in its own replies).
# New PRs (deduped by number) are appended to:
#   <esr-ng>/docs/together/<YYYY-MM-DD>/pr-inbox.md
# The coordinator curates pr-inbox → the committed `pr-ledger.md` (with each PR's
# disposition). This hook is the AUTO-CAPTURE half; curation stays human/cc.
#
# Fire-and-forget: ANY failure → exit 0 (never block a Stop).

set -uo pipefail

PROJECTS_DIR="/Users/h2oslabs/.claude/projects/-Users-h2oslabs-cc-openclaw"
TOGETHER_BASE="/Users/h2oslabs/Workspace/esr-ng/docs/together"
STATE_DIR="/Users/h2oslabs/cc-openclaw/.claude/state"
STATE="${STATE_DIR}/recorded_prs.txt"
TAIL_BYTES=3000000   # scan the last ~3 MB of the transcript

log() { printf '[%s] pr-auto-record: %s\n' "$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST')" "$*" >&2; }

command -v grep >/dev/null 2>&1 || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
touch "$STATE" 2>/dev/null || exit 0

# --- resolve transcript: stdin transcript_path (hook payload) wins, else newest jsonl ---
INPUT=$(cat 2>/dev/null || echo '{}')
TRANSCRIPT=""
if command -v jq >/dev/null 2>&1; then
  TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // .transcript // empty' 2>/dev/null)
fi
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  TRANSCRIPT=$(ls -t "$PROJECTS_DIR"/*.jsonl 2>/dev/null | head -1)
fi
[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || exit 0

# --- extract PR URLs from INBOUND channel lines only (openclaw-channel source) ---
# One JSON object per transcript line; an inbound channel message is a single line
# carrying both the `openclaw-channel` marker and the forwarded PR URL.
PR_URLS=$(
  tail -c "$TAIL_BYTES" "$TRANSCRIPT" 2>/dev/null \
    | grep -a 'openclaw-channel' 2>/dev/null \
    | grep -oaE 'github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+' 2>/dev/null \
    | sort -u
)
[ -n "$PR_URLS" ] || exit 0

DATE=$(TZ='Asia/Shanghai' date '+%Y-%m-%d')
TIME=$(TZ='Asia/Shanghai' date '+%H:%M CST')
INBOX_DIR="${TOGETHER_BASE}/${DATE}"
INBOX="${INBOX_DIR}/pr-inbox.md"

added=0
while IFS= read -r url; do
  [ -n "$url" ] || continue
  num="${url##*/pull/}"
  # only digits
  case "$num" in ''|*[!0-9]*) continue ;; esac
  # dedup by PR number (across days — a PR is recorded once)
  if grep -qxF "$num" "$STATE" 2>/dev/null; then
    continue
  fi
  # first NEW PR of the run → ensure the inbox file exists with a header
  if [ ! -f "$INBOX" ]; then
    mkdir -p "$INBOX_DIR" 2>/dev/null || exit 0
    {
      printf '# PR inbox (auto-captured) — %s\n\n' "$DATE"
      printf 'Forwarded PRs auto-recorded on arrival by `.claude/hooks/pr-auto-record.sh`.\n'
      printf 'The coordinator curates these into `pr-ledger.md` with each PR'"'"'s disposition.\n\n'
    } >> "$INBOX" 2>/dev/null || exit 0
  fi
  printf -- '- [%s] #%s — https://%s\n' "$TIME" "$num" "$url" >> "$INBOX" 2>/dev/null || continue
  echo "$num" >> "$STATE" 2>/dev/null
  added=$((added + 1))
done <<EOF
$PR_URLS
EOF

[ "$added" -gt 0 ] && log "recorded $added new PR(s) → $INBOX"
exit 0
