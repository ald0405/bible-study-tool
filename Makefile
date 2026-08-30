PORT := 8765
PID_FILE := .server.pid
LOG_FILE := server.log

.PHONY: help setup run start stop restart status open clean

.DEFAULT_GOAL := help

help:
	@echo "make setup            install deps, download NLTK/cross-ref data (run once)"
	@echo "make start            start the server in the background"
	@echo "make open [REF=\"...\"] start (if needed) and open a passage in the browser"
	@echo "                      e.g. make open REF=\"John 3:16\"  (defaults to Ephesians 1)"
	@echo "make stop             stop the background server"
	@echo "make restart          stop + start"
	@echo "make status           check whether it's running"
	@echo "make run              run in the foreground, Ctrl-C to stop (for watching logs)"
	@echo "make clean            clear the on-disk translation cache"

setup:
	uv sync
	uv run python scripts/setup_data.py

# Foreground — blocks this terminal, Ctrl-C to stop. Good for watching logs.
run:
	uv run python server.py

# Background — starts the server detached and returns control immediately.
start:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Already running (PID $$(cat $(PID_FILE))) at http://localhost:$(PORT)"; \
	else \
		nohup uv run python server.py > $(LOG_FILE) 2>&1 & echo $$! > $(PID_FILE); \
		sleep 1; \
		if kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
			echo "Started (PID $$(cat $(PID_FILE))) at http://localhost:$(PORT)"; \
		else \
			echo "Failed to start — check $(LOG_FILE)"; rm -f $(PID_FILE); exit 1; \
		fi; \
	fi

# Stop the background server, however it was started.
stop:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)); rm -f $(PID_FILE); \
		echo "Stopped."; \
	else \
		rm -f $(PID_FILE); \
		PID=$$(lsof -ti:$(PORT)); \
		if [ -n "$$PID" ]; then kill $$PID; echo "Stopped (was running outside make, PID $$PID)."; \
		else echo "Not running."; fi; \
	fi

restart: stop start

status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Running (PID $$(cat $(PID_FILE))) at http://localhost:$(PORT)"; \
	elif lsof -ti:$(PORT) >/dev/null 2>&1; then \
		echo "Running on port $(PORT) (not tracked by make — started outside make start)"; \
	else \
		echo "Not running."; \
	fi

# Start (if needed) and open a passage directly in the browser.
# Usage: make open REF="Ephesians 1"   (defaults to Ephesians 1 if REF omitted)
open: start
	@REF="$(REF)"; \
	if [ -z "$$REF" ]; then REF="Ephesians 1"; fi; \
	ENCODED=$$(uv run python -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$$REF"); \
	URL="http://localhost:$(PORT)/#$$ENCODED"; \
	echo "Opening $$URL"; \
	open "$$URL"

clean:
	rm -rf data/cache/*
