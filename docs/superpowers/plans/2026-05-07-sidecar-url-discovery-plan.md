# Sidecar URL Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move sidecar pidfile from `<project>/.sidecar.pid` (cwd-relative, fragile under launchd-started gateway) to `~/.openclaw/sidecar.pid` (absolute, machine-level state) so the openclaw plugin can discover it regardless of the gateway's cwd.

**Architecture:** Sidecar (Python) writes pidfile to a hard-coded `~/.openclaw/sidecar.pid` using atomic temp-file + rename within the same directory. Plugin (JS) reads the same hard-coded absolute path. No cwd, no project root, no env var — just two files agreeing on one path. Both retain the legacy fallback to `configUrl || SIDECAR_DEFAULT_URL` for the brief window before sidecar boots on a fresh install.

**Tech Stack:** Python 3.14 + aiohttp (sidecar), Node 22 (plugin runs in openclaw gateway), `pytest` for Python tests, native `node --test` runner for JS tests, `uv` for Python tooling, `launchctl` for sidecar lifecycle.

**Spec:** [docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md](../specs/2026-05-07-sidecar-url-discovery-design.md) (commit `4092551`)

## Execution rules

- **Tasks must be dispatched sequentially**; each Task ends in a commit before the next is started. Do not parallelise — Task 2 depends on Task 1's helper, Task 3's plugin export depends on no Python work but Task 5 depends on all four code Tasks. Don't try to be clever.
- **Rollback** if Task 6 (deploy/smoke) fails: `gh pr revert <pr-number> --create-pr` (or `git revert <merge-sha>` + push), then `launchctl unload && load` sidecar. Old code re-creates `<project>/.sidecar.pid` and PR #21's port-alignment keeps production working — change is cheap to undo.

---

## File map

| File | Change | Why |
|---|---|---|
| `sidecar/main.py` | Replace lines 114–119 with call to new `write_pidfile_atomic` helper. Helper added in same file (top-level, importable for testing). | Move pidfile to `~/.openclaw/sidecar.pid`; atomic write. |
| `sidecar/config.py` | Add 1-line lockstep comment on `api_port` default. | Document that plugin manifest default must match. |
| `openclaw-sidecar-plugin/index.js` | Replace lines 12–39 (imports + `resolveSidecarUrl`). Export `resolveSidecarUrl` as named export so test can import it. | Plugin reads from absolute path; testable in isolation. |
| `openclaw-sidecar-plugin/openclaw.plugin.json` | Update `sidecarUrl` description; keep `default`. | Document override semantics + lockstep. |
| `tests/sidecar/test_pidfile.py` (NEW) | 3 pytest tests for `write_pidfile_atomic`. | TDD: test helper before wiring it. |
| `tests/plugin/test_resolve_sidecar_url.test.mjs` (NEW) | 3 `node --test` cases for `resolveSidecarUrl`. | Silent-failure mode requires automated coverage. |

---

## Worktree setup (run once before Task 1)

- [ ] **Create worktree off `spec/sidecar-url-discovery` branch**

```bash
cd /Users/h2oslabs/cc-openclaw
git worktree add .claude/worktrees/sidecar-url-discovery -b fix/sidecar-url-discovery spec/sidecar-url-discovery
cd .claude/worktrees/sidecar-url-discovery
git log --oneline -3
```

Expected: HEAD is `4092551 docs(spec): robust sidecar URL discovery via ~/.openclaw/sidecar.pid` (the spec commit), branch is `fix/sidecar-url-discovery`.

- [ ] **All subsequent work runs from the worktree** (`/Users/h2oslabs/cc-openclaw/.claude/worktrees/sidecar-url-discovery`).

---

## Task 1: Add `write_pidfile_atomic` helper (TDD)

**Files:**
- Create: `tests/sidecar/test_pidfile.py`
- Modify: `sidecar/main.py` (add helper near top of file)

- [ ] **Step 1.1: Write the failing test**

Create `tests/sidecar/test_pidfile.py` with:

