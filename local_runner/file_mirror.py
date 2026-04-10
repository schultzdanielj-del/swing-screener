"""
File Mirror — Upload local JSON files to Railway as exact copies.

Every grinder calls mirror_file() right after json.dump().
The file is uploaded verbatim to Railway's file_mirror table,
retrievable by its local path.

Usage:
    from file_mirror import mirror_file
    
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    mirror_file(out_path)

Never raises — logs warnings on failure. Never blocks the grinder.
"""

import os
import requests

API_BASE = "https://web-production-e3025.up.railway.app"


def mirror_file(filepath):
    """Read a local file and upload it to Railway. Never raises."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = f.read()

        # Use path relative to repo root for consistency
        # e.g. "local_runner/cache/pyramid_dtss_mp_20260309.json"
        #   or "data/exit_grind/exit_grind_dtss_20260309.json"
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.abspath(filepath).startswith(repo_root):
            rel_path = os.path.relpath(filepath, repo_root)
        else:
            rel_path = os.path.basename(filepath)

        # Normalize to forward slashes
        rel_path = rel_path.replace("\\", "/")

        r = requests.post(
            f"{API_BASE}/api/v2/files",
            json={"path": rel_path, "data": data},
            timeout=60,
        )
        if r.status_code == 200:
            size_kb = len(data) / 1024
            print(f"  [mirror] OK {rel_path} ({size_kb:.0f} KB)")
        else:
            print(f"  [mirror] WARNING: upload failed HTTP {r.status_code}: {r.text[:200]}")

    except Exception as e:
        print(f"  [mirror] WARNING: {e}")
