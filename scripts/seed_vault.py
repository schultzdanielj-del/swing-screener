"""
Seed Vault — Backup critical data to Railway file mirror.

Usage:
    python scripts/seed_vault.py              # Backup to Railway
    python scripts/seed_vault.py --restore    # Restore from Railway into local DB

Backs up everything needed to rebuild a fully operational system:
  - SQLite tables: examples, setups, earnings, pending, rejected, grind cycles,
    conditions, signals, exit conditions, health, regime model, scores, watchlist
  - Vetting JSON files: signal filter outputs, vetting decisions, setup refiner outputs

Recovery process (new machine):
  1. Clone repo from GitHub
  2. python scripts/seed_vault.py --restore
  3. python local_runner/cache_builder.py --5yr --force   (~30 min)
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
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# Tables to back up — every table with recoverable state
BACKUP_TABLES = [
    "examples",
    "setups",
    "earnings_dates",
    "pending_examples",
    "rejected_signals",
    "grind_cycles",
    "cycle_conditions",
    "cycle_signals",
    "cycle_sacrificial_signals",
    "exit_conditions",
    "cycle_health",
    "regime_model",
    "signal_regime_scores",
    "nightly_watchlist",
]

# JSON file patterns to back up (relative to data/)
BACKUP_FILE_PATTERNS = [
    "signal_filter/filtered_*.json",
    "vetting/vetting_*.json",
    "setup_refiner/refined_*.json",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upload_to_railway(path, data_str):
    """Upload a file to Railway's file mirror."""
    try:
        r = requests.post(f"{RAILWAY_API}/api/v2/files", json={
            "path": path,
            "data": data_str,
        }, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ✗ Upload {path} failed: {e}")
        return False


def download_from_railway(path):
    """Download a file from Railway's file mirror. Returns string or None."""
    try:
        r = requests.get(f"{RAILWAY_API}/api/v2/files/{path}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ✗ Download {path} failed: {e}")
        return None


def list_railway_files(prefix):
    """List files on Railway with a given prefix."""
    try:
        r = requests.get(f"{RAILWAY_API}/api/v2/files", params={"prefix": prefix}, timeout=15)
        r.raise_for_status()
        return [f["path"] for f in r.json().get("files", [])]
    except Exception as e:
        print(f"  ✗ List {prefix} failed: {e}")
        return []


# ════════════════════════════════════════════════════════════
# BACKUP
# ════════════════════════════════════════════════════════════

def backup():
    print("=" * 60)
    print("  Seed Vault — Backup to Railway")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if not os.path.exists(DB_PATH):
        print(f"\n  ✗ DB not found: {DB_PATH}")
        sys.exit(1)

    db = get_db()
    total_uploaded = 0

    # ── Back up SQLite tables ──
    print("\n── SQLite Tables ──")
    for table in BACKUP_TABLES:
        try:
            rows = db.execute(f"SELECT * FROM {table}").fetchall()
            data = [dict(r) for r in rows]
            data_str = json.dumps(data, indent=2, default=str)
            path = f"seed/{table}.json"
            if upload_to_railway(path, data_str):
                print(f"  ✓ {table}: {len(data)} rows ({len(data_str)} bytes)")
                total_uploaded += 1
            else:
                print(f"  ✗ {table}: upload failed")
        except Exception as e:
            print(f"  ✗ {table}: {e}")

    # ── Back up JSON files ──
    print("\n── JSON Files ──")
    for pattern in BACKUP_FILE_PATTERNS:
        full_pattern = os.path.join(DATA_DIR, pattern)
        files = glob.glob(full_pattern)
        if not files:
            print(f"  · {pattern}: no files")
            continue
        for filepath in files:
            try:
                with open(filepath) as f:
                    data_str = f.read()
                # Store relative to data/
                rel_path = os.path.relpath(filepath, DATA_DIR)
                seed_path = f"seed/files/{rel_path}"
                if upload_to_railway(seed_path, data_str):
                    size_kb = len(data_str) / 1024
                    print(f"  ✓ {rel_path} ({size_kb:.0f} KB)")
                    total_uploaded += 1
            except Exception as e:
                print(f"  ✗ {filepath}: {e}")

    # ── Upload manifest ──
    manifest = {
        "backed_up_at": datetime.now().isoformat(),
        "tables": BACKUP_TABLES,
        "file_patterns": BACKUP_FILE_PATTERNS,
        "db_path": DB_PATH,
    }
    upload_to_railway("seed/manifest.json", json.dumps(manifest, indent=2))

    db.close()
    print(f"\n{'=' * 60}")
    print(f"  Done — {total_uploaded} items backed up")
    print(f"{'=' * 60}\n")


# ════════════════════════════════════════════════════════════
# RESTORE
# ════════════════════════════════════════════════════════════

def restore():
    print("=" * 60)
    print("  Seed Vault — Restore from Railway")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Ensure DB exists (init_db creates tables)
    if not os.path.exists(DB_PATH):
        print(f"\n  DB not found — creating via server init...")
        import server  # triggers init_db()
        print(f"  ✓ DB created at {DB_PATH}")

    db = get_db()
    db.execute("PRAGMA foreign_keys=OFF")  # Disable during bulk import
    total_restored = 0

    # ── Check manifest ──
    manifest_str = download_from_railway("seed/manifest.json")
    if manifest_str:
        manifest = json.loads(manifest_str)
        print(f"\n  Last backup: {manifest.get('backed_up_at', '?')}")
    else:
        print("\n  ⚠ No manifest found — attempting restore anyway")

    # ── Restore SQLite tables ──
    print("\n── SQLite Tables ──")
    for table in BACKUP_TABLES:
        path = f"seed/{table}.json"
        data_str = download_from_railway(path)
        if data_str is None:
            print(f"  · {table}: not found on Railway")
            continue
        try:
            rows = json.loads(data_str)
            if not rows:
                print(f"  · {table}: empty")
                continue
            # Get column names from first row
            cols = list(rows[0].keys())
            # Skip auto-increment id columns for tables that have them
            if "id" in cols and table not in ("setups", "exit_conditions", "regime_model", "cycle_health"):
                cols_no_id = [c for c in cols if c != "id"]
            else:
                cols_no_id = cols
            placeholders = ",".join("?" for _ in cols_no_id)
            col_names = ",".join(cols_no_id)
            inserted = 0
            for row in rows:
                try:
                    vals = [row.get(c) for c in cols_no_id]
                    db.execute(f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})", vals)
                    if db.execute("SELECT changes()").fetchone()[0] > 0:
                        inserted += 1
                except Exception as e:
                    pass  # Skip duplicates silently
            db.commit()
            print(f"  ✓ {table}: {inserted} inserted ({len(rows)} in backup)")
            total_restored += inserted
        except Exception as e:
            print(f"  ✗ {table}: {e}")

    # ── Restore JSON files ──
    print("\n── JSON Files ──")
    seed_files = list_railway_files("seed/files/")
    if not seed_files:
        print("  · No JSON files in seed vault")
    for seed_path in seed_files:
        data_str = download_from_railway(seed_path)
        if data_str is None:
            continue
        # seed/files/signal_filter/filtered_dtss.json → data/signal_filter/filtered_dtss.json
        rel_path = seed_path.replace("seed/files/", "", 1)
        local_path = os.path.join(DATA_DIR, rel_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            with open(local_path, "w") as f:
                f.write(data_str)
            print(f"  ✓ {rel_path}")
            total_restored += 1
        except Exception as e:
            print(f"  ✗ {rel_path}: {e}")

    db.execute("PRAGMA foreign_keys=ON")
    db.close()

    print(f"\n{'=' * 60}")
    print(f"  Done — {total_restored} items restored")
    print(f"  Next steps:")
    print(f"    python local_runner/cache_builder.py --5yr --force")
    print(f"    python local_runner/expr_cache_builder.py --build")
    print(f"    python local_runner/nightly.py --force")
    print(f"    python -m uvicorn server:app --port 8000")
    print(f"{'=' * 60}\n")


def main():
    if "--restore" in sys.argv:
        restore()
    else:
        backup()


if __name__ == "__main__":
    main()
