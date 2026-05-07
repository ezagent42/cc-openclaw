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

// On Windows, os.homedir() reads USERPROFILE, not HOME. The override
// dance below assumes POSIX semantics; skip on Windows rather than
// silently testing against the real home directory.
if (process.platform === "win32") {
  console.log("Skipping: Windows HOME override semantics differ from POSIX");
  process.exit(0);
}

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
