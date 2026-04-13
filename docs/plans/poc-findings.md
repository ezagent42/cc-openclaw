# PoC Findings: OpenClaw config.patch and Peer Routing

> Date: 2026-04-13
> Status: In progress

## 1. config.patch RPC is WebSocket-only, not HTTP

**Critical finding:** The `config.get` and `config.patch` RPCs are exposed via **WebSocket RPC only**, not HTTP REST endpoints. HTTP paths `/api/config.get` and `/rpc/config.get` return 404.

**CLI wrapper:** `openclaw gateway call <method> --params '<json>' --json`

```bash
# Get config
openclaw gateway call config.get --json
# Returns: { path, exists, raw, parsed: {...config...}, hash: "..." }
# Note: field is "hash", not "baseHash"

# Patch config
openclaw gateway call config.patch --params '{"raw": "{ ... JSON5 ... }", "baseHash": "<hash>"}'
# "raw" is a JSON5 string, "baseHash" comes from config.get's "hash" field
```

**Rate limit:** 3 requests per 60 seconds per deviceId+clientIp. Returns `UNAVAILABLE` with `retryAfterMs` when exceeded.

## 2. Implementation approach: CLI subprocess

Instead of implementing a WebSocket RPC client, use `asyncio.create_subprocess_exec` to call the `openclaw` CLI. This:
- Avoids reimplementing the WebSocket auth/framing protocol
- Gets updates for free when OpenClaw upgrades
- Is reliable and well-tested (CLI is the official interface)

```python
async def config_get() -> dict:
    proc = await asyncio.create_subprocess_exec(
        "openclaw", "gateway", "call", "config.get", "--json",
        stdout=PIPE, stderr=PIPE)
    stdout, _ = await proc.communicate()
    return json.loads(stdout)
```

## 3. Bindings format (verified)

Current bindings use `accountId` matching:
```json
{"agentId": "linyilun", "match": {"channel": "feishu", "accountId": "linyilun"}}
```

Target peer binding format (from OpenClaw docs, NOT yet tested live):
```json
{"agentId": "u-shared-ou_alice", "match": {"channel": "feishu", "peer": {"kind": "direct", "id": "ou_alice"}}}
```

## 4. config.get response structure

```json
{
  "path": "/Users/h2oslabs/.openclaw/openclaw.json",
  "exists": true,
  "raw": null,
  "parsed": { ...full config... },
  "hash": "53a6617bfeb867bfd3c9c56c82195fb39c6cc585ea650fcf4cdcbb4672a2e2eb"
}
```

- Config is in `parsed` field (not top-level)
- Hash for baseHash is in `hash` field (not `baseHash`)

## 5. Still need to verify

- [ ] Peer binding actually routes correctly (add a test binding, send a DM)
- [ ] Fallback agent catch-all binding format
- [ ] config.patch with bindings array replacement behavior
- [ ] Hot reload timing after config.patch
