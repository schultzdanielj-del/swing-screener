"""
Bulk mirror — one-time upload of all existing local JSON files to Railway.

Run from repo root on your machine:
    python scripts/bulk_mirror.py

Walks local_runner/cache/ and data/, uploads every .json file to Railway's
file_mirror table. Skips manifests (_manifest.json) and expr_series cache.
"""

import os
import sys
import requests
import time

API_BASE = "https://web-production-e3025.up.railway.app"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIRS_TO_SCAN = [
    os.path.join(REPO_ROOT, "local_runner", "cache"),
    os.path.join(REPO_ROOT, "data"),
]

# Skip these — regenerable infrastructure, not grinder results
SKIP_PATTERNS = [
    "_manifest.json",
    "expr_series",
    "market_series",
]


def should_skip(filepath):
    for pat in SKIP_PATTERNS:
        if pat in filepath:
            return True
    return False


def upload_file(filepath):
    """Read a local JSON file and upload to Railway. Returns (ok, size_bytes)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = f.read()

        rel_path = os.path.relpath(filepath, REPO_ROOT).replace("\\", "/")

        r = requests.post(
            f"{API_BASE}/api/v2/files",
            json={"path": rel_path, "data": data},
            timeout=120,
        )
        if r.status_code == 200:
            return True, len(data)
        else:
            print(f"  FAILED: {rel_path} — HTTP {r.status_code}: {r.text[:100]}")
            return False, 0
    except Exception as e:
        print(f"  FAILED: {filepath} — {e}")
        return False, 0


def main():
    # Collect all .json files
    files = []
    for scan_dir in DIRS_TO_SCAN:
        if not os.path.isdir(scan_dir):
            print(f"  Skipping (not found): {scan_dir}")
            continue
        for root, dirs, filenames in os.walk(scan_dir):
            for fn in filenames:
                if fn.endswith(".json"):
                    full = os.path.join(root, fn)
                    if not should_skip(full):
                        files.append(full)

    print(f"Found {len(files)} JSON files to upload")
    print(f"Total size: {sum(os.path.getsize(f) for f in files) / 1024 / 1024:.1f} MB")
    print()

    uploaded = 0
    failed = 0
    total_bytes = 0
    t0 = time.time()

    for i, filepath in enumerate(sorted(files), 1):
        rel = os.path.relpath(filepath, REPO_ROOT).replace("\\", "/")
        size_kb = os.path.getsize(filepath) / 1024
        sys.stdout.write(f"  [{i}/{len(files)}] {rel} ({size_kb:.0f} KB)...")
        sys.stdout.flush()

        ok, nbytes = upload_file(filepath)
        if ok:
            uploaded += 1
            total_bytes += nbytes
            print(" OK")
        else:
            failed += 1

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s")
    print(f"  Uploaded: {uploaded}")
    print(f"  Failed:   {failed}")
    print(f"  Total:    {total_bytes / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
