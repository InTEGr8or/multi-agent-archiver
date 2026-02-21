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
    def __init__(self, config_path="config.yaml", dry_run=False, limit=None):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        self.dry_run = dry_run
        self.limit = limit
        self.processed_count = 0
        
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

        self.s3_client = boto3.client('s3')
        
        self.google_api_key = os.getenv("GOOGLE_API_KEY")
        if self.google_api_key:
            self.client = genai.Client(api_key=self.google_api_key)
        else:
            logger.warning("GOOGLE_API_KEY not found in environment variables. Summarization will be skipped.")
            self.client = None
            
        self.request_delay = self.config.get('summarization', {}).get('delay', 10)
        self.total_bytes_saved = 0

    def format_size(self, bytes_val):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.2f} TB"

    def discover_files(self, category):
        patterns = self.config['paths'].get(category, [])
        files = []
        for pattern in patterns:
            expanded_pattern = str(Path(pattern).expanduser())
            
            # Efficient discovery for Aider using 'find'
            if category == 'aider' and "**" in expanded_pattern:
                search_root = expanded_pattern.split("**")[0]
                filename = expanded_pattern.split("/")[-1]
                try:
                    cmd = [
                        "find", search_root, "-name", filename,
                        "-not", "-path", "*/node_modules/*",
                        "-not", "-path", "*/.venv/*",
                        "-not", "-path", "*/.git/*"
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                    found = [f for f in result.stdout.splitlines() if f.strip()]
                    files.extend(found)
                except subprocess.CalledProcessError as e:
                    logger.error(f"Error running find for {category}: {e}")
            else:
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
        if self.dry_run:
            return "Dry run summary"
        
        if not self.client:
            return "Summary unavailable (No API Key)"
        
        # Initial configured delay
        time.sleep(self.request_delay)
        
        retries = 0
        max_retries = 5
        base_delay = 5

        while retries < max_retries:
            try:
                # Stricter prompt to avoid preamble and fluff
                prompt = (
                    "Summarize the following AI chat conversation in 1 phrase. "
                    "CRITICAL: Do not include any preamble like 'This conversation is about...', 'In this chat...', "
                    "'The AI successfully...', or 'The following discussed...'. "
                    "Start directly with the core topic or task. Be terse, objective, and title-like.\n\n"
                    f"Conversation content:\n{content[:15000]}"
                )
                
                response = self.client.models.generate_content(
                    model=self.config['summarization']['model'],
                    contents=prompt
                )
                summary = response.text.strip()
                
                # Secondary cleanup just in case the AI ignored instructions
                prefixes_to_strip = [
                    "This conversation is about", "This chat is about", 
                    "In this conversation", "In this chat", 
                    "The conversation discussed", "The following discussed",
                    "The conversation begins with", "The chat starts with",
                    "The conversation is an", "The chat is an"
                ]
                for prefix in prefixes_to_strip:
                    if summary.lower().startswith(prefix.lower()):
                        summary = summary[len(prefix):].strip().lstrip(':').strip()
                
                return summary[:200]
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
        return "Unknown", "Unknown"

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
            
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=20)
        keep_count = 5 # Number of recent chats to preserve per workspace

        for ws_path, ws_files in workspaces.items():
            # Sort files by modification time, newest first
            ws_files_with_mtime = []
            for f in ws_files:
                try:
                    mtime = datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
                    ws_files_with_mtime.append((f, mtime))
                except OSError:
                    continue
            
            ws_files_with_mtime.sort(key=lambda x: x[1], reverse=True)
            
            # Process files, skipping the most recent N
            for i, (f, mtime) in enumerate(ws_files_with_mtime):
                if self.limit and self.processed_count >= self.limit:
                    break
                
                if i < keep_count:
                    logger.debug(f"Preserving {f} (Newest {i+1} in workspace {ws_path})")
                    continue
                    
                if mtime < cutoff_date:
                    self.archive_file(f, category, ws_path)
                else:
                    logger.debug(f"Skipping {f} (Too recent: {mtime.isoformat()})")

    def process_gemini_archive(self):
        self.process_category_archival('gemini')

    def process_roo(self):
        self.process_category_archival('roo')

    def process_aider(self):
        self.process_category_archival('aider')

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
            summary = self.get_summary(markdown_content)
            
            # Generate slug from summary
            slug = self.slugify(summary)
            if not slug:
                slug = "conversation"
            
            base_key_name = f"{date_str}-{slug}"
            
            # Prepend summary to markdown for better readability in the archive
            full_markdown = f"# Summary\n{summary}\n\n{markdown_content}"
            
            s3_prefix = f"{self.config['s3']['prefix']}{category}/{base_key_name}"
            
            uploads = [
                {"key": f"{s3_prefix}.md", "body": full_markdown, "type": "text/markdown"},
                {"key": f"{s3_prefix}.summary.txt", "body": summary, "type": "text/plain"},
                {"key": f"{s3_prefix}.json", "body": json.dumps(parsed_content, indent=2), "type": "application/json"}
            ]

            if self.dry_run:
                for upload in uploads:
                    logger.info(f"[DRY-RUN] Would upload to s3://{self.config['s3']['bucket']}/{upload['key']}")
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
        
        mode_prefix = "[DRY-RUN] Estimated" if self.dry_run else "Total"
        print(f"\n{mode_prefix} disk space reclaimed: {self.format_size(self.total_bytes_saved)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archive AI chats to S3.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run.")
    parser.add_argument("--limit", type=int, help="Limit the number of files archived.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only perform cleanup of commit-only chats.")
    args = parser.parse_args()

    archiver = ChatArchiver(dry_run=args.dry_run, limit=args.limit)
    archiver.run(cleanup_only=args.cleanup_only)
