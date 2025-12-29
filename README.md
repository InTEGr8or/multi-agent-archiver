# ChatArch: AI Conversation Archiver

ChatArch is a utility designed to migrate local AI chat histories to AWS S3, freeing up local disk space while preserving conversations in a human-readable, summarized, and searchable format.

## Features

- **Multi-Platform Support:** Automatically discovers chats from Gemini CLI, Roo Code (Cline), and Aider.
- **Smart Cleanup:** Identifies and deletes "commit-only" chats (short messages used solely for git commits) before archiving.
- **Markdown Conversion:** Converts complex JSON chat schemas into clean, readable Markdown files within the archive.
- **AI-Powered Summarization:** Uses Gemini 2.5 Flash to generate 1-2 sentence summaries for every archived conversation.
- **Safe Archival:**
  - **Retention Policy:** Only archives chats older than 20 days.
  - **Verification:** Local files are only deleted after a successful S3 upload.
  - **Rate Limit Handling:** Includes configurable delays and exponential backoff for API calls.
- **Space Reporting:** Reports the total disk space reclaimed at the end of each run.

## Prerequisites

- **Python & uv:** Managed via `uv`.
- **AWS CLI:** Must be configured with credentials that have `s3:PutObject` permissions.
- **1Password CLI (`op`):** Used to securely retrieve the Gemini API key.
- **Google Gemini API Key:** Stored in 1Password at `op://Private/GEMINI_API_KEY/credential`.

## Usage

Commands are managed via the `Makefile` for convenience.

### 1. Dry Run (Recommended)
Verify which files will be deleted or archived without actually performing any actions:
```bash
make dry-run
```

### 2. Actual Archive
Perform the cleanup and migration:
```bash
make archive
```

### 3. Using Limits
To test the full process on a small sample (e.g., 5 files):
```bash
make archive ARGS="--limit 5"
```

### 4. Maintenance
Run linting and auto-fixes:
```bash
make lint
```

## Configuration

Settings like S3 bucket names, local search paths, and API delays are managed in `config.yaml`.

## Logging

All detailed operations and API responses are logged to `chatarch/archiver.log`. Progress is displayed in the terminal via a status bar.