```python
"""Tests for sidecar.main.write_pidfile_atomic — atomic pidfile creation."""

import json
import os
from unittest.mock import patch

import pytest

from sidecar.main import write_pidfile_atomic


def test_write_pidfile_atomic_creates_file(tmp_path):
    """Writes JSON {pid, port} to <pidfile_dir>/sidecar.pid and returns the path."""
    path = write_pidfile_atomic(str(tmp_path), pid=1234, port=18791)

    assert path == str(tmp_path / "sidecar.pid")
    content = (tmp_path / "sidecar.pid").read_text()
    assert json.loads(content) == {"pid": 1234, "port": 18791}


def test_write_pidfile_atomic_creates_dir_if_missing(tmp_path):
    """If pidfile_dir doesn't exist, it is created (mkdir -p)."""
    target_dir = tmp_path / "new" / "nested" / ".openclaw"
    assert not target_dir.exists()

    path = write_pidfile_atomic(str(target_dir), pid=1, port=2)

    assert target_dir.exists()
    assert os.path.exists(path)


def test_write_pidfile_atomic_no_tmp_residue_on_success(tmp_path):
    """After a successful write, no .sidecar.pid.* tmp files remain."""
    write_pidfile_atomic(str(tmp_path), pid=1, port=2)

    residue = list(tmp_path.glob(".sidecar.pid.*"))
    assert residue == [], f"unexpected tmp files: {residue}"


def test_write_pidfile_atomic_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """If os.replace fails, the .tmp file is unlinked, not orphaned."""
    def boom(*args, **kwargs):
        raise OSError("simulated rename failure")
    monkeypatch.setattr("os.replace", boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        write_pidfile_atomic(str(tmp_path), pid=1, port=2)

    residue = list(tmp_path.glob(".sidecar.pid.*"))
    assert residue == [], f"orphaned tmp files: {residue}"
```

- [ ] **Step 1.2: Run test, verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw/.claude/worktrees/sidecar-url-discovery
uv run pytest tests/sidecar/test_pidfile.py -v
```

Expected: 4 tests collected, 4 errors with `ImportError: cannot import name 'write_pidfile_atomic'`.

- [ ] **Step 1.3: Add the helper to `sidecar/main.py`**

Insert this function **after the imports (after line 18, before `log = logging.getLogger("sidecar")`)**.

**Note**: `json` and `tempfile` are imported *inside* the function body deliberately — keeps the helper self-contained for testing and matches the spec snippet exactly. If you prefer hoisted imports, move them to module top, but don't drop them.

```python
def write_pidfile_atomic(pidfile_dir: str, pid: int, port: int) -> str:
    """Atomically write {pid, port} JSON to <pidfile_dir>/sidecar.pid.

    `dir=pidfile_dir` passed to tempfile.mkstemp is LOAD-BEARING, not
    incidental: os.replace is only atomic when src and dst share a
    filesystem. Default tempfile dir ($TMPDIR → /var/folders/.../T on
    macOS) might be on a different volume; never use the default here.

    Returns the absolute path of the pidfile written.
    """
    import json
    import tempfile

    os.makedirs(pidfile_dir, exist_ok=True)
    pidfile_path = os.path.join(pidfile_dir, "sidecar.pid")
    fd, tmp_path = tempfile.mkstemp(prefix=".sidecar.pid.", dir=pidfile_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": pid, "port": port}))
        os.replace(tmp_path, pidfile_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return pidfile_path
```

- [ ] **Step 1.4: Run test, verify it passes**

```bash
uv run pytest tests/sidecar/test_pidfile.py -v
```

Expected: 4 passed.

- [ ] **Step 1.5: Run full sidecar test suite to confirm no regression**

```bash
uv run pytest tests/sidecar/ -v
```

Expected: 76 passed (72 existing + 4 new).

- [ ] **Step 1.6: Commit**

```bash
git add tests/sidecar/test_pidfile.py sidecar/main.py
git commit -m "feat(sidecar): write_pidfile_atomic helper with tests

TDD-style introduction of an atomic pidfile writer. Wiring into the
boot path lands in the next commit."
```

---

## Task 2: Wire `write_pidfile_atomic` into sidecar boot

**Files:**
- Modify: `sidecar/main.py` lines 114–119 (replace pidfile-write block)

- [ ] **Step 2.1: Replace the pidfile-write block in `main()`**

Find this block (lines 114–119 in current main.py):

```python
    # Write pidfile for service discovery
    pidfile_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), ".sidecar.pid")
    import json as _json
    with open(pidfile_path, "w") as f:
        f.write(_json.dumps({"pid": os.getpid(), "port": actual_port}))
    log.info("sidecar ready on http://127.0.0.1:%d (pidfile: %s)", actual_port, pidfile_path)
