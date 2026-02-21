import os
import hashlib
import json
import csv
import yaml
import glob
from pathlib import Path
import subprocess
import re

def get_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def is_gemini_commit_only(file_path):
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
        
        messages = data.get("messages", [])
        if not messages:
            return True

        # Check the very first message
        first_msg_raw = messages[0].get("content", "")
        if isinstance(first_msg_raw, list):
            first_msg_text = " ".join([str(p) for p in first_msg_raw])
        else:
            first_msg_text = str(first_msg_raw)
        
        if not first_msg_text.strip():
            return True

        first_msg_lower = first_msg_text.lower()
        commit_patterns = [
            "git diff --cached",
            "create a git commit",
            "perform the commit without asking",
            "create a git commit for all staged",
            "the commit has been successfully created"
        ]
        
        is_commit_prompt = any(pattern in first_msg_lower for pattern in commit_patterns)
        if is_commit_prompt and len(messages) <= 15:
            return True
            
        return False
    except Exception:
        return False

def discover_files(patterns):
    files = []
    for pattern in patterns:
        expanded_pattern = str(Path(pattern).expanduser())
        if "**" in expanded_pattern:
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
            except subprocess.CalledProcessError:
                pass
        else:
            found = glob.glob(expanded_pattern, recursive=True)
            files.extend(found)
    return sorted(list(set(files)))

def main():
    # 1. Map local paths to hashes
    search_roots = [
        "/home/mstouffer/repos",
        "/home/mstouffer/repos/turboheatweldingtools"
    ]
    
    hash_to_path = {}
    for root in search_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for item in root_path.iterdir():
            if item.is_dir():
                abs_path = str(item.absolute())
                hash_to_path[get_sha256(abs_path)] = abs_path

    # 2. Scan Gemini tmp for hashes
    gemini_tmp = Path("/home/mstouffer/.gemini/tmp")
    existing_hashes = [d.name for d in gemini_tmp.iterdir() if d.is_dir() and len(d.name) == 64]
    
    # 3. Build index
    index = {
        "workspaces": [],
        "unmatched_hashes": []
    }
    
    # Load config for counts
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    # Gemini Processing
    for h in existing_hashes:
        path_resolved = hash_to_path.get(h)
        all_chat_files = glob.glob(str(gemini_tmp / h / "chats" / "session-*.json"))
        
        # Filter out commit-only chats
        valuable_chats = [f for f in all_chat_files if not is_gemini_commit_only(f)]
        
        ws_info = {
            "hash": h,
            "category": "gemini",
            "resolved_path": path_resolved,
            "chat_count": len(valuable_chats),
            "tmp_path": str(gemini_tmp / h)
        }
        
        if not path_resolved:
            # Try to find a clue in logs.json
            log_path = gemini_tmp / h / "logs.json"
            if log_path.exists():
                try:
                    with open(log_path, 'r') as f:
                        content = f.read(5000)
                        match = re.search(r'working in the following directories:\s+- (/[^\s]+)', content)
                        if match:
                            ws_info["clue_path"] = match.group(1)
                except Exception:
                    pass
            index["unmatched_hashes"].append(ws_info)
        else:
            index["workspaces"].append(ws_info)

    # Roo and Aider processing (simple counts for now)
    for category in ['roo', 'aider']:
        patterns = config['paths'].get(category, [])
        files = discover_files(patterns)
        
        temp_groups = {} # path -> count
        for f in files:
            p = Path(f)
            if category == 'roo':
                # Heuristic for Roo: try to find workspace directory in content
                try:
                    with open(f, 'r') as content_f:
                        c = content_f.read(10000)
                        m = re.search(r'Current Workspace Directory \((.*?)\)', c)
                        ws = m.group(1) if m else str(p.parent.parent.parent)
                except Exception:
                    ws = str(p.parent.parent.parent)
            else: # aider
                ws = str(p.parent)
            
            temp_groups[ws] = temp_groups.get(ws, 0) + 1
            
        for ws, count in temp_groups.items():
            index["workspaces"].append({
                "hash": None,
                "category": category,
                "resolved_path": ws,
                "chat_count": count
            })

    # 4. Save JSON
    os.makedirs("data", exist_ok=True)
    with open("data/workspace_index.json", "w") as f:
        json.dump(index, f, indent=2)

    # 5. Save CSV
    with open("data/workspaces.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Category", "Resolved Path", "Chat Count", "Gemini Hash", "Clue Path (if unmatched)"])
        # Sort workspaces for consistency
        all_entries = index["workspaces"] + index["unmatched_hashes"]
        for entry in sorted(all_entries, key=lambda x: (x['category'], str(x['resolved_path']))):
            writer.writerow([
                entry['category'],
                entry.get('resolved_path') or "UNMATCHED",
                entry['chat_count'],
                entry['hash'] or "N/A",
                entry.get('clue_path') or ""
            ])

    print(f"Index generated: {len(index['workspaces'])} matched, {len(index['unmatched_hashes'])} unmatched.")
    print("Saved to data/workspace_index.json and data/workspaces.csv")

if __name__ == "__main__":
    main()
