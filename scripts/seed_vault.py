"""
Seed Vault — Verify, backup, and restore from Railway.

Usage:
    python scripts/seed_vault.py              # Verify Railway has everything
    python scripts/seed_vault.py --backup     # Upload any local grind files missing from Railway
    python scripts/seed_vault.py --restore    # Restore from Railway to local machine

Railway is the authority. Grinders mirror their output to Railway automatically
via file_mirror.py, and structured data is written to Railway's DB via API.
The --backup flag catches any files where mirror_file() silently failed.

Recovery process (new machine):
  1. Clone repo from GitHub
  2. python scripts/seed_vault.py --restore        (~5 min)
  3. python local_runner/cache_builder.py --daily --force   (~30 min)
  4. python local_runner/expr_cache_builder.py --build     (~2 hrs)
  5. python local_runner/nightly.py --force                (~20 min)
  6. python -m uvicorn server:app --port 8000              (done)
"""

import os
import sys
import json
import glob
import sqlite3
import requests
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

RAILWAY_API = "https://web-production-e3025.up.railway.app"
DB_PATH = os.path.join(PROJECT_ROOT, "data", "scanperfect.db")

# Tables that are rebuilt by morning cache scripts or are infrastructure.
# Everything NOT in this list gets backed up and restored.
EXCLUDE_TABLES = {
    "ohlcv", "universe_ohlcv",                          # cache_builder rebuilds
    "tradable_universe", "universe_tickers",             # fetch_universe rebuilds
    "universe_fetch_status", "ticker_sectors",           # fetch_universe rebuilds
    "file_mirror",                                       # mirror storage itself
    "task_queue", "research_jobs",                       # transient job queues
    "sqlite_sequence",                                   # SQLite internal
}

# Local directories containing grind outputs that must be on Railway.
# These are the dirs where grinders write JSON files.
# New setup types write to the same dirs, so no per-setup config needed.
LOCAL_GRIND_DIRS = [
    os.path.join(PROJECT_ROOT, "local_runner", "cache"),
    os.path.join(PROJECT_ROOT, "data", "exit_grind"),
    os.path.join(PROJECT_ROOT, "data", "signal_exit_grind"),
    os.path.join(PROJECT_ROOT, "data", "signal_filter"),
    os.path.join(PROJECT_ROOT, "data", "profit_grind"),
]


def railway_query(sql, limit=50000):
    """Run a SELECT against Railway's DB. Returns list of dicts."""
    try:
        r = requests.post(f"{RAILWAY_API}/api/query/bulk",
                          json={"sql": sql, "limit": limit}, timeout=120)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        print(f"  ✗ Query failed: {e}")
        return []


def discover_tables():
    """Get all table names from Railway, minus the exclude list."""
    all_tables = railway_query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [r["name"] for r in all_tables if r["name"] not in EXCLUDE_TABLES]


def list_mirrored_files():
    """List all non-seed files on Railway's file mirror."""
    try:
        r = requests.get(f"{RAILWAY_API}/api/v2/files", timeout=30)
        r.raise_for_status()
        all_files = r.json().get("files", [])
        return [f for f in all_files if not f["path"].startswith("seed/")]
    except Exception as e:
        print(f"  ✗ Failed to list files: {e}")
        return []


