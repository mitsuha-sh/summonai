SHELL := /bin/bash

.PHONY: setup start stop

setup:
	bash setup.sh

start:
	@command -v zellij >/dev/null 2>&1 || { \
		echo "Error: zellij is not installed. Run 'make setup' first and install zellij."; \
		exit 1; \
	}
	@zellij kill-session summonai 2>/dev/null || true; \
	zellij delete-session summonai 2>/dev/null || true; \
	zellij --session summonai --new-session-with-layout "$(CURDIR)/zellij/layouts/summonai-start.kdl"

stop:
	@zellij kill-session summonai 2>/dev/null || true
	@zellij delete-session summonai 2>/dev/null || true
	@echo "summonai session stopped."
