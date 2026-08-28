import argparse
import os
import json
import yaml
import glob
import re
import boto3
from pathlib import Path
import logging
from google import genai
from datetime import datetime, timedelta, timezone
import time
import random
from tqdm import tqdm
import subprocess

from agent_registry import discover_agent_chats, get_chat_workspace, get_chat_last_active

# Categories whose file discovery is delegated to agent-cli-registry instead
# of the `paths` globs in config.yaml. `search_roots` is only meaningful for
# patterns relative to a project (e.g. Aider's history file); registry-global
# patterns (e.g. Claude Code's ~/.claude/projects/**) ignore it.
REGISTRY_BACKED_CATEGORIES = {
    "claude": {"agent_id": "claude"},
    "aider": {
        "agent_id": "aider",
        "search_roots": [Path.home() / "repos", Path.home() / "projects"],
    },
}

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='archiver.log', # Write logs to file
    filemode='a'
)
# Create a console handler for critical errors only
console = logging.StreamHandler()
console.setLevel(logging.WARNING)
logging.getLogger('').addHandler(console)

logger = logging.getLogger(__name__)

class ChatArchiver:
    def __init__(self, config_path="config.yaml", dry_run=False, limit=None, days_override=None):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        self.dry_run = dry_run
        self.limit = limit
        self.days_override = days_override
        self.processed_count = 0
        
        self.retention_days = self.days_override if self.days_override is not None else self.config.get('retention', {}).get('days', 20)
        self.keep_count = self.config.get('retention', {}).get('keep_recent', 5)
        
        # Load workspace index if it exists
        self.workspace_index = {}
        index_path = Path("data/workspace_index.json")
        if index_path.exists():
            try:
                with open(index_path, "r") as f:
                    data = json.load(f)
                    for entry in data.get("workspaces", []) + data.get("unmatched_hashes", []):
                        if entry.get("hash"):
                            self.workspace_index[entry["hash"]] = entry.get("resolved_path") or entry.get("clue_path")
            except Exception as e:
                logger.error(f"Error loading workspace index: {e}")

        self._s3_client = None
        self._gemini_client = None
        self.gemini_api_key = None
        self._gemini_key_fetch_attempted = False
            
        self.request_delay = self.config.get('summarization', {}).get('delay', 10)
        self.total_bytes_saved = 0

    @property
    def s3_client(self):
        if self._s3_client is None:
            self._s3_client = boto3.client('s3')
        return self._s3_client

    @property
    def client(self):
        """Lazy loader for the Gemini client."""
        if self._gemini_client is None and not self._gemini_key_fetch_attempted:
            self._gemini_key_fetch_attempted = True
            # Check environment for GEMINI_API_KEY or legacy GOOGLE_API_KEY
            self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

            if not self.gemini_api_key:
                # Attempt to fetch from 1Password
                secret_ref = "op://Private/GEMINI_API_KEY/credential"
                try:
                    logger.info(f"GEMINI_API_KEY not found in environment. Fetching from 1Password ({secret_ref})...")

                    # Try 'op' first, then 'op.exe' for WSL compatibility with Windows Hello.
                    # A timeout is required: op.exe can block indefinitely on an
                    # unanswered Windows Hello prompt in a headless run.
                    for op_cmd in ["op", "op.exe"]:
                        try:
                            result = subprocess.run(
                                [op_cmd, "read", secret_ref],
                                capture_output=True,
                                text=True,
                                check=True,
                                timeout=10,
                            )
                            self.gemini_api_key = result.stdout.strip()
                            if self.gemini_api_key:
                                break
                        except subprocess.TimeoutExpired:
                            logger.warning(f"Timed out waiting for '{op_cmd}' (10s) -- skipping.")
                        except (subprocess.CalledProcessError, FileNotFoundError) as e:
                            # Only warn on the last attempt
                            if op_cmd == "op.exe":
                                error_msg = getattr(e, 'stderr', str(e)).strip()
                                logger.warning(f"Failed to fetch GEMINI_API_KEY from 1Password: {error_msg}")
                except Exception as e:
                    logger.warning(f"An unexpected error occurred while fetching from 1Password: {e}")

            if self.gemini_api_key:
                self._gemini_client = genai.Client(api_key=self.gemini_api_key)
            else:
                logger.warning("Summarization client unavailable (missing GEMINI_API_KEY).")
        return self._gemini_client

    def format_size(self, bytes_val):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.2f} TB"

    def discover_files(self, category):
        if category in REGISTRY_BACKED_CATEGORIES:
            cfg = REGISTRY_BACKED_CATEGORIES[category]
            chats = discover_agent_chats(
                agent_id=cfg["agent_id"],
                search_roots=cfg.get("search_roots"),
            )
            return sorted({str(c.path) for c in chats})

        patterns = self.config['paths'].get(category, [])
        files = []
        for pattern in patterns:
            expanded_pattern = str(Path(pattern).expanduser())
            found = glob.glob(expanded_pattern, recursive=True)
            files.extend(found)
        return sorted(list(set(files)))

    def is_gemini_commit_only(self, file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
            
            messages = data.get("messages", [])
            
            # Case 1: Empty conversation
            if not messages:
                return True, "Empty conversation"

            # Check the very first message
            first_msg_raw = messages[0].get("content", "")
            if isinstance(first_msg_raw, list):
                first_msg_text = " ".join([str(p) for p in first_msg_raw])
            else:
                first_msg_text = str(first_msg_raw)
            
            # Case 2: Contentless conversation
            if not first_msg_text.strip():
                return True, "No content in first message"

            first_msg_lower = first_msg_text.lower()
            commit_patterns = [
                "git diff --cached",
                "create a git commit",
                "perform the commit without asking",
                "create a git commit for all staged",
                "The commit has been successfully created"
            ]
            
            is_commit_prompt = any(pattern in first_msg_lower for pattern in commit_patterns)
            preview = first_msg_text.strip().replace("\n", " ")[:50]
            
            # Case 3: Automated commit prompt with more leeway for messages (retries/errors)
            if is_commit_prompt and len(messages) <= 15:
                return True, preview
                
            return False, preview
        except Exception as e:
            logger.error(f"Error checking {file_path}: {e}")
            return False, ""

    def get_summary(self, content):
        # Access self.client to trigger secret fetch even in dry-run
        client = self.client
        
        if self.dry_run:
            return "Dry Run Title", "Dry run summary (Secret Fetch Verified). This is a mock 1-3 paragraph summary to simulate the new format."
        
        if not client:
            return "Summary unavailable", "Summary unavailable (No API Key)"
        
        # Initial configured delay
        time.sleep(self.request_delay)
        
        retries = 0
        max_retries = 5
        base_delay = 5

        while retries < max_retries:
            try:
                # Stricter prompt for structured title and 1-3 paragraph summary
                prompt = (
                    "Summarize the following AI chat conversation. Provide your response in exactly two parts:\n"
                    "TITLE: A single-line, direct, technical title for the conversation.\n"
                    "SUMMARY: A 1 to 3 paragraph detailed summary focusing on technical objectives, "
                    "specific problems addressed, and the final resolution.\n\n"
                    "CRITICAL: Do not include any preamble like 'This conversation is about...', 'In this chat...', "
                    "or 'The following discussed...'. Start both the title and the summary directly with the core content.\n\n"
                    f"Conversation content:\n{content[:15000]}"
                )
                
                response = client.models.generate_content(
                    model=self.config['summarization']['model'],
                    contents=prompt
                )
                text = response.text.strip()
                
                title = "Untitled Conversation"
                summary = text
                
                # Try to parse title and summary from the response
                if "TITLE:" in text and "SUMMARY:" in text:
                    parts = text.split("SUMMARY:", 1)
                    title_part = parts[0].replace("TITLE:", "").strip()
                    summary_part = parts[1].strip()
                    
                    if title_part:
                        title = title_part
                    if summary_part:
                        summary = summary_part

                # Cleanup generic prefixes if they still occur
                prefixes_to_strip = [
                    "This conversation is about", "This chat is about", 
                    "In this conversation", "In this chat", 
                    "The conversation discussed", "The following discussed",
                    "The conversation begins with", "The chat starts with",
                    "The conversation is an", "The chat is an"
                ]
                for prefix in prefixes_to_strip:
                    if title.lower().startswith(prefix.lower()):
                        title = title[len(prefix):].strip().lstrip(':').strip()
                    if summary.lower().startswith(prefix.lower()):
                        summary = summary[len(prefix):].strip().lstrip(':').strip()
                
                return title[:200], summary
            except Exception as e:
                is_rate_limit = False
                if hasattr(e, 'code') and e.code == 429:
                    is_rate_limit = True
                elif "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    is_rate_limit = True
                
                if is_rate_limit:
                    delay = (base_delay * (2 ** retries)) + random.uniform(0, 1)
                    logger.warning(f"Rate limit hit. Retrying in {delay:.2f}s... (Attempt {retries + 1}/{max_retries})")
                    time.sleep(delay)
                    retries += 1
                else:
                    logger.error(f"Error getting summary: {e}")
                    raise e
        
        raise Exception("Max retries exceeded for rate limiting.")

    def process_gemini_cleanup(self):
        logger.info("Cleaning up commit-only Gemini chats...")
        files = self.discover_files('gemini')
        logger.info(f"Scanning {len(files)} Gemini chats for cleanup.")
        
        deleted_count = 0
        for f in tqdm(files, desc="Cleaning Gemini chats", unit="file"):
            is_commit, preview = self.is_gemini_commit_only(f)
            if is_commit:
                file_size = Path(f).stat().st_size
                if self.dry_run:
                    tqdm.write(f"[DRY-RUN] Identifying commit-only chat: {f} | Prompt: {preview}...")
                
                logger.info(f"Identified commit-only Gemini chat: {f} ({self.format_size(file_size)})")
                if not self.dry_run:
                    try:
                        os.remove(f)
                        logger.info(f"Deleted {f}")
                        self.total_bytes_saved += file_size
                        deleted_count += 1
                    except OSError as e:
                        logger.error(f"Error deleting {f}: {e}")
                else:
                    self.total_bytes_saved += file_size
                    deleted_count += 1
        
        logger.info(f"Cleanup complete. Identified {deleted_count} files for deletion.")

    def get_workspace_info(self, file_path, category):
        p = Path(file_path)
        if category == 'gemini':
            # Use hash from path
            h = p.parent.parent.name
            resolved = self.workspace_index.get(h)
            if resolved:
                return resolved, Path(resolved).name
            return str(p.parent.parent), h
        elif category == 'roo':
            # Extract from JSON if possible
            try:
                with open(file_path, 'r') as f:
                    content = f.read(10000)
                    match = re.search(r'Current Workspace Directory \((.*?)\)', content)
                    if match:
                        ws_path = match.group(1)
                        return ws_path, Path(ws_path).name
            except Exception:
                pass
            return str(p.parent.parent.parent), p.parent.parent.parent.name
        elif category == 'aider':
            return str(p.parent), p.parent.name
        elif category == 'claude':
            ws = get_chat_workspace(self._as_discovered_chat(file_path, category))
            if ws:
                return str(ws), ws.name
            return "Unknown", "Unknown"
        return "Unknown", "Unknown"

    def _as_discovered_chat(self, file_path, category):
        from agent_registry import DiscoveredChat
        parser_type = "jsonl" if category == "claude" else "markdown"
        return DiscoveredChat(agent_id=category, path=Path(file_path), parser_type=parser_type)

    def process_category_archival(self, category):
        if self.limit and self.processed_count >= self.limit:
            return
            
        logger.info(f"Processing {category} chats for archival...")
        files = self.discover_files(category)
        
        # Group files by workspace
        workspaces = {}
        for f in files:
            ws_path, ws_id = self.get_workspace_info(f, category)
            if ws_path not in workspaces:
                workspaces[ws_path] = []
            workspaces[ws_path].append(f)
            
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        keep_count = self.keep_count

        # Collect all files to process to show overall progress
        files_to_archive = []
        for ws_path, ws_files in workspaces.items():
            # Sort by true last-active time where available (e.g. Claude Code's
            # per-message timestamps), falling back to file mtime otherwise --
            # mtime alone can be misleading for metadata-only stub rewrites.
            ws_files_with_mtime = []
            for f in ws_files:
                try:
                    last_active = None
                    if category == "claude":
                        last_active = get_chat_last_active(self._as_discovered_chat(f, category))
                    mtime = last_active or datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
                    ws_files_with_mtime.append((f, mtime, ws_path))
                except OSError:
                    continue
            
            ws_files_with_mtime.sort(key=lambda x: x[1], reverse=True)
            
            # Process files, skipping the most recent N
            for i, (f, mtime, ws_p) in enumerate(ws_files_with_mtime):
                if i < keep_count:
                    continue
                    
                if mtime < cutoff_date:
                    files_to_archive.append((f, ws_p))

        if not files_to_archive:
            logger.info(f"No {category} files found for archival.")
            return

        desc = f"Archiving {category.capitalize()}"
        for f, ws_p in tqdm(files_to_archive, desc=desc, unit="file"):
            if self.limit and self.processed_count >= self.limit:
                break
            self.archive_file(f, category, ws_p)

    def process_gemini_archive(self):
        self.process_category_archival('gemini')

    def process_roo(self):
        self.process_category_archival('roo')

    def process_aider(self):
        self.process_category_archival('aider')

    def process_claude(self):
        self.process_category_archival('claude')

    def to_markdown(self, category, content, workspace_path=None):
        md = [f"# Archive: {category.capitalize()} Chat", "---"]
        if workspace_path:
            md.append(f"**Workspace:** `{workspace_path}`\n")
        
        if category == 'gemini':
            try:
                data = content if isinstance(content, dict) else json.loads(content)
                for msg in data.get("messages", []):
                    role = msg.get("role", msg.get("type", "unknown")).upper()
                    ts = msg.get("timestamp", "")
                    text = msg.get("content", "")
                    md.append(f"### {role} ({ts})")
                    md.append(f"{text}\n")
            except Exception:
                md.append(str(content))

        elif category == 'roo':
            try:
                data = content if isinstance(content, list) else json.loads(content)
                for msg in data:
                    mtype = msg.get("type", "")
                    say = msg.get("say", "")
                    text = msg.get("text", "")
                    
                    if mtype == "say" and say in ["text", "user_feedback"]:
                        role = "USER" if say == "user_feedback" else "ROO"
                        md.append(f"### {role}")
                        md.append(f"{text}\n")
                    elif mtype == "ask" and msg.get("ask") == "tool":
                        md.append("### ROO (Tool Use)")
                        md.append(f"{text}\n")
                    elif mtype == "ask" and msg.get("ask") == "command":
                        md.append("### ROO (Command)")
                        md.append(f"```bash\n{text}\n```\n")
            except Exception:
                md.append("Error parsing Roo JSON")

        elif category == 'aider':
            md.append(str(content))

        elif category == 'claude':
            try:
                data = content if isinstance(content, list) else json.loads(content)
                for entry in data:
                    etype = entry.get("type")
                    if etype not in ("user", "assistant"):
                        continue
                    message = entry.get("message", {})
                    role = (message.get("role") or etype).upper()
                    ts = entry.get("timestamp", "")
                    raw = message.get("content", "")

                    if isinstance(raw, list):
                        parts = []
                        for block in raw:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "text":
                                parts.append(block.get("text", ""))
                            elif btype == "tool_use":
                                parts.append(f"[Tool call: {block.get('name', 'unknown')}]")
                            elif btype == "tool_result":
                                result_content = block.get("content", "")
                                if isinstance(result_content, list):
                                    result_content = " ".join(
                                        b.get("text", "") for b in result_content if isinstance(b, dict)
                                    )
                                parts.append(f"[Tool result: {str(result_content)[:300]}]")
                        text = "\n".join(p for p in parts if p)
                    else:
                        text = str(raw)

                    if not text.strip():
                        continue
                    md.append(f"### {role} ({ts})")
                    md.append(f"{text}\n")
            except Exception:
                md.append("Error parsing Claude Code JSONL")

        return "\n".join(md)

    def slugify(self, text):
        text = text.lower()
        text = re.sub(r'[^\w\s-]', '', text)
        text = re.sub(r'[-\s]+', '-', text)
        return text.strip('-')[:60]

    def archive_file(self, file_path, category, workspace_path=None):
        try:
            p = Path(file_path)
            file_size = p.stat().st_size
            with open(file_path, "r") as f:
                raw_content = f.read()
            
            parsed_content = None
            if file_path.endswith(".json"):
                try:
                    parsed_content = json.loads(raw_content)
                except json.JSONDecodeError:
                    parsed_content = raw_content
            elif file_path.endswith(".jsonl"):
                parsed_content = []
                for line in raw_content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed_content.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            else:
                parsed_content = raw_content

            # Determine Date
            date_str = datetime.now().strftime("%Y-%m-%d")
            match = re.search(r'(\d{4}-\d{2}-\d{2})', p.name)
            if match:
                date_str = match.group(1)
            else:
                # Fallback to file modification time
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                date_str = mtime.strftime("%Y-%m-%d")

            markdown_content = self.to_markdown(category, parsed_content, workspace_path)
            title, summary = self.get_summary(markdown_content)
            
            # Generate slug from the title
            slug = self.slugify(title)
            if not slug:
                slug = "conversation"
            
            base_key_name = f"{date_str}-{slug}"
            
            # Prepend title and summary to markdown for better readability in the archive
            full_markdown = f"# Title: {title}\n\n# Summary\n{summary}\n\n{markdown_content}"
            
            # Use both title and summary in the summary.txt file
            full_summary_text = f"Title: {title}\n\nSummary:\n{summary}"
            
            # Extract Year and Month for sharding
            # date_str is expected to be YYYY-MM-DD
            year = date_str[:4]
            month = date_str[5:7]
            
            s3_prefix = f"{self.config['s3']['prefix']}{category}/{year}/{month}/{base_key_name}"
            
            uploads = [
                {"key": f"{s3_prefix}.md", "body": full_markdown, "type": "text/markdown"},
                {"key": f"{s3_prefix}.summary.txt", "body": full_summary_text, "type": "text/plain"},
                {"key": f"{s3_prefix}.json", "body": json.dumps(parsed_content, indent=2), "type": "application/json"}
            ]

            if self.dry_run:
                tqdm.write(f"[DRY-RUN] Would upload to s3://{self.config['s3']['bucket']}/{s3_prefix}.md")
                self.total_bytes_saved += file_size
                self.processed_count += 1
                return

            logger.info(f"Archiving {file_path} to S3 (3 files)...")
            
            for upload in uploads:
                self.s3_client.put_object(
                    Bucket=self.config['s3']['bucket'],
                    Key=upload['key'],
                    Body=upload['body'],
                    ContentType=upload['type']
                )
            
            os.remove(file_path)
            self.total_bytes_saved += file_size
            self.processed_count += 1
            logger.info(f"Successfully archived (MD, Summary, JSON) and removed local file: {file_path}")
            
        except Exception as e:
            logger.error(f"Failed to archive {file_path}: {e}")

    def run(self, cleanup_only=False):
        self.process_gemini_cleanup()
        if cleanup_only:
            mode_prefix = "[DRY-RUN] Estimated" if self.dry_run else "Total"
            print(f"\n{mode_prefix} disk space reclaimed from cleanup: {self.format_size(self.total_bytes_saved)}")
            return

        self.process_gemini_archive()
        self.process_roo()
        self.process_aider()
        self.process_claude()
        
        mode_prefix = "[DRY-RUN] Estimated" if self.dry_run else "Total"
        s3_location = f"s3://{self.config['s3']['bucket']}/{self.config['s3']['prefix']}"
        
        print(f"\n{mode_prefix} disk space reclaimed: {self.format_size(self.total_bytes_saved)}")
        print(f"Archive Destination: {s3_location}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archive AI chats to S3.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run.")
    parser.add_argument("--limit", type=int, help="Limit the number of files archived.")
    parser.add_argument("--days", type=int, help="Override the retention period (number of days). Use 0 for no limit.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only perform cleanup of commit-only chats.")
    args = parser.parse_args()

    archiver = ChatArchiver(dry_run=args.dry_run, limit=args.limit, days_override=args.days)
    archiver.run(cleanup_only=args.cleanup_only)
