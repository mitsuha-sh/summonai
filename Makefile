SHELL := /bin/bash

.PHONY: setup start start-mobile stop stop-mobile

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
	@export SUMMONAI_ROOT="$(CURDIR)"; \
	if zellij list-sessions -n 2>/dev/null | grep -Fxq "summonai"; then \
		echo "Attaching existing zellij session: summonai"; \
		exec zellij attach summonai; \
	else \
		echo "Creating zellij session: summonai"; \
		exec zellij --session summonai --config "$(CURDIR)/zellij/config/summonai.kdl" --new-session-with-layout "$(CURDIR)/zellij/layouts/summonai-start.kdl"; \
	fi

start-mobile:
	@command -v zellij >/dev/null 2>&1 || { \
		echo "Error: zellij is not installed. Run 'make setup' first and install zellij."; \
		exit 1; \
	}
	@command -v claude >/dev/null 2>&1 || { \
		echo "Error: claude CLI is not installed."; \
		exit 1; \
	}
	@if zellij list-sessions -n 2>/dev/null | grep -Fxq "summonai-mobile"; then \
		echo "Attaching existing zellij session: summonai-mobile"; \
		exec zellij attach summonai-mobile; \
	else \
		echo "Creating zellij session with mobile layout: summonai-mobile"; \
		exec zellij --session summonai-mobile --config "$(CURDIR)/zellij/config/summonai-mobile.kdl" --new-session-with-layout "$(CURDIR)/zellij/layouts/summonai-start-mobile.kdl"; \
	fi

stop:
	@zellij kill-session summonai 2>/dev/null || true
	@zellij delete-session summonai 2>/dev/null || true
	@echo "summonai session stopped."

stop-mobile:
	@zellij kill-session summonai-mobile 2>/dev/null || true
	@zellij delete-session summonai-mobile 2>/dev/null || true
	@echo "summonai-mobile session stopped."
