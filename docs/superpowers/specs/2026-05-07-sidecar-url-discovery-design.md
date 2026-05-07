# Design: Robust Sidecar URL Discovery

> Date: 2026-05-07
> Status: Proposed
> Related: PR #21 (interim fix — bound sidecar to `cfg.api_port`)
> Blocks: removal of `SIDECAR_DEFAULT_URL` hardcode in plugin manifest

## Problem

The openclaw sidecar plugin (`openclaw-sidecar-plugin/index.js`) cannot reliably discover the sidecar HTTP API port. Symptom: plugin → sidecar `fetch` calls fail, plugin returns "系统维护中，请稍后重试" to every fallback message, blocking new-user provisioning end-to-end.

### Root cause

Plugin's `resolveSidecarUrl()` searches for `.sidecar.pid` relative to `process.cwd()`:

```js
const candidates = [
  resolve(process.cwd(), PIDFILE_NAME),        // ./.sidecar.pid
  resolve(process.cwd(), "..", PIDFILE_NAME),  // ../.sidecar.pid
];
```

The plugin runs inside the openclaw gateway process. Gateway is **not launchd-managed** (unlike sidecar/channel-server, both of which set `WorkingDirectory` in their plists). Empirically the gateway runs from `cwd=/`, so both candidates resolve to `/.sidecar.pid` and `/.sidecar.pid` (since `/..` is `/`) — both miss.

Plugin falls back to its hardcoded `SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791"`. PR #21 made sidecar bind to that exact port (via `cfg.api_port`), so production currently works — **but only because two independent constants happened to align**. Any drift (operator changes yaml's `api_port` to avoid conflict, repo gets renamed, gateway's startup mechanism changes) silently re-breaks the chain with the same opaque "系统维护中" symptom.

### Scope

Single sidecar instance per machine. Multi-instance dev side-by-side is **not** a goal — confirmed during brainstorming.

## Solution

Move the pidfile to a stable, machine-level absolute path: **`~/.openclaw/sidecar.pid`**.

This sits alongside `~/.openclaw/sidecar.sqlite` and the existing `agents/`, `archived/`, `logs/` directories — all of which are already machine-level state. Both writer (sidecar) and reader (plugin) hardcode this path; no cwd, project-root, or env-var dependencies.

```
~/.openclaw/                    ← machine-level openclaw state root
├── sidecar.sqlite              ← (existing)
├── sidecar.pid                 ← NEW: {"pid": ..., "port": ...}
├── agents/                     ← (existing)
├── archived/                   ← (existing)
└── logs/                       ← (existing)
```

The legacy `<project>/.sidecar.pid` is removed (no external readers; gitignore already covers).

## Architecture

```
[ launchd starts sidecar ]
    cwd=/Users/h2oslabs/cc-openclaw  (irrelevant — pidfile path is absolute)
    │
    ▼
sidecar/main.py
    1. mkdir -p ~/.openclaw  (idempotent)
    2. bind aiohttp server to cfg.api_port (e.g. 18791)
    3. write {"pid": ..., "port": ...} → ~/.openclaw/sidecar.pid.tmp
    4. os.replace(.tmp, ~/.openclaw/sidecar.pid)   ← atomic
    │
[ openclaw gateway started later, cwd=/ ]
    │
    ▼
openclaw-sidecar-plugin/index.js
    On EACH plugin call (not cached):
    1. read ~/.openclaw/sidecar.pid
    2. parse {port}
    3. return http://127.0.0.1:<port>
    4. fetch(`${url}/api/v1/resolve-sender`, ...)
```

Re-reading per call (rather than caching at plugin load) means sidecar restarts pick up automatically without bouncing the gateway.

## Components

### `sidecar/main.py`

Replace pidfile path computation. Currently:

```python
pidfile_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), ".sidecar.pid")
```

Becomes:

```python
pidfile_dir = os.path.expanduser("~/.openclaw")
os.makedirs(pidfile_dir, exist_ok=True)
pidfile_path = os.path.join(pidfile_dir, "sidecar.pid")
```

