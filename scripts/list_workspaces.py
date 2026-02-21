import os
import yaml
import glob
import csv
import re
from pathlib import Path
import subprocess

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

def get_workspace_info(file_path, category):
    p = Path(file_path)
    if category == 'gemini':
        # path/to/tmp/<workspace_id>/chats/session-*.json
        return str(p.parent.parent), p.parent.parent.name
    elif category == 'roo':
        # path/to/tasks/<task_id>/ui_messages.json
        # Try to extract from JSON if possible, otherwise parent of tasks
        try:
            with open(file_path, 'r') as f:
                content = f.read(10000) # Read first bit
                # Look for "Current Workspace Directory (path)"
                match = re.search(r'Current Workspace Directory \((.*?)\)', content)
                if match:
                    ws_path = match.group(1)
                    return ws_path, Path(ws_path).name
        except Exception:
            pass
        return str(p.parent.parent.parent), p.parent.parent.parent.name
    elif category == 'aider':
        # path/to/repo/.aider.chat.history.md
        return str(p.parent), p.parent.name
    return "Unknown", "Unknown"

def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    
    workspaces = {} # (path, category) -> count

    for category in ['gemini', 'roo', 'aider']:
        patterns = config['paths'].get(category, [])
        files = discover_files(patterns)
        for f in files:
            ws_path, ws_name = get_workspace_info(f, category)
            key = (ws_path, category)
            workspaces[key] = workspaces.get(key, 0) + 1

    os.makedirs("data", exist_ok=True)
    with open("data/workspaces.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Category", "Workspace Path", "Chat Count"])
        for (ws_path, category), count in sorted(workspaces.items()):
            writer.writerow([category, ws_path, count])

    print(f"Discovered {len(workspaces)} workspaces. Results saved to data/workspaces.csv")

if __name__ == "__main__":
    main()
