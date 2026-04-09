SHELL := /bin/bash

.PHONY: setup start

setup:
	bash setup.sh

start:
	@command -v zellij >/dev/null 2>&1 || { \
		echo "Error: zellij is not installed. Run 'make setup' first and install zellij."; \
		exit 1; \
	}
	@zellij attach --create summonai options --default-layout "$(CURDIR)/zellij/layouts/summonai-start.kdl"
