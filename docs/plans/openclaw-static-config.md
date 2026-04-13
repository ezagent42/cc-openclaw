# OpenClaw Static Configuration for Sidecar

This documents the static configuration that must be added to `openclaw.json`
before the Sidecar can operate.

## Prerequisites

- Shared Feishu App: `cli_a9563cc03d399cc9` (already configured)
- Admin group chat_id: `oc_f871082d96debf59ff47981f6fcce1d7`
- Sidecar API: `http://127.0.0.1:18791`

## 1. Add fallback agent

The fallback agent catches all DM messages that don't match any peer binding.
It must be the LAST binding in the array (lowest priority).

```json5
// In agents section:
"fallback": {
    "agentDir": "~/.openclaw/agents/fallback/agent",
    "workspace": "~/.openclaw/agents/fallback/workspace"
    // model: inherits default
}
```

```json5
// In bindings array (MUST be last):
{
    "agentId": "fallback",
    "match": { "channel": "feishu" }  // no peer = catch-all
}
```

## 2. Add admin agent

The admin agent is bound to the admin group chat.

```json5
// In agents section:
"admin": {
    "agentDir": "~/.openclaw/agents/admin/agent",
    "workspace": "~/.openclaw/agents/admin/workspace"
}
```

```json5
// In bindings array (before fallback):
{
    "agentId": "admin",
    "match": {
        "channel": "feishu",
        "peer": { "kind": "group", "id": "oc_f871082d96debf59ff47981f6fcce1d7" }
    }
}
```

## 3. Set dmPolicy to "open"

```json5
// In channels.feishu (default account):
"dmPolicy": "open"  // Let all messages through, fallback handles unauthorized
```

## 4. Agent workspace setup

Create the directories and copy SOUL.md templates:

```bash
mkdir -p ~/.openclaw/agents/fallback/{agent,workspace}
mkdir -p ~/.openclaw/agents/admin/{agent,workspace}
cp sidecar/templates/fallback-agent/SOUL.md ~/.openclaw/agents/fallback/workspace/
cp sidecar/templates/admin-agent/SOUL.md ~/.openclaw/agents/admin/workspace/
```

## 5. Fallback agent tools

The fallback agent needs MCP tools or shell tools configured to call the Sidecar API.
This can be done via:
- A custom MCP server that wraps the Sidecar API
- Shell tool execution (`curl` commands)
- Or OpenClaw's native tool system if it supports HTTP calls

This is a PoC verification item — determine the best approach.
