.PHONY: run-server run-channel setup

run-server:
	uv run python3 feishu/channel_server.py

run-channel:
	uv run python3 feishu/channel.py

setup:
	uv sync
