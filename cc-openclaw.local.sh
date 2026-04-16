#!/bin/bash
# autoservice.local.sh — local overrides (proxy, API keys, etc.)
# Sourced by autoservice.sh on startup. Tracked in git.

# ── Proxy ─────────────────────────────────────────────────────────────────
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897
export HTTP_PROXY=$http_proxy
export HTTPS_PROXY=$https_proxy
export no_proxy=localhost,127.0.0.1,::1,.feishu.cn,.larksuite.com,open.feishu.cn
export NO_PROXY=$no_proxy

# ── Feishu Admin Group ────────────────────────────────────────────────────
# 林懿伦 DM with OneSyn管理小龙虾
export ADMIN_CHAT_ID=oc_d9b47511b085e9d5b66c4595b3ef9bb9
export ADMIN_GROUP_CHAT_ID=oc_e75a27e1cb30a93a700014dd7d014b6c
