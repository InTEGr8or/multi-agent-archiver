import json
import re
from archiver import ChatArchiver
from tqdm import tqdm

def main():
    archiver = ChatArchiver(dry_run=False)
    bucket = archiver.config['s3']['bucket']
    prefix = archiver.config['s3']['prefix']
    
    # We only care about the 'gemini' folder for now based on user report, 
    # but let's make it generic if possible or just focus on gemini first.
    # The user specifically showed 'chats/gemini/'.
    
    categories = ['gemini', 'roo', 'aider']
    
    for category in categories:
        s3_prefix = f"{prefix}{category}/"
        print(f"Scanning {s3_prefix}...")
        
        paginator = archiver.s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=s3_prefix)
        
        for page in pages:
            if 'Contents' not in page:
                continue
                
            for obj in tqdm(page['Contents'], desc=f"Processing {category}"):
                key = obj['Key']
                
                # Skip if it already looks like a new format (ends in .md, .summary.txt)
                # Or if it doesn't match the old 'session-' pattern for gemini
                if key.endswith(".md") or key.endswith(".summary.txt"):
                    continue
                
                # For Gemini, we are looking for 'session-*.json' 
                # For others, we might have different old patterns, but let's focus on cleaning up .json files that seem to be single-file archives.
                
                # If it's the new format .json (companion to .md), we should skip it.
                # How to tell? The new format is YYYY-MM-DD-slug.json.
                # Old format: session-YYYY-MM-DD-hash.json (Gemini)
                # Or task-id.json (Roo - hypothetical)
                
                # Heuristic: If there is a corresponding .md file, it's likely already converted/new format.
                base_name = key.replace(".json", "")
                try:
                    archiver.s3_client.head_object(Bucket=bucket, Key=f"{base_name}.md")
                    # If .md exists, this json is likely the raw data companion of the new format.
                    # Verify if naming convention matches date-slug.
                    if re.match(r'.*/\d{4}-\d{2}-\d{2}-[\w-]+\.json$', key):
                         continue
                except Exception:
                    # .md does not exist, so this is likely an old archive file to be migrated.
                    pass

                print(f"Migrating {key}...")
                
                try:
                    response = archiver.s3_client.get_object(Bucket=bucket, Key=key)
                    content = response['Body'].read().decode('utf-8')
                    
                    data = json.loads(content)
                    
                    raw_content = data
                    summary = None
                    
                    # Check if it's a wrapper
                    if isinstance(data, dict) and "raw_content" in data and "summary" in data:
                        raw_content = data["raw_content"]
                        summary = data["summary"]
                        # We ignore the 'markdown' in the wrapper, we'll regenerate it to be safe/consistent.
                    
                    # Regenerate markdown to ensure latest format
                    markdown_content = archiver.to_markdown(category, raw_content)
                    
                    if not summary:
                        print(f"Generating summary for {key}...")
                        summary = archiver.get_summary(markdown_content)
                    
                    # Generate Slug
                    slug = archiver.slugify(summary)
                    if not slug:
                        slug = "conversation"

                    # Extract Date
                    date_str = "2025-01-01" # Default
                    # Try from filename
                    match = re.search(r'(\d{4}-\d{2}-\d{2})', key)
                    if match:
                        date_str = match.group(1)
                    else:
                        # Try from content if wrapper
                        if isinstance(data, dict) and "archived_at" in data:
                             date_str = data["archived_at"][:10]
                    
                    new_base_name = f"{date_str}-{slug}"
                    new_prefix = f"{prefix}{category}/{new_base_name}"
                    
                    # Prepare uploads
                    full_markdown = f"# Summary\n{summary}\n\n{markdown_content}"
                    
                    uploads = [
                        {"key": f"{new_prefix}.md", "body": full_markdown, "type": "text/markdown"},
                        {"key": f"{new_prefix}.summary.txt", "body": summary, "type": "text/plain"},
                        {"key": f"{new_prefix}.json", "body": json.dumps(raw_content, indent=2), "type": "application/json"}
                    ]
                    
                    for upload in uploads:
                        archiver.s3_client.put_object(
                            Bucket=bucket,
                            Key=upload['key'],
                            Body=upload['body'],
                            ContentType=upload['type']
                        )
                        
                    # Delete old file
                    archiver.s3_client.delete_object(Bucket=bucket, Key=key)
                    print(f"Migrated {key} -> {new_base_name}")

                except Exception as e:
                    print(f"Failed to migrate {key}: {e}")

if __name__ == "__main__":
    main()
