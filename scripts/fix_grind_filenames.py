"""
Fix pyramid grind filenames — replace fake signal counts with real deduped counts.

Reads each pyramid_*.json, computes the real deduped signal count from the last
5yr tier's final_signals, renames the file locally and on Railway.

Usage (from repo root):
    python scripts/fix_grind_filenames.py

Dry run first (no changes):
    python scripts/fix_grind_filenames.py --dry-run
"""

import os
import sys
import json
import argparse
import requests
from collections import Counter

API_BASE = "https://web-production-e3025.up.railway.app"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def compute_real_counts(data):
    """Compute real deduped signal count from the last 5yr tier."""
    tr = data.get("tier_results", {})

    for pass_prefix in ["monthly_5yr", "weekly_5yr", "daily_5yr", "5yr"]:
        if pass_prefix not in tr:
            continue
        sigs = tr[pass_prefix].get("final_signals", [])
        if not sigs:
            continue

        # Dedupe by (ticker, date)
        seen = set()
        deduped = []
        for s in sigs:
            key = (s["ticker"], s["date"])
            if key not in seen:
                seen.add(key)
                deduped.append(s)

        total = len(deduped)
        date_counts = Counter(s["date"] for s in deduped)
        peak = max(date_counts.values()) if date_counts else 0
        return total, peak, pass_prefix

    return None, None, None


def fix_filename(old_name, real_total, real_peak):
    """Replace sig{N}_pk{N} in filename with real numbers."""
    import re
    new_name = re.sub(r'_sig\d+_pk\d+_', f'_sig{real_total}_pk{real_peak}_', old_name)
    return new_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show changes without doing them")
    args = parser.parse_args()

    # Get all pyramid files from Railway
    r = requests.get(f"{API_BASE}/api/v2/files?prefix=local_runner/cache/pyramid_")
    files = r.json().get("files", [])

    print(f"Found {len(files)} pyramid files on Railway")
    print()

    changes = []

    for f in sorted(files, key=lambda x: x["path"]):
        old_path = f["path"]
        old_name = os.path.basename(old_path)

        if "_sig" not in old_name or "_pk" not in old_name:
            continue
        # Skip non-timestamped files (like pyramid_results_dtss.json)
        if old_name.count("_") < 5:
            continue

        # Fetch the file
        resp = requests.get(f"{API_BASE}/api/v2/files/{old_path}")
        if resp.status_code != 200:
            print(f"  SKIP (fetch error): {old_path}")
            continue

        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            print(f"  SKIP (parse error): {old_path}")
            continue

        real_total, real_peak, source_tier = compute_real_counts(data)

        if real_total is None:
            print(f"  SKIP (no 5yr tier data): {old_path}")
            continue

        new_name = fix_filename(old_name, real_total, real_peak)
        new_path = old_path.replace(old_name, new_name)

        if old_name == new_name:
            print(f"  OK (already correct): {old_name}")
            continue

        # Also update the summary in the JSON
        data["summary"] = {
            "final_total": real_total,
            "final_peak": real_peak,
            "final_avg": round(real_total / max(1, len(set(s["date"] for s in [] ))), 1),
        }
        # Recompute avg properly
        tr = data.get("tier_results", {})
        for pp in ["monthly_5yr", "weekly_5yr", "daily_5yr", "5yr"]:
            if pp in tr and tr[pp].get("final_signals"):
                sigs = tr[pp]["final_signals"]
                seen = set()
                deduped = []
                for s in sigs:
                    key = (s["ticker"], s["date"])
                    if key not in seen:
                        seen.add(key)
                        deduped.append(s)
                date_counts = Counter(s["date"] for s in deduped)
                avg = round(sum(date_counts.values()) / max(len(date_counts), 1), 1)
                data["summary"]["final_avg"] = avg
                break

        changes.append({
            "old_path": old_path,
            "new_path": new_path,
            "old_name": old_name,
            "new_name": new_name,
            "real_total": real_total,
            "real_peak": real_peak,
            "data": data,
        })

        print(f"  {old_name}")
        print(f"    → {new_name}  ({real_total} signals, peak {real_peak})")

    print(f"\n{len(changes)} files to rename")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return

    if not changes:
        print("Nothing to do.")
        return

    # Apply changes
    local_cache = os.path.join(REPO_ROOT, "local_runner", "cache")

    for c in changes:
        new_json = json.dumps(c["data"], indent=2)

        # 1. Upload with new path to Railway
        resp = requests.post(f"{API_BASE}/api/v2/files",
                             json={"path": c["new_path"], "data": new_json},
                             timeout=120)
        if resp.status_code == 200:
            print(f"  Railway: uploaded {c['new_name']}")
        else:
            print(f"  Railway: FAILED to upload {c['new_name']} — {resp.status_code}")
            continue

        # 2. Delete old path from Railway
        resp = requests.delete(f"{API_BASE}/api/v2/files/{c['old_path']}", timeout=30)
        if resp.status_code == 200:
            print(f"  Railway: deleted {c['old_name']}")
        else:
            print(f"  Railway: FAILED to delete {c['old_name']} — {resp.status_code}")

        # 3. Rename locally if the file exists
        old_local = os.path.join(local_cache, c["old_name"])
        new_local = os.path.join(local_cache, c["new_name"])
        if os.path.exists(old_local):
            # Write new content (with fixed summary)
            with open(new_local, "w") as f:
                f.write(new_json)
            os.remove(old_local)
            print(f"  Local: {c['old_name']} → {c['new_name']}")
        else:
            # Just write the new file locally
            with open(new_local, "w") as f:
                f.write(new_json)
            print(f"  Local: created {c['new_name']} (old file not found)")

    print(f"\nDone. {len(changes)} files renamed.")


if __name__ == "__main__":
    main()