def download_file(path):
    """Download a file from Railway's file mirror. Returns string or None."""
    try:
        r = requests.get(f"{RAILWAY_API}/api/v2/files/{path}", timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ✗ Download {path} failed: {e}")
        return None


def upload_file(rel_path, data_str):
    """Upload a file to Railway's file mirror. Returns True on success."""
    try:
        r = requests.post(f"{RAILWAY_API}/api/v2/files", json={
            "path": rel_path,
            "data": data_str,
        }, timeout=60)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ✗ Upload {rel_path} failed: {e}")
        return False


# ════════════════════════════════════════════════════════════
# BACKUP — catch failed mirrors
# ════════════════════════════════════════════════════════════

def backup():
    print("=" * 60)
    print("  Seed Vault — Backup (catch failed mirrors)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Get set of paths already on Railway
    mirrored = list_mirrored_files()
    railway_paths = set()
    for f in mirrored:
        # Normalize to forward slashes for comparison
        railway_paths.add(f["path"].replace("\\", "/"))

    print(f"\n  Railway has {len(railway_paths)} mirrored files")

    # Scan local grind dirs for JSON files missing from Railway
    missing = []
    for grind_dir in LOCAL_GRIND_DIRS:
        if not os.path.isdir(grind_dir):
            continue
        for fname in os.listdir(grind_dir):
            if not fname.endswith(".json"):
                continue
            local_path = os.path.join(grind_dir, fname)
            rel_path = os.path.relpath(local_path, PROJECT_ROOT).replace("\\", "/")
            if rel_path not in railway_paths:
                missing.append((rel_path, local_path))

    if not missing:
        print("  ✓ All local grind files are on Railway — nothing to upload")
        print(f"\n{'=' * 60}\n")
        return

    print(f"  Found {len(missing)} local files missing from Railway\n")

    uploaded = 0
    failed = 0
    for rel_path, local_path in missing:
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data_str = f.read()
            if upload_file(rel_path, data_str):
                size_kb = len(data_str) / 1024
                print(f"  ✓ {rel_path} ({size_kb:.0f} KB)")
                uploaded += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ {rel_path}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Uploaded: {uploaded} files")
    if failed:
        print(f"  Failed: {failed} files")
    print(f"{'=' * 60}\n")


# ════════════════════════════════════════════════════════════
# VERIFY
# ════════════════════════════════════════════════════════════

def verify():
    print("=" * 60)
    print("  Seed Vault — Verify Railway")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── Check DB tables ──
    print("\n── DB Tables ──")
    tables = discover_tables()
    if not tables:
        print("  ✗ Could not discover tables from Railway")
        return

    total_rows = 0
    empty_tables = []
    for table in tables:
        rows = railway_query(f"SELECT COUNT(*) as n FROM {table}")
        n = rows[0]["n"] if rows else 0
        total_rows += n
        if n > 0:
            print(f"  ✓ {table}: {n} rows")
        else:
            empty_tables.append(table)

    if empty_tables:
        print(f"  · Empty: {', '.join(empty_tables)}")

    # ── Check mirrored files ──
    print("\n── Mirrored Grind Files ──")
    files = list_mirrored_files()
    total_bytes = sum(f["size_bytes"] for f in files)

    # Group by directory
    dirs = {}
    for f in files:
        clean = f["path"].replace("\\", "/")
        d = os.path.dirname(clean)
        dirs[d] = dirs.get(d, 0) + 1

    for d in sorted(dirs):
        print(f"  ✓ {d}/: {dirs[d]} files")

    print(f"\n{'=' * 60}")
    print(f"  DB: {total_rows} rows across {len(tables)} tables")
    print(f"  Files: {len(files)} files ({total_bytes / 1024 / 1024:.1f} MB)")
    print(f"{'=' * 60}\n")


# ════════════════════════════════════════════════════════════
# RESTORE
# ════════════════════════════════════════════════════════════

def restore():
    print("=" * 60)
    print("  Seed Vault — Restore from Railway")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── Ensure local DB exists ──
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.exists(DB_PATH):
        print(f"\n  DB not found — creating via server init...")
        import server  # triggers init_db()
        print(f"  ✓ DB created at {DB_PATH}")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA journal_mode=WAL")

    # ── Discover and restore tables ──
    print("\n── DB Tables ──")
    tables = discover_tables()
    if not tables:
        print("  ✗ Could not discover tables from Railway")
        db.close()
        return

    total_rows_restored = 0
    for table in tables:
        rows = railway_query(f"SELECT * FROM {table}")
        if not rows:
            print(f"  · {table}: empty")
            continue

        # Check table exists locally
        local_tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        if table not in local_tables:
            print(f"  ⚠ {table}: not in local schema, skipping")
            continue

        # Get local column names to match against
        local_cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]

        # Use only columns that exist in both Railway data and local schema
        railway_cols = list(rows[0].keys())
        cols = [c for c in railway_cols if c in local_cols]
        if not cols:
            print(f"  ⚠ {table}: no matching columns, skipping")
            continue

        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        inserted = 0

        for row in rows:
            try:
                vals = [row.get(c) for c in cols]
                db.execute(
                    f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                    vals
                )
                inserted += 1
            except Exception:
                pass  # skip rows that fail constraints

        db.commit()
        total_rows_restored += inserted
        print(f"  ✓ {table}: {inserted} rows")

    # ── Restore mirrored grind files ──
    print("\n── Mirrored Grind Files ──")
    files = list_mirrored_files()
    if not files:
        print("  · No mirrored files on Railway")

    files_restored = 0
    files_failed = 0
    total_bytes = 0

    for entry in files:
        path = entry["path"]
        clean_path = path.replace("\\", "/")
        local_path = os.path.join(PROJECT_ROOT, clean_path)

        data_str = download_file(path)
        if data_str is None:
            files_failed += 1
            continue

        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            with open(local_path, "w") as f:
                f.write(data_str)
            size_kb = len(data_str) / 1024
            total_bytes += len(data_str)
            print(f"  ✓ {clean_path} ({size_kb:.0f} KB)")
            files_restored += 1
        except Exception as e:
            print(f"  ✗ {clean_path}: {e}")
            files_failed += 1

    db.execute("PRAGMA foreign_keys=ON")
    db.close()

    print(f"\n{'=' * 60}")
    print(f"  Restored:")
    print(f"    DB: {total_rows_restored} rows across {len(tables)} tables")
    print(f"    Files: {files_restored} files ({total_bytes / 1024 / 1024:.1f} MB)")
    if files_failed:
        print(f"    Failed: {files_failed} files")
    print(f"\n  Next steps:")
    print(f"    python local_runner/cache_builder.py --daily --force")
    print(f"    python local_runner/expr_cache_builder.py --build")
    print(f"    python local_runner/nightly.py --force")
    print(f"    python -m uvicorn server:app --port 8000")
    print(f"{'=' * 60}\n")


def main():
    if "--restore" in sys.argv:
        restore()
    elif "--backup" in sys.argv:
        backup()
    else:
        verify()


if __name__ == "__main__":
    main()