```

Replace with:

```python
    # Write pidfile to ~/.openclaw/sidecar.pid (machine-level state alongside
    # sidecar.sqlite). The plugin reads from this absolute path regardless
    # of its own cwd. See docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md.
    pidfile_dir = os.path.expanduser("~/.openclaw")
    pidfile_path = write_pidfile_atomic(pidfile_dir, pid=os.getpid(), port=actual_port)
    log.info("sidecar ready on http://127.0.0.1:%d (pidfile: %s)", actual_port, pidfile_path)
```

(The previous `import json as _json` stays out — the helper handles its own json import.)

- [ ] **Step 2.2: Run full sidecar tests**

```bash
uv run pytest tests/sidecar/ -v
```

Expected: 76 passed. No regression.

- [ ] **Step 2.3: Smoke-import sidecar module to confirm no syntax/import errors**

```bash
uv run python -c "from sidecar.main import main, write_pidfile_atomic; print('ok')"
```

Expected: `ok`

- [ ] **Step 2.4: Commit**

```bash
git add sidecar/main.py
git commit -m "feat(sidecar): write pidfile to ~/.openclaw/sidecar.pid

Removes cwd-derived pidfile path so the openclaw plugin can read it from
a stable absolute location regardless of its own cwd. See spec at
docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md."
```

---

## Task 3: Plugin — export `resolveSidecarUrl` + change pidfile path (TDD)

**Files:**
- Create: `tests/plugin/test_resolve_sidecar_url.test.mjs`
- Modify: `openclaw-sidecar-plugin/index.js` (lines 12–39)

- [ ] **Step 3.1: Create the test directory**

```bash
mkdir -p tests/plugin
```

- [ ] **Step 3.2: Write the failing test**

Create `tests/plugin/test_resolve_sidecar_url.test.mjs` with:

```js
// Tests for openclaw-sidecar-plugin/index.js — resolveSidecarUrl()
//
// Failure mode this test guards against is silent: if the plugin reads
// the wrong pidfile path or doesn't fall back correctly, the only symptom
// is "系统维护中" in production. Worth automated coverage even though the
// rest of the plugin has none.

import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Override HOME *before* dynamic import — PIDFILE_PATH is captured at
// module load time, so process.env.HOME must be set first.
const fakeHome = join(tmpdir(), `oc-test-${process.pid}-${Date.now()}`);
mkdirSync(join(fakeHome, ".openclaw"), { recursive: true });
process.env.HOME = fakeHome;

const { resolveSidecarUrl } = await import("../../openclaw-sidecar-plugin/index.js");

const PIDFILE = join(fakeHome, ".openclaw", "sidecar.pid");

// Defensive: each test starts from a clean state regardless of order.
beforeEach(() => {
  try { rmSync(PIDFILE, { force: true }); } catch {}
});

test("resolveSidecarUrl returns http URL when pidfile present", () => {
  writeFileSync(PIDFILE, JSON.stringify({ pid: 1, port: 12345 }));
  assert.equal(resolveSidecarUrl("http://override:9"), "http://127.0.0.1:12345");
});

test("resolveSidecarUrl falls back to configUrl when pidfile missing", () => {
  // beforeEach ensured PIDFILE is gone
  assert.equal(resolveSidecarUrl("http://override:9"), "http://override:9");
});

test("resolveSidecarUrl falls back to default URL when pidfile malformed and no configUrl", () => {
  writeFileSync(PIDFILE, "{not valid json");
  assert.equal(resolveSidecarUrl(undefined), "http://127.0.0.1:18791");
});

