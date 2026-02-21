# GEMINI.md - ChatArch Project Context

## Project Overview
**ChatArch** is a Python-based utility designed to manage and archive AI conversation histories from various local platforms (Gemini CLI, Roo Code/Cline, and Aider) to AWS S3. Its primary goal is to reclaim local disk space while preserving conversations in a summarized, searchable Markdown format.

### Key Technologies
- **Python 3.12+**: Core logic.
- **uv**: Dependency management and environment isolation.
- **AWS SDK (boto3)**: For S3 uploads.
- **Google Gemini API (google-genai)**: For conversation summarization using `gemini-2.5-flash`.
- **1Password CLI (`op`)**: Secure retrieval of API keys.
- **Ruff**: Linting and code formatting.

### Architecture
- `archiver.py`: Contains the core `ChatArchiver` class which handles file discovery, filtering (e.g., "commit-only" chats), Markdown conversion, summarization, and S3 uploading. Each conversation is archived as three separate files in S3:
  - `<name>.md`: Human-readable Markdown with the summary at the top.
  - `<name>.summary.txt`: A standalone text file containing the AI-generated summary.
  - `<name>.json`: The original raw conversation data.
- `config.yaml`: Centralized configuration for S3 settings, summarization parameters, local search paths, and filtering rules.
- `Makefile`: Provides a simplified CLI interface for common tasks.
- `archiver.log`: Detailed execution logs.

## Building and Running

### Prerequisites
1.  **uv**: Install via `curl -LsSf https://astral.sh/uv/install.sh | sh`.
2.  **AWS CLI**: Configured with appropriate S3 permissions.
3.  **1Password CLI (`op`)**: Installed and signed in (`op signin`).
4.  **Gemini API Key**: Should be stored in 1Password at `op://Private/GEMINI_API_KEY/credential`.

### Common Commands
- **Dry Run**: Preview changes without affecting local files or S3.
  ```bash
  make dry-run
  ```
- **Run Archive**: Perform full linting, summarization, upload, and deletion.
  ```bash
  make archive
  ```
- **Limited Run**: Process only a specific number of files (useful for testing).
  ```bash
  make archive ARGS="--limit 5"
  ```
- **Linting**:
  ```bash
  make lint
  ```

## Development Conventions

### Code Style
- Follows PEP 8 via **Ruff**.
- Ensure all changes are linted using `make lint` before execution.

### Configuration
- Paths for different chat platforms are defined in `config.yaml`.
- When adding support for a new platform, update the `paths` section in `config.yaml` and implement the corresponding parsing logic in `archiver.py`.

### Safety & Verification
- **Retention Policy**: By default, only files older than 20 days are archived (configurable/logic-driven in `archiver.py`).
- **Atomic Operations**: Files are only deleted locally if the S3 upload is confirmed successful.
- **Logging**: All operations should be logged to `archiver.log` using the `logger` instance.

### Error Handling
- Use exponential backoff for API calls (handled in `archiver.py`).
- Critical errors are mirrored to `stderr` via the console handler, while detailed info stays in the log file.
