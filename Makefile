#!/usr/bin/env bash
.PHONY: help
help: ## Display this help screen
	@echo "Available commands:"
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z_-]+:.*?## / {printf "  \033[32m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ==============================================================================
# Application Tasks
# ==============================================================================

check-op: ## Check `op` authentication status
	@op whoami > /dev/null 2>&1 || (echo "\033[31mError: Not signed in to 1Password. Please run 'op signin'.\033[0m" && exit 1)

lint: ## Run linting and auto-fix with Ruff
	uv run ruff check --fix .

dry-run: lint check-op ## Perform a dry run of the archiver (e.g. make dry-run ARGS="--limit 5")
	export GOOGLE_API_KEY=$$(op read "op://Private/GEMINI_API_KEY/credential") && [ -n "$$GOOGLE_API_KEY" ] || exit 1; \
	uv run python archiver.py --dry-run $(ARGS)

archive: lint check-op ## Archive chats to S3 and remove local files
	export GOOGLE_API_KEY=$$(op read "op://Private/GEMINI_API_KEY/credential") && [ -n "$$GOOGLE_API_KEY" ] || exit 1; \
	uv run python archiver.py $(ARGS)