// Cleanup the fake home on test process exit
process.on("exit", () => { rmSync(fakeHome, { recursive: true, force: true }); });
```

- [ ] **Step 3.3: Run test, verify it fails**

```bash
cd /Users/h2oslabs/cc-openclaw/.claude/worktrees/sidecar-url-discovery
node --test tests/plugin/test_resolve_sidecar_url.test.mjs 2>&1 | tail -20
```

Expected: failures because (a) `resolveSidecarUrl` is not a named export, and (b) the plugin's current `PIDFILE_NAME = ".sidecar.pid"` looks for the file under `process.cwd()`, not `~/.openclaw/`.

- [ ] **Step 3.4: Replace lines 12–39 of `openclaw-sidecar-plugin/index.js`**

Find the current block:

```js
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";

const SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791";
const DEFAULT_ACCOUNT_ID = "shared";
const PIDFILE_NAME = ".sidecar.pid";

/**
 * Resolve sidecar URL by reading pidfile (written by sidecar on startup).
 * Falls back to config or default URL if pidfile is missing.
 */
function resolveSidecarUrl(configUrl) {
  try {
    // Look for pidfile in project root (same dir as sidecar-config.yaml)
    const candidates = [
      resolve(process.cwd(), PIDFILE_NAME),
      resolve(process.cwd(), "..", PIDFILE_NAME),
    ];
    for (const pidfile of candidates) {
      try {
        const content = readFileSync(pidfile, "utf-8");
        const { port } = JSON.parse(content);
        if (port) return `http://127.0.0.1:${port}`;
      } catch { /* try next */ }
    }
  } catch { /* fallback */ }
  return configUrl || SIDECAR_DEFAULT_URL;
}
```

Replace with:

```js
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// MUST track sidecar/config.py:api_port default. The pidfile (written by
// sidecar at boot) wins when present; this default is only consulted on
// fresh installs before sidecar has booted. If you change this, change
// the YAML default in lockstep — see specs/2026-05-07-sidecar-url-discovery-design.md.
const SIDECAR_DEFAULT_URL = "http://127.0.0.1:18791";
const DEFAULT_ACCOUNT_ID = "shared";

// Absolute machine-level path; matches sidecar/main.py write_pidfile_atomic.
const PIDFILE_PATH = join(homedir(), ".openclaw", "sidecar.pid");

/**
 * Resolve sidecar URL by reading the pidfile sidecar writes on startup.
 * Falls back to configUrl or SIDECAR_DEFAULT_URL when pidfile missing or
 * malformed (e.g. fresh install pre-sidecar-boot).
 *
 * Exported for unit testing; called per plugin invocation (not cached) so
 * sidecar restarts pick up the new port without bouncing the gateway.
 */