Atomic write (replaces current `open(..., "w").write(...)`):

```python
import tempfile
# `dir=pidfile_dir` is LOAD-BEARING, not incidental: os.replace is only
# atomic when src and dst share a filesystem. Default tempfile dir
# ($TMPDIR → /var/folders/.../T on macOS) might be on a different
# volume; never use the default here.
fd, tmp_path = tempfile.mkstemp(prefix=".sidecar.pid.", dir=pidfile_dir)
try:
    with os.fdopen(fd, "w") as f:
        f.write(_json.dumps({"pid": os.getpid(), "port": actual_port}))
    os.replace(tmp_path, pidfile_path)
except Exception:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    raise
```

**Implementer note**: the previous `config_path`-derived pidfile line at `sidecar/main.py:111-114` must be **deleted**, not commented out — leaving it as dead code is a long-term trap.

### `openclaw-sidecar-plugin/index.js`

Replace `resolveSidecarUrl` body. Currently does cwd-based search with two relative candidates. Becomes single absolute path:

```js
import { homedir } from "node:os";
import { join } from "node:path";

const PIDFILE_PATH = join(homedir(), ".openclaw", "sidecar.pid");

function resolveSidecarUrl(configUrl) {
  try {
    const content = readFileSync(PIDFILE_PATH, "utf-8");
    const { port } = JSON.parse(content);
    if (port) return `http://127.0.0.1:${port}`;
  } catch {
    /* pidfile missing or unparseable — fall through */
  }
  return configUrl || SIDECAR_DEFAULT_URL;
}
```

Removed: `dirname` import, `process.cwd()` usage, the candidates loop. The `configUrl || SIDECAR_DEFAULT_URL` fallback is retained as defensive last-resort.

### `openclaw-sidecar-plugin/openclaw.plugin.json`

Update `sidecarUrl` description to reflect that it's no longer the primary discovery mechanism:

```json
"sidecarUrl": {
  "type": "string",
  "description": "Emergency override. Pidfile at ~/.openclaw/sidecar.pid wins when present — this value is ONLY used when the pidfile is missing/malformed. Set only for non-standard topologies (e.g. remote testing).",
  "default": "http://127.0.0.1:18791"
}
```

**Constraint**: this `default` value MUST track `sidecar/config.py:44`'s `api_port: int = 18791` default. If either changes, both must change in lockstep — otherwise we recreate the original alignment-by-coincidence problem in the fresh-install / pre-pidfile-write window. Document this as a comment in both files.

### `~/.openclaw/openclaw.json` (operator-side cleanup, not in this PR)

The currently-deployed `openclaw.json` has `sidecarUrl: "http://127.0.0.1:18791"` set explicitly (line 866). With the new code that value becomes operationally inert (pidfile wins) but readers will reasonably assume it's load-bearing.

**Action item** (operator-side, post-deploy): clear `sidecarUrl` from `openclaw.json` so the field is unset by default. Out of scope for this PR (operator config, not source-controlled), but the migration checklist must include it.

## Data flow

### Boot

1. launchd starts sidecar (cwd=`/Users/h2oslabs/cc-openclaw` per plist).
2. sidecar: `os.makedirs("~/.openclaw", exist_ok=True)`. (Idempotent — sqlite path also under this dir, already created lazily.)
3. sidecar binds aiohttp to `cfg.api_port`.
4. sidecar atomically writes `{"pid", "port"}` to `~/.openclaw/sidecar.pid`.
5. sidecar logs `sidecar ready on http://127.0.0.1:<port> (pidfile: ~/.openclaw/sidecar.pid)`.
6. Gateway, started independently (cwd=`/`), loads plugin.
7. Plugin's `register()` runs: stores config + accountId. **Does not** read pidfile yet.

### Runtime

Each `before_dispatch` invocation:

