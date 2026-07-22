.PHONY: run-server run-channel setup run-sidecar install-sidecar uninstall-sidecar restart-sidecar sidecar-logs install-heartbeat uninstall-heartbeat restart-heartbeat heartbeat-logs test test-py test-js

run-server:
	uv run python3 channel_server/app.py

run-channel:
	uv run python3 channel_server/adapters/cc/channel.py

setup:
	uv sync

# --- Sidecar ---

run-sidecar:
	uv run python3 sidecar/main.py

install-sidecar:
	cp deploy/ai.openclaw.sidecar.plist ~/Library/LaunchAgents/
	launchctl load ~/Library/LaunchAgents/ai.openclaw.sidecar.plist

uninstall-sidecar:
	launchctl unload ~/Library/LaunchAgents/ai.openclaw.sidecar.plist 2>/dev/null || true
	rm -f ~/Library/LaunchAgents/ai.openclaw.sidecar.plist

restart-sidecar:
	launchctl kickstart -k gui/$$(id -u)/ai.openclaw.sidecar

sidecar-logs:
	tail -f ~/.openclaw/logs/sidecar.log

# --- Feishu progress heartbeat (launchd timer, fires every 30 min) ---
# One-time human activation: `make install-heartbeat`. Reload after edits:
# `make uninstall-heartbeat install-heartbeat`. Loading twice is harmless.

install-heartbeat:
	cp deploy/ai.openclaw.feishu-heartbeat.plist ~/Library/LaunchAgents/
	launchctl load ~/Library/LaunchAgents/ai.openclaw.feishu-heartbeat.plist

uninstall-heartbeat:
	launchctl unload ~/Library/LaunchAgents/ai.openclaw.feishu-heartbeat.plist 2>/dev/null || true
	rm -f ~/Library/LaunchAgents/ai.openclaw.feishu-heartbeat.plist

restart-heartbeat:
	launchctl kickstart -k gui/$$(id -u)/ai.openclaw.feishu-heartbeat

heartbeat-logs:
	tail -f ~/.openclaw/logs/feishu-heartbeat.log

test: test-py test-js

test-py:
	uv run pytest tests/ -v

test-js:
	node --test 'tests/plugin/*.test.mjs'
