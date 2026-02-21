import hashlib
from pathlib import Path

def get_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def main():
    # Roots to scan for workspaces
    search_roots = [
        "/home/mstouffer/repos",
        "/home/mstouffer/repos/turboheatweldingtools"
    ]
    
    # Existing hash folders in gemini tmp
    gemini_tmp = Path("/home/mstouffer/.gemini/tmp")
    existing_hashes = {d.name: d for d in gemini_tmp.iterdir() if d.is_dir() and len(d.name) == 64}
    
    print(f"Found {len(existing_hashes)} existing hash folders in {gemini_tmp}")
    
    # Map paths to hashes
    path_to_hash = {}
    hash_to_path = {}
    
    for root in search_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
            
        for item in root_path.iterdir():
            if item.is_dir():
                abs_path = str(item.absolute())
                h = get_sha256(abs_path)
                path_to_hash[abs_path] = h
                hash_to_path[h] = abs_path

    # Compare
    matched = []
    unmatched = []
    
    for h in existing_hashes:
        if h in hash_to_path:
            matched.append((h, hash_to_path[h]))
        else:
            unmatched.append(h)
            
    print("\n--- MATCHED WORKSPACES ---")
    for h, p in sorted(matched, key=lambda x: x[1]):
        print(f"{h[:8]}... -> {p}")
        
    print("\n--- UNMATCHED HASHES ---")
    for h in sorted(unmatched):
        # Try to peek into the logs of unmatched hashes to see if we can find clues
        log_path = gemini_tmp / h / "logs.json"
        clue = ""
        if log_path.exists():
            try:
                with open(log_path, 'r') as f:
                    content = f.read(2000)
                    import re
                    match = re.search(r'working in the following directories:\s+- (/[^\s]+)', content)
                    if match:
                        clue = f" (Likely: {match.group(1)})"
            except Exception:
                pass
        print(f"{h}{clue}")

if __name__ == "__main__":
    main()
