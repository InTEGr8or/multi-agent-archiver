import argparse

import verkit  # type: ignore[import-untyped]
from rich.console import Console

from chatarch.archiver import ChatArchiver


def main():
    parser = argparse.ArgumentParser(description="Archive AI chats to S3.")
    parser.add_argument("-V", "--version", action="store_true", help="Show version info and exit.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run.")
    parser.add_argument("--limit", type=int, help="Limit the number of files archived.")
    parser.add_argument("--days", type=int, help="Override the retention period (number of days). Use 0 for no limit.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only perform cleanup of commit-only chats.")
    args = parser.parse_args()

    console = Console()
    if args.version:
        verkit.display_version_info(console, "chatarch", upgrade_cmd="uv tool upgrade chatarch")
        return

    archiver = ChatArchiver(
        config_path=args.config,
        dry_run=args.dry_run,
        limit=args.limit,
        days_override=args.days,
    )
    archiver.run(cleanup_only=args.cleanup_only)


if __name__ == "__main__":
    main()
