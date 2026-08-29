#!/usr/bin/env bash
.PHONY: help
help: ## Display this help screen
	@echo "Available commands:"
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z_-]+:.*?## / {printf "  \033[32m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ==============================================================================
# Application Tasks
# ==============================================================================

lint: ## Run linting and auto-fix with Ruff
	uv run ruff check --fix .

dry-run: lint ## Perform a dry run of the archiver (e.g. make dry-run ARGS="--limit 5")
	uv run cax --dry-run $(ARGS)

archive: lint ## Archive chats to S3 and remove local files
	uv run cax $(ARGS)

cleanup: lint ## Delete commit-only chats locally (no S3 upload)
	uv run cax --cleanup-only $(ARGS)
