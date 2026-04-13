.PHONY: run-server run-channel setup run-sidecar install-sidecar uninstall-sidecar restart-sidecar sidecar-logs test

run-server:
	uv run python3 feishu/channel_server.py

run-channel:
	uv run python3 feishu/channel.py

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

test:
	uv run pytest tests/ -v