export function resolveSidecarUrl(configUrl) {
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

(The function is now `export function` instead of plain `function`. The default-export object below at line 41 still works — it doesn't reference `resolveSidecarUrl` directly, only uses it via the closure in `getSidecarUrl`.)

- [ ] **Step 3.5: Run test, verify all 3 cases pass**

```bash
node --test tests/plugin/test_resolve_sidecar_url.test.mjs
```

Expected: `# pass 3`, `# fail 0`.

- [ ] **Step 3.6: Verify the rest of the plugin still loads correctly (syntax)**

```bash
node --check openclaw-sidecar-plugin/index.js
```

Expected: no output (means syntax-clean). If you see syntax errors, the export keyword change broke something — most likely a stray reference to the old `dirname` import that you forgot to remove.

- [ ] **Step 3.6b: Verify both default and named exports load**

```bash
node -e "import('./openclaw-sidecar-plugin/index.js').then(m => { if (!m.default || !m.resolveSidecarUrl) { console.error('missing exports:', Object.keys(m)); process.exit(1); } console.log('exports ok'); })"
```

Expected: `exports ok`. If it fails, the `register()` default-export path is broken — most likely a closure reference to the now-removed cwd path code.

- [ ] **Step 3.7: Commit**

```bash
git add tests/plugin/test_resolve_sidecar_url.test.mjs openclaw-sidecar-plugin/index.js
git commit -m "feat(plugin): resolveSidecarUrl reads from ~/.openclaw/sidecar.pid

Pidfile path is now an absolute machine-level path that matches
sidecar/main.py. Eliminates the cwd-relative search that broke when
gateway started from cwd=/. Adds 3 node --test cases (the failure mode
is silent so manual smoke wasn't sufficient)."
```

---

## Task 4: Plugin manifest description + lockstep comment in `config.py`

**Files:**
- Modify: `openclaw-sidecar-plugin/openclaw.plugin.json` (lines 7–11)
- Modify: `sidecar/config.py` (line 44)

- [ ] **Step 4.1: Update plugin manifest description**

Replace the `sidecarUrl` block in `openclaw-sidecar-plugin/openclaw.plugin.json`:

```json
      "sidecarUrl": {
        "type": "string",
        "description": "Sidecar API base URL",
        "default": "http://127.0.0.1:18791"
      },
```

With:

```json
      "sidecarUrl": {
        "type": "string",
        "description": "Emergency override. Pidfile at ~/.openclaw/sidecar.pid wins when present — this value is ONLY used when the pidfile is missing/malformed. Set only for non-standard topologies (e.g. remote testing). MUST track sidecar/config.py:api_port default if used.",
        "default": "http://127.0.0.1:18791"
      },
```

- [ ] **Step 4.2: Add lockstep comment in `sidecar/config.py`**

Find line 44:

```python
    api_port: int = 18791
```

Replace with:

```python
    # MUST track openclaw-sidecar-plugin/openclaw.plugin.json:sidecarUrl default.
    # The plugin uses sidecarUrl as a fresh-install fallback before sidecar's
    # pidfile is written; if these two values drift, that fallback window
    # silently breaks. See specs/2026-05-07-sidecar-url-discovery-design.md.
    api_port: int = 18791
```

- [ ] **Step 4.3: Verify Python tests still pass**

```bash
uv run pytest tests/sidecar/ -v
```

Expected: 76 passed.

- [ ] **Step 4.4: Verify JSON parses**

```bash
uv run python -c "import json; json.load(open('openclaw-sidecar-plugin/openclaw.plugin.json')); print('json ok')"
```

Expected: `json ok`

- [ ] **Step 4.5: Commit**

```bash
git add openclaw-sidecar-plugin/openclaw.plugin.json sidecar/config.py
git commit -m "docs: lockstep comments for sidecar api_port / plugin sidecarUrl

Make the cross-file dependency explicit. The plugin's sidecarUrl default
is consulted only as a fresh-install fallback; it must match the
sidecar's api_port default or that fallback window silently breaks."
```

---

## Task 5: Push branch + open PR + merge

- [ ] **Step 5.1: Push the branch**

```bash
git push -u origin fix/sidecar-url-discovery
```

Expected: branch pushes; gh remote prints PR-create URL.

- [ ] **Step 5.2: Open PR**

```bash
gh pr create --base main --head fix/sidecar-url-discovery \
  --title "fix(sidecar): pidfile at ~/.openclaw/sidecar.pid (cwd-independent discovery)" \
  --body "$(cat <<'EOF'
## Summary

Plugin's pidfile lookup was cwd-relative; gateway runs from `cwd=/` (not launchd-managed) so it never found the pidfile and silently fell back to `SIDECAR_DEFAULT_URL = http://127.0.0.1:18791`. PR #21 made sidecar bind to that exact port so production worked, but only because two independent constants happened to align — any drift would re-break new-user provisioning with "系统维护中".

This PR moves the pidfile to a stable absolute path: `~/.openclaw/sidecar.pid`. Sidecar writes there atomically (temp + rename, same-volume). Plugin reads from there directly. `SIDECAR_DEFAULT_URL` is retained as a fresh-install fallback only.

## Files changed

- `sidecar/main.py` — extract `write_pidfile_atomic` helper; wire into boot path
- `sidecar/config.py` — lockstep comment
- `openclaw-sidecar-plugin/index.js` — new absolute path; export `resolveSidecarUrl` for testing
- `openclaw-sidecar-plugin/openclaw.plugin.json` — describe override semantics
- `tests/sidecar/test_pidfile.py` (NEW) — 4 pytest cases
- `tests/plugin/test_resolve_sidecar_url.test.mjs` (NEW) — 3 `node --test` cases

## Spec

[`docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md`](docs/superpowers/specs/2026-05-07-sidecar-url-discovery-design.md)

## Test plan

- [x] `uv run pytest tests/sidecar/` → 76/76 pass (72 existing + 4 new)
- [x] `node --test tests/plugin/test_resolve_sidecar_url.test.mjs` → 3/3 pass
- [ ] Manual smoke after merge: launchctl restart sidecar → verify `~/.openclaw/sidecar.pid` written → curl `/api/v1/resolve-sender` via plugin path → no "系统维护中"

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5.3: Verify CI / mergeable**

```bash
gh pr view --json mergeable,mergeStateStatus
```

Expected: `{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}` (or similar non-blocked state).

- [ ] **Step 5.4: Squash-merge**

```bash
PR=$(gh pr view --json number --jq .number)
gh pr merge "$PR" --squash --admin --delete-branch
```

Expected: branch deleted, fast-forward to main.

- [ ] **Step 5.5: Sync main repo**

```bash
cd /Users/h2oslabs/cc-openclaw  # back to the main checkout
git stash push -m "local sidecar-config.yaml" sidecar-config.yaml 2>/dev/null || true
git fetch origin main
git checkout main
git pull --ff-only origin main
git stash pop 2>/dev/null || true
git log --oneline -3
```

Expected: main HEAD is the new squash commit.

---

## Task 6: Deploy + smoke test

- [ ] **Step 6.1: Restart sidecar via launchd**

```bash
launchctl unload ~/Library/LaunchAgents/ai.openclaw.sidecar.plist
sleep 1
launchctl load ~/Library/LaunchAgents/ai.openclaw.sidecar.plist
sleep 3
launchctl list | grep ai.openclaw.sidecar
```

Expected: `<PID>\t0\tai.openclaw.sidecar` (PID > 0, status 0).

- [ ] **Step 6.2: Verify pidfile location + content**

```bash
ls -la ~/.openclaw/sidecar.pid
cat ~/.openclaw/sidecar.pid
```

Expected: file exists, contents like `{"pid": <pid>, "port": 18791}`.

- [ ] **Step 6.3: Verify HOME expansion (smoke #2 from spec)**

```bash
tail -5 ~/.openclaw/logs/sidecar.err.log | grep "pidfile:"
```

Expected: a line like `sidecar ready on http://127.0.0.1:18791 (pidfile: /Users/h2oslabs/.openclaw/sidecar.pid)`. The path must be **absolute** — `/Users/h2oslabs/.openclaw/...` not `~/.openclaw/...`. If you see literal `~/`, HOME wasn't expanded; investigate launchd env.

- [ ] **Step 6.4: Verify port is bound**

```bash
lsof -iTCP:18791 -sTCP:LISTEN -P 2>&1 | head -3
```

Expected: a `python3.* sidecar/main.py` listener on 18791.

- [ ] **Step 6.5: Sidecar API health from plugin's perspective**

```bash
curl -sS --noproxy '*' -o /dev/null -w "GET /api/v1/agents → HTTP %{http_code}\n" \
  http://127.0.0.1:18791/api/v1/agents
```

Expected: `HTTP 200`.

- [ ] **Step 6.5b: Verify gateway plugin picked up the new pidfile path**

The plugin logs its resolved sidecar URL once at registration. If pidfile discovery worked, the log line shows the actual port from `~/.openclaw/sidecar.pid`. If it fell through to `SIDECAR_DEFAULT_URL`, the log shows that constant — only OK by coincidence today, will silently break under any drift.

```bash
# Find the gateway log. Common locations on this box:
ls -lat ~/.openclaw/logs/gateway*.log 2>/dev/null | head -3
# Tail the most recent one for the registration line:
tail -200 "$(ls -t ~/.openclaw/logs/gateway*.log 2>/dev/null | head -1)" 2>/dev/null \
  | grep "Sidecar plugin registered" | tail -1
```

Expected: a line like `[plugins] Sidecar plugin registered (url=http://127.0.0.1:18791, account=shared)` where the port matches `cfg.api_port` (verify: `grep '^\s*api_port' /Users/h2oslabs/cc-openclaw/sidecar-config.yaml`).

If gateway logs aren't at `~/.openclaw/logs/gateway*.log`, ask the operator where they're tailing them — this step blocks shipping if the plugin-side log can't be confirmed.

- [ ] **Step 6.6: Clean up legacy pidfile (one-time)**

```bash
ls -la /Users/h2oslabs/cc-openclaw/.sidecar.pid 2>/dev/null && rm /Users/h2oslabs/cc-openclaw/.sidecar.pid
ls /Users/h2oslabs/cc-openclaw/.sidecar.pid 2>&1
```

Expected: second `ls` reports "No such file or directory".

- [ ] **Step 6.7: End-to-end test — provision a real-looking user via the plugin path**

Use the same target user from earlier debugging (manager-A, `ou_3f665b6a0be6015795114e5986874417`):

```bash
curl -sS --noproxy '*' -X POST http://127.0.0.1:18791/api/v1/resolve-sender \
  -H "Content-Type: application/json" \
  -d '{"open_id":"ou_3f665b6a0be6015795114e5986874417"}'
```

Expected: a JSON action — `{"action": "active"}` (already provisioned earlier in this session) or `{"action": "retry_later"}`. **Not** an HTTP error or "系统维护中". This proves the sidecar-side endpoint is reachable; the gateway plugin uses the same fetch path.

- [ ] **Step 6.8: Watch sidecar log for next reconcile (≤ 10 min) — confirm no idempotency regression**

```bash
# Wait until the 10-minute boundary, or skip if you've already verified
tail -20 ~/.openclaw/logs/sidecar.err.log
```

Expected: latest "Reconciliation complete" line. **Should NOT** show "Reconciled revoke for ou_e2e4..." for the 3 ghost users (those are now `(0,0)` in DB and the idempotency fix from PR #21 should skip them).

- [ ] **Step 6.9: Operator-side cleanup of `~/.openclaw/openclaw.json` — do this LAST, after 6.7 confirms pidfile path works**

**Sequence safety**: do NOT run this until Step 6.7 has passed. Removing the explicit `sidecarUrl` strips the operator's last fallback before this PR — if the new pidfile path is broken at runtime, the gateway has only `SIDECAR_DEFAULT_URL` (the hardcoded constant) left.

- [ ] **6.9a — Inspect**:

```bash
grep -n "sidecarUrl" ~/.openclaw/openclaw.json
```

Expected: shows the line setting `"sidecarUrl": "http://127.0.0.1:18791"` in the openclaw-sidecar plugin block.

- [ ] **6.9b — Edit**: open `~/.openclaw/openclaw.json` in an editor; delete the `"sidecarUrl": "..."` line from the openclaw-sidecar plugin's `config` block. Save.

- [ ] **6.9c — Verify**: gateway picks up the change. Either wait for the gateway's next config-reload (if it polls), or restart the gateway via the operator's usual mechanism (typically the `openclaw` CLI — ask if unsure). Then re-run Step 6.5b (gateway log) and confirm the registered URL still matches `cfg.api_port`. If it does, pidfile-only discovery works without any fallback masking it.

This step is **optional-but-recommended**. The operator config is not source-controlled, so it's not part of the PR; the change is permanent until the operator re-adds it.

- [ ] **Step 6.10: Cleanup worktree**

```bash
cd /Users/h2oslabs/cc-openclaw
CONFIRM_DESTROY=1 git worktree remove .claude/worktrees/sidecar-url-discovery
git branch -D fix/sidecar-url-discovery 2>&1 || true   # auto-deleted by gh pr merge --delete-branch
git worktree list
```

Expected: only the main checkout remains in the worktree list.

---

## Done criteria

- [x] All Python tests pass: `uv run pytest tests/sidecar/` → 76/76
- [x] All JS tests pass: `node --test tests/plugin/` → 3/3
- [x] PR merged to `main`
- [x] Sidecar restarted via launchctl, listening on 18791
- [x] `~/.openclaw/sidecar.pid` exists with correct content
- [x] Legacy `<project>/.sidecar.pid` deleted
- [x] Sidecar startup log shows expanded absolute path (no literal `~/`)
- [x] `curl /api/v1/resolve-sender` returns valid JSON (no "系统维护中")
- [x] Worktree removed, branch cleaned up
