PIDFILE_DEV = .pids/dev.pid
PIDFILE_TUNNEL = .pids/tunnel.pid
PIDFILE_GATEWAY = .pids/gateway.pid
GATEWAY_DIR = ../voice_gateway

.PHONY: start stop restart dev tunnel gateway logs

start: dev gateway tunnel
	@echo "All services started"

stop:
	@echo "Stopping services..."
	@if [ -f $(PIDFILE_GATEWAY) ]; then \
		kill $$(cat $(PIDFILE_GATEWAY)) 2>/dev/null; \
		rm -f $(PIDFILE_GATEWAY); \
		echo "  gateway stopped"; \
	fi
	@if [ -f $(PIDFILE_DEV) ]; then \
		kill $$(cat $(PIDFILE_DEV)) 2>/dev/null; \
		rm -f $(PIDFILE_DEV); \
		echo "  dev server stopped"; \
	fi
	@if [ -f $(PIDFILE_TUNNEL) ]; then \
		kill $$(cat $(PIDFILE_TUNNEL)) 2>/dev/null; \
		rm -f $(PIDFILE_TUNNEL); \
		echo "  tunnel stopped"; \
	fi
	@pkill -f "tsx server.ts" 2>/dev/null || true
	@pkill -f "next-server" 2>/dev/null || true
	@pkill -f "voice_gateway/server.py" 2>/dev/null || true
	@sleep 2
	@pkill -9 -f "tsx server.ts" 2>/dev/null || true
	@pkill -9 -f "next-server" 2>/dev/null || true
	@pkill -9 -f "voice_gateway/server.py" 2>/dev/null || true
	@sleep 1
	@rm -rf .next

restart: stop start

dev:
	@mkdir -p .pids
	@echo "Starting Next.js dev server..."
	@pnpm dev > /tmp/voice-web-dev.log 2>&1 & echo $$! > $(PIDFILE_DEV)
	@sleep 8
	@if kill -0 $$(cat $(PIDFILE_DEV)) 2>/dev/null; then \
		echo "  dev server running (PID $$(cat $(PIDFILE_DEV)))"; \
	else \
		echo "  dev server failed, check /tmp/voice-web-dev.log"; \
		rm -f $(PIDFILE_DEV); \
		exit 1; \
	fi

gateway:
	@mkdir -p .pids
	@echo "Starting Python voice gateway..."
	@cd $(GATEWAY_DIR) && uv run python server.py > /tmp/voice-gateway.log 2>&1 & echo $$! > $(CURDIR)/$(PIDFILE_GATEWAY)
	@sleep 3
	@if kill -0 $$(cat $(PIDFILE_GATEWAY)) 2>/dev/null; then \
		echo "  gateway running (PID $$(cat $(PIDFILE_GATEWAY)))"; \
	else \
		echo "  gateway failed, check /tmp/voice-gateway.log"; \
		rm -f $(PIDFILE_GATEWAY); \
		exit 1; \
	fi

tunnel:
	@mkdir -p .pids
	@echo "Starting Cloudflare tunnel..."
	@cloudflared tunnel run > /tmp/voice-web-tunnel.log 2>&1 & echo $$! > $(PIDFILE_TUNNEL)
	@sleep 5
	@if kill -0 $$(cat $(PIDFILE_TUNNEL)) 2>/dev/null; then \
		echo "  tunnel running (PID $$(cat $(PIDFILE_TUNNEL)))"; \
	else \
		echo "  tunnel failed, check /tmp/voice-web-tunnel.log"; \
		rm -f $(PIDFILE_TUNNEL); \
		exit 1; \
	fi

logs:
	@echo "=== Gateway ===" && tail -30 /tmp/voice-gateway.log 2>/dev/null || echo "(no log)"
	@echo ""
	@echo "=== Dev Server ===" && tail -15 /tmp/voice-web-dev.log 2>/dev/null || echo "(no log)"
	@echo ""
	@echo "=== Tunnel ===" && tail -10 /tmp/voice-web-tunnel.log 2>/dev/null || echo "(no log)"
