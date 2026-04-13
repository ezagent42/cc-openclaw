# Design: OpenClaw Sidecar Routing Plugin

> Date: 2026-04-13
> Status: Approved
> Replaces: Fallback agent LLM-based curl approach

## Problem

Fallback agent uses LLM to execute curl commands → tool calls visible to user, LLM token waste.

## Solution

OpenClaw plugin using `before_dispatch` hook to intercept messages routed to fallback agent. Plugin calls Sidecar API directly (no LLM), handles provision/restore/deny, returns reply text.

## Hook: before_dispatch

- Fires after routing decision, before agent LLM invocation
- Has `sessionKey` (contains agent ID) and `senderId` (open_id)
- Returns `{ handled: true, text?: string }` to bypass agent and reply directly

## Logic

```
before_dispatch fires →
  if accountId != "shared" → skip (not our concern)
  if sessionKey doesn't contain "fallback" → skip (has dedicated agent)
  
  POST /api/v1/resolve-sender {open_id: senderId}
  
  action=provision → POST /api/v1/provision → reply "正在准备..."
  action=restore  → POST /api/v1/restore  → reply "已恢复..."
  action=deny     → reply deny message
  action=deny_silent → handled, no reply
  other           → skip (let fallback handle)
```

## Plugin Structure

```
openclaw-sidecar-plugin/
├── package.json
├── openclaw.plugin.json
├── index.js
└── README.md
```

## Fallback Agent Role

Retained as safety net. If plugin crashes or Sidecar is down, messages still reach fallback agent (degraded but not lost).
