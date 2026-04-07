SHELL := /bin/bash

.PHONY: setup start

setup:
	bash setup.sh

start:
	claude