1. Plugin computes `getSidecarUrl()` → fresh read of `~/.openclaw/sidecar.pid`.
2. Plugin `fetch` to that URL.
3. Result returned to gateway.

Each `registerCommand` invocation (e.g. `/status`, `/agents`): same pattern.

## Error handling

| Failure mode                                          | Detection                | Behaviour                                                                                                                |
| ----------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| Pidfile absent (sidecar not started, fresh install)   | `readFileSync` → ENOENT  | Fall back to `configUrl \|\| SIDECAR_DEFAULT_URL`. Subsequent fetch may still fail → plugin returns "系统维护中". Acceptable. |
| Pidfile JSON malformed (interrupted write — but atomic write should prevent) | `JSON.parse` throws      | Fall back as above.                                                                                                      |
| Pidfile points to dead sidecar (port no longer bound) | fetch fails              | Plugin's existing try/catch returns "系统维护中". On next sidecar boot, pidfile is overwritten — self-heals.                  |
| `~/.openclaw/` missing on fresh install               | `os.replace` would error | sidecar's explicit `os.makedirs(..., exist_ok=True)` ensures it. Note: `~/.openclaw/` is shared with the openclaw CLI's state dir; if openclaw ever changes that convention (XDG move, Windows `%LOCALAPPDATA%`), sidecar must follow. |
| Read-during-write race                                | same-filesystem atomic rename | `os.replace` is atomic **only when source and destination share a filesystem**. Implementation MUST pass `dir=pidfile_dir` to `tempfile.mkstemp` (load-bearing, see code comment in patch). Reader sees complete previous OR complete new file, never partial. |
| `HOME` not set in sidecar's launchd env               | `os.path.expanduser("~")` returns literal `"~/.openclaw/sidecar.pid"` (no expansion) | macOS launchd user-agents propagate `HOME` by default and the plist's `EnvironmentVariables` block additively merges (only sets `PATH`). Smoke check (see Testing) verifies expansion produces an absolute path. |

We do **not** validate that the PID in the file is alive. Stale pidfiles are normal during a sidecar restart window; the port-test (via fetch) is the real liveness check.

## Testing

| Layer            | Test                                                                                                                                                              | Status                                                                                  |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `sidecar/main.py` | New: pidfile writes to `~/.openclaw/sidecar.pid` (mock `Path.home()` / `os.path.expanduser` to a tmp dir; assert file contents).                                  | To add. Pattern: existing `tests/sidecar/test_*.py` use `pytest`+mock.                  |
| `sidecar/main.py` | New: atomic write — write to `.tmp` first, then rename. Verify no `.tmp` file remains after success. Verify no partial pidfile on simulated mid-write failure.    | To add.                                                                                 |
| Plugin (JS)      | New: Node built-in `node --test` script `tests/plugin/test_resolve_sidecar_url.test.mjs`. Asserts (a) pidfile-present case returns `http://127.0.0.1:<port>`, (b) pidfile-missing case falls back to configUrl, (c) malformed-JSON case falls back to default. ~25 LOC. Requires exporting `resolveSidecarUrl` as named export from `index.js` (currently private). Manual smoke is insufficient because the failure mode is silent — exactly the symptom we're designing our way out of. | To add. |
| Reconciler tests | (existing 72 tests) — not affected by this change.                                                                                                                | Run after to confirm no regression.                                                     |

### Manual smoke checklist (post-deploy)

1. `launchctl unload && load` sidecar.
2. **Verify HOME expansion**: sidecar startup log line should read `pidfile: /Users/h2oslabs/.openclaw/sidecar.pid` (absolute path, not literal `~/.openclaw/sidecar.pid` — that would mean expansion failed).
3. Verify `~/.openclaw/sidecar.pid` exists with current PID + port.
4. Verify `<project>/.sidecar.pid` no longer being written. If a stale file exists from before this PR, delete it manually (one-time).
5. Verify gateway log: next plugin call shows `Sidecar plugin registered (url=http://127.0.0.1:<expected_port>, account=shared)` matching the actual binding (port=`cfg.api_port`).
6. Send fallback message in Feishu DM to a known unprovisioned user (or curl `/api/v1/resolve-sender` directly via plugin → sidecar) → no "系统维护中".
7. **Operator post-deploy**: edit `~/.openclaw/openclaw.json` and remove the explicit `sidecarUrl` field so it defaults to unset. Confirms pidfile-only discovery works with no fallback override masking it.

