#!/bin/bash
# autoservice.local.sh — local overrides (proxy, API keys, etc.)
# Sourced by autoservice.sh on startup. Tracked in git.

# -- OpenRouter GLM ---------------------------------------------------------------
SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
OPENROUTER_ENV_FILE="${OPENROUTER_ENV_FILE:-"$SCRIPT_DIR/.openrouter-cc.env"}"

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$OPENROUTER_ENV_FILE" ]; then
    set -a
    source "$OPENROUTER_ENV_FILE"
    set +a
fi

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "OPENROUTER_API_KEY is not set. Export it or create $OPENROUTER_ENV_FILE." >&2
    return 1 2>/dev/null || exit 1
fi

export ANTHROPIC_BASE_URL=https://openrouter.ai/api
export ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY"
export ANTHROPIC_API_KEY=
export ANTHROPIC_MODEL=z-ai/glm-5.2
export ANTHROPIC_DEFAULT_OPUS_MODEL=z-ai/glm-5.2
export ANTHROPIC_DEFAULT_SONNET_MODEL=z-ai/glm-5.2
export ANTHROPIC_DEFAULT_HAIKU_MODEL=z-ai/glm-5.2
export CLAUDE_CODE_SUBAGENT_MODEL=z-ai/glm-5.2
export CLAUDE_CODE_EFFORT_LEVEL=max

# ── Feishu Admin Group ────────────────────────────────────────────────────
# 林懿伦 DM with OneSyn管理小龙虾
export ADMIN_CHAT_ID=oc_d9b47511b085e9d5b66c4595b3ef9bb9
export ADMIN_GROUP_CHAT_ID=oc_e75a27e1cb30a93a700014dd7d014b6c
