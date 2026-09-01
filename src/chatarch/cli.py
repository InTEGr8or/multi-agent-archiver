import argparse

import verkit  # type: ignore[import-untyped]
from rich.console import Console
from rich.table import Table

from chatarch.archiver import ChatArchiver

COMMANDS = [
    ("dry-run", "Preview what would be cleaned up and archived, without changing anything."),
    ("archive", "Archive eligible chats to S3 and remove the local copies."),
    ("cleanup", "Delete commit-only chats locally, without uploading anything."),
]


def show_menu(console: Console):
    table = Table(title="chatarch (cax) -- AI chat archiver", header_style="bold cyan")
    table.add_column("Command", style="bold green")
    table.add_column("Description")
    for name, desc in COMMANDS:
        table.add_row(name, desc)
    console.print(table)
    console.print(
        "\nRun [bold]cax <command> --help[/bold] for options, or [bold]cax -V[/bold] for version info."
    )


def main():
    parser = argparse.ArgumentParser(description="Archive AI chats to S3.")
    parser.add_argument("-V", "--version", action="store_true", help="Show version info and exit.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    subparsers = parser.add_subparsers(dest="command")

    dry_run_p = subparsers.add_parser("dry-run", help="Preview without changing anything.")
    dry_run_p.add_argument("--limit", type=int, help="Limit the number of files processed.")
    dry_run_p.add_argument("--days", type=int, help="Override the retention period (days). Use 0 for no limit.")

    archive_p = subparsers.add_parser("archive", help="Archive chats to S3 and remove local files.")
    archive_p.add_argument("--limit", type=int, help="Limit the number of files archived.")
    archive_p.add_argument("--days", type=int, help="Override the retention period (days). Use 0 for no limit.")

    cleanup_p = subparsers.add_parser("cleanup", help="Delete commit-only chats locally (no S3 upload).")
    cleanup_p.add_argument("--limit", type=int, help="Limit the number of files processed.")
    cleanup_p.add_argument("--dry-run", action="store_true", help="Preview cleanup without deleting anything.")

    args = parser.parse_args()
    console = Console()

    if args.version:
        verkit.display_version_info(console, "agent-chat-archiver", upgrade_cmd="uv tool upgrade agent-chat-archiver")
        return

    if args.command is None:
        show_menu(console)
        return

    archiver = ChatArchiver(
        config_path=args.config,
        dry_run=args.command == "dry-run" or getattr(args, "dry_run", False),
        limit=getattr(args, "limit", None),
        days_override=getattr(args, "days", None),
    )
    archiver.run(cleanup_only=args.command == "cleanup")


if __name__ == "__main__":
    main()