## Migration

One-time on first restart with the new code:

1. Old `<project>/.sidecar.pid` file may exist from previous sidecar runs. Sidecar will no longer write it; **stale file is harmless** (`.gitignore` line 6 already lists `.sidecar.pid`; verified no readers in the new code — see verification below).
2. No data migration needed — pidfile is ephemeral state, regenerated on every sidecar boot.
3. No downtime — atomic flip in a single PR + sidecar restart.

### Reader/writer verification (codified)

Grep evidence that the legacy `<project>/.sidecar.pid` has only two consumers, both updated by this PR:

```
$ grep -rn "\.sidecar\.pid" --include="*.py" --include="*.js" --include="*.json" \
    --include="*.sh" --include="*.md" --include="*.plist" --include="*.yaml" \
    --exclude-dir={.venv,node_modules,.git,voice-web} .
sidecar/main.py:111             # writer (this PR moves to ~/.openclaw/sidecar.pid)
openclaw-sidecar-plugin/index.js:17  # reader (this PR moves to ~/.openclaw/sidecar.pid)
.gitignore:6                    # cosmetic — keeps stray copy out of git
```

No CI scripts, no `cc-openclaw.sh` wrappers, no docs reference it. A future implementer should re-run this grep before merging to confirm no new consumer was added in the meantime.

## Out of scope

- Multi-instance support (confirmed not needed for this machine's "manage local openclaw install" use case).
- Authenticated discovery / TLS (sidecar is `127.0.0.1` only; no remote attack surface).
- JS test infrastructure (plugin tests would be valuable long-term but expand this fix).
- Removing `SIDECAR_DEFAULT_URL` constant entirely (kept as defense-in-depth fallback for first-run / fresh-install pre-sidecar-start scenario).
- Updating launchd plist to add gateway management (gateway is started by openclaw CLI; out of scope to change).

## Why not env var? (rejected alternative B)

Considered passing `OPENCLAW_SIDECAR_URL` from sidecar → gateway. Rejected because gateway typically starts before sidecar restarts and there's no clean way to push env vars into a running gateway process. File-based discovery sidesteps the lifecycle ordering problem entirely.

## Why not plugin `import.meta.url`? (rejected alternative C)

Considered using ES module `import.meta.url` to find plugin's own install location and walk up to project root. Rejected because: (a) plugin install location may vary if it ever ships as an npm package, (b) `URL → filesystem path` is platform-fragile, (c) it still ties pidfile location to "wherever this checkout lives" which conflicts with the "machine-level state" framing.

## Why not Unix domain socket? (rejected alternative D)

Considered making sidecar listen on `~/.openclaw/sidecar.sock` (Unix domain socket) instead of TCP `127.0.0.1:<port>`. Modest threat-model gain (filesystem permissions instead of "any local user can curl 127.0.0.1"). Rejected because:

1. **Node's built-in `fetch` does not accept a `socketPath` option.** The plugin would have to import `undici` explicitly and build an `Agent({ socketPath: ... })`, then pass it via `dispatcher`. Adds dependency surface.
2. **Threat model is single-user mac**: only `h2oslabs` user runs anything on this box, no multi-tenant exposure. The 127.0.0.1 binding already excludes off-box callers.
3. **Doesn't even solve the discovery problem**: plugin still needs to know the socket path, which is the same problem we're solving with the absolute-path pidfile. Just shifts the constant from a port number to a path.

If the threat model ever expands (multi-user host, sandbox-escape concerns), revisit.
