#!/usr/bin/env bash
# Stop hook — L1 (Allen 2026-05-24): ensure the agent called
# `mcp__openclaw-channel__reply` if it produced substantive
# user-visible text in this turn. Prevents the "agent talked in TUI
# but didn't push to Feishu so user thinks comms broken" failure mode.
#
# v3 (2026-05-24, same day as v1+v2): switch line-number detection
# to REAL JSON PARSING via jq (was awk substring match — broke on
# tool_result payloads that happened to contain `"type":"text"`
# substring, causing false-positive blocks).
#
# Turn boundary = last user-INPUT entry. Distinguished from harness-
# injected user-role entries (task-notification, system-reminder,
# local-command-stdout etc.) which should NOT reset the turn.
#
# Companion to [[feedback_always_use_reply]] memory.

set -uo pipefail

MIN_SUBSTANTIVE_LEN=40

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // .transcript // empty' 2>/dev/null)

[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

# Reusable jq predicate: is this entry a real human/loop user input
# (not a harness injection)?
#
#   - array content + first block type == "text" → multimodal user msg
#   - string content NOT starting with a harness-wrapper tag
#       (<task-notification>, <system-reminder>, <local-command-*>)
#     → real user typing OR <channel ...> Feishu inbound OR
#       <command-name>/slash invocation OR <<autonomous-loop-dynamic>>
#     ALL of those should reset the turn.
IS_REAL_USER_INPUT='
  .type == "user" and .message and .message.content and (
    ((.message.content | type) == "array" and
     ((.message.content[0].type // "") == "text")) or
    ((.message.content | type) == "string" and
     ((.message.content | startswith("<task-notification>")) | not) and
     ((.message.content | startswith("<system-reminder>")) | not) and
     ((.message.content | startswith("<local-command-stdout>")) | not) and
     ((.message.content | startswith("<local-command-stderr>")) | not) and
     ((.message.content | startswith("<local-command-output>")) | not))
  )
'

# Line number of the LAST real user input (= start of this turn).
# Uses jq's `input_line_number` builtin to get the line number of the
# value just read by `inputs`.
LAST_USER_LINE=$(
  jq -nr --arg pred "$IS_REAL_USER_INPUT" '
    inputs |
    select(.type == "user" and .message and .message.content and (
      ((.message.content | type) == "array" and
       ((.message.content[0].type // "") == "text")) or
      ((.message.content | type) == "string" and
       ((.message.content | startswith("<task-notification>")) | not) and
       ((.message.content | startswith("<system-reminder>")) | not) and
       ((.message.content | startswith("<local-command-stdout>")) | not) and
       ((.message.content | startswith("<local-command-stderr>")) | not) and
       ((.message.content | startswith("<local-command-output>")) | not))
    )) |
    input_line_number
  ' "$TRANSCRIPT" 2>/dev/null | tail -1
)

START_LINE="${LAST_USER_LINE:-1}"

# Count substantive text blocks from assistant entries in this turn.
# Use --raw-input + fromjson?//empty so malformed lines don't kill jq.
SUBSTANTIVE_TEXT_COUNT=$(
  tail -n +"$START_LINE" "$TRANSCRIPT" |
  jq -Rrs --argjson min "$MIN_SUBSTANTIVE_LEN" '
    [ split("\n")[] |
      fromjson? // empty |
      select(.type == "assistant" and .message and .message.content) |
      .message.content[]? |
      select(.type == "text" and (.text | length) >= $min)
    ] | length
  ' 2>/dev/null
)
SUBSTANTIVE_TEXT_COUNT=${SUBSTANTIVE_TEXT_COUNT:-0}

# Count mcp__openclaw-channel__reply tool calls in this turn.
REPLY_COUNT=$(
  tail -n +"$START_LINE" "$TRANSCRIPT" |
  jq -Rrs '
    [ split("\n")[] |
      fromjson? // empty |
      select(.type == "assistant" and .message and .message.content) |
      .message.content[]? |
      select(.type == "tool_use" and .name == "mcp__openclaw-channel__reply")
    ] | length
  ' 2>/dev/null
)
REPLY_COUNT=${REPLY_COUNT:-0}

# Block iff substantive text exists but no reply was sent
if [ "$SUBSTANTIVE_TEXT_COUNT" -gt 0 ] && [ "$REPLY_COUNT" -eq 0 ]; then
  [ "${OC_SKIP_REPLY_CHECK:-0}" = "1" ] && exit 0

  echo "本轮有正文但未调 mcp__openclaw-channel__reply。" >&2
  echo "chat_id=oc_d9b47511b085e9d5b66c4595b3ef9bb9 — 请补一次。" >&2
  exit 2
fi

exit 0
