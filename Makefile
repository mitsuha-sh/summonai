SHELL := /bin/bash

.PHONY: setup start stop

setup:
	bash setup.sh

start:
	@command -v zellij >/dev/null 2>&1 || { \
		echo "Error: zellij is not installed. Run 'make setup' first and install zellij."; \
		exit 1; \
	}
	@command -v claude >/dev/null 2>&1 || { \
		echo "Error: claude CLI is not installed."; \
		exit 1; \
	}
	@if zellij list-sessions -n 2>/dev/null | grep -Fxq "summonai"; then \
		echo "Attaching existing zellij session: summonai"; \
		exec zellij attach summonai; \
	else \
		echo "Creating zellij session: summonai"; \
		exec zellij --session summonai --new-session-with-layout "$(CURDIR)/zellij/layouts/summonai-start.kdl"; \
	fi

stop:
	@zellij kill-session summonai 2>/dev/null || true
	@zellij delete-session summonai 2>/dev/null || true
	@echo "summonai session stopped."
