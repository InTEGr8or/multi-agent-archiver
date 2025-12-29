import argparse
import os
import json
import yaml
import glob
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
        
        if self.dry_run:
            logger.info("DRY RUN MODE ENABLED - No files will be deleted or uploaded.")
        
        if self.limit:
            logger.info(f"LIMIT ENABLED - Only processing up to {self.limit} files.")
        
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
            filters = self.config['filters']['gemini_commit_only']
            
            if len(messages) > filters['max_messages']:
                return False
            
            content = " ".join([m.get("content", "").lower() for m in messages if m.get("content")])
            for keyword in filters['keywords']:
                if keyword.lower() in content:
                    return True
            return False
        except Exception as e:
            logger.error(f"Error checking {file_path}: {e}")
            return False

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
                prompt = f"Summarize the following AI chat conversation in 1-2 sentences. Focus on the main task or topic:\n\n{content[:15000]}"
                
                response = self.client.models.generate_content(
                    model=self.config['summarization']['model'],
                    contents=prompt
                )
                return response.text.strip()
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
            if self.is_gemini_commit_only(f):
                file_size = Path(f).stat().st_size
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

    def process_gemini_archive(self):
        if self.limit and self.processed_count >= self.limit:
            return
        logger.info("Processing Gemini chats for archival...")
        files = self.discover_files('gemini')
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=20)
        logger.info(f"Archiving chats older than {cutoff_date.isoformat()}")

        for f in tqdm(files, desc="Archiving Gemini chats", unit="file"):
            if self.limit and self.processed_count >= self.limit:
                break
            file_path = Path(f)
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            
            if mtime < cutoff_date:
                self.archive_file(f, "gemini")
            else:
                logger.debug(f"Skipping {f} (Last modified: {mtime.isoformat()})")

    def process_roo(self):
        if self.limit and self.processed_count >= self.limit:
            return
        logger.info("Processing Roo Code chats...")
        files = self.discover_files('roo')
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=20)
        
        for f in tqdm(files, desc="Archiving Roo chats", unit="file"):
            if self.limit and self.processed_count >= self.limit:
                break
            file_path = Path(f)
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)

            if mtime < cutoff_date:
                self.archive_file(f, "roo")
            else:
                logger.debug(f"Skipping Roo chat {f} (too recent)")

    def process_aider(self):
        if self.limit and self.processed_count >= self.limit:
            return
        logger.info("Processing Aider chats...")
        files = self.discover_files('aider')
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=20)
        
        for f in tqdm(files, desc="Archiving Aider chats", unit="file"):
            if self.limit and self.processed_count >= self.limit:
                break
            file_path = Path(f)
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)

            if mtime < cutoff_date:
                self.archive_file(f, "aider")
            else:
                logger.debug(f"Skipping Aider chat {f} (too recent)")

    def to_markdown(self, category, content):
        md = [f"# Archive: {category.capitalize()} Chat", "---"]
        
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

    def archive_file(self, file_path, category):
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

            if category == 'roo':
                unique_name = f"{p.parent.name}_{p.name}"
            elif category == 'aider':
                parts = p.parts
                try:
                    start_idx = -1
                    if 'repos' in parts:
                        start_idx = parts.index('repos')
                    elif 'projects' in parts:
                        start_idx = parts.index('projects')
                    
                    if start_idx != -1:
                        unique_name = "_".join(parts[start_idx+1:]).replace(".", "_")
                    else:
                        unique_name = f"{p.parent.name}_{p.name}".replace(".", "_")
                except Exception:
                    unique_name = f"{p.parent.name}_{p.name}".replace(".", "_")
            else:
                unique_name = p.name

            markdown_content = self.to_markdown(category, parsed_content)
            summary = self.get_summary(markdown_content)
            
            archive_data = {
                "category": category,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "original_path": str(file_path),
                "summary": summary,
                "markdown": markdown_content,
                "raw_content": parsed_content
            }
            
            s3_key = f"{self.config['s3']['prefix']}{category}/{unique_name}"
            if not s3_key.endswith(".json"):
                s3_key += ".json"

            if self.dry_run:
                logger.info(f"[DRY-RUN] Would archive {file_path} to s3://{self.config['s3']['bucket']}/{s3_key}")
                self.total_bytes_saved += file_size
                self.processed_count += 1
                return

            logger.info(f"Archiving {file_path} to s3://{self.config['s3']['bucket']}/{s3_key}")
            
            self.s3_client.put_object(
                Bucket=self.config['s3']['bucket'],
                Key=s3_key,
                Body=json.dumps(archive_data, indent=2),
                ContentType='application/json'
            )
            
            os.remove(file_path)
            self.total_bytes_saved += file_size
            self.processed_count += 1
            logger.info(f"Successfully archived and removed local file: {file_path}")
            
        except Exception as e:
            logger.error(f"Failed to archive {file_path}: {e}")

    def run(self):
        self.process_gemini_cleanup()
        self.process_gemini_archive()
        self.process_roo()
        self.process_aider()
        
        mode_prefix = "[DRY-RUN] Estimated" if self.dry_run else "Total"
        print(f"\n{mode_prefix} disk space reclaimed: {self.format_size(self.total_bytes_saved)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Archive AI chats to S3.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run.")
    parser.add_argument("--limit", type=int, help="Limit the number of files archived.")
    args = parser.parse_args()

    archiver = ChatArchiver(dry_run=args.dry_run, limit=args.limit)
    archiver.run()
