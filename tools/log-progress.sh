#!/usr/bin/env bash
# Append one progress entry to .claude/state/progress.jsonl.
# Used by the agent (or anyone) to drop a recap line that the
# Feishu-progress hook will read on its next ≥10-min check.
#
# Usage:
#   log-progress.sh <type> <summary>
#   log-progress.sh pr_merged "PR-A reconciler merged (#260, ~740 LOC deleted)"
#
# `type` is a short tag: pr_merged / subagent_dispatched /
# subagent_completed / decision_point / blocked / note / etc.

set -euo pipefail

PROJECT="/Users/h2oslabs/cc-openclaw"
LOG="${PROJECT}/.claude/state/progress.jsonl"
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] || touch "$LOG"

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <type> <summary>" >&2
  echo "  type: pr_merged / subagent_dispatched / subagent_completed / decision_point / blocked / note" >&2
  exit 2
fi

TYPE="$1"
shift
SUMMARY="$*"
TS=$(date '+%Y-%m-%d %H:%M:%S')

# Compact JSON, one object per line (jsonl)
printf '%s\n' "$(jq -nc \
  --arg ts "$TS" \
  --arg type "$TYPE" \
  --arg summary "$SUMMARY" \
  '{ts: $ts, type: $type, summary: $summary}')" >> "$LOG"
