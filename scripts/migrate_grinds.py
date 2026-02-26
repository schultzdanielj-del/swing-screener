"""
One-time migration: organize existing grind files into standardized structure.

Run from repo root:
    python scripts/migrate_grinds.py

What it does:
    1. Creates local_runner/grinds/dtss/{signal,exit,outcome,market}/
    2. Copies current grind results into the right folders with timestamps
    3. Sets latest.json for each step
    4. Moves legacy/outdated files to local_runner/cache/archive/
    5. Prints summary of what was moved

Does NOT delete originals — just copies. You can delete old locations after verifying.
"""

import os
import shutil
import json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
DATA_DIR = os.path.join(REPO_ROOT, "data")
GRINDS_DIR = os.path.join(REPO_ROOT, "local_runner", "grinds")
ARCHIVE_DIR = os.path.join(CACHE_DIR, "archive")


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def copy_and_set_latest(src, dest_dir, step, timestamp):
    """Copy file to dest_dir with standardized name, set latest.json."""
    if not os.path.exists(src):
        print(f"  SKIP (not found): {src}")
        return False

    ensure_dir(dest_dir)
    dest_file = os.path.join(dest_dir, f"{step}_{timestamp}.json")
    latest_file = os.path.join(dest_dir, "latest.json")

    shutil.copy2(src, dest_file)
    shutil.copy2(src, latest_file)
    print(f"  COPY: {os.path.relpath(src, REPO_ROOT)}")
    print(f"    -> {os.path.relpath(dest_file, REPO_ROOT)}")
    print(f"    -> {os.path.relpath(latest_file, REPO_ROOT)} (latest)")
    return True


def archive(src, reason):
    """Move file to archive directory."""
    if not os.path.exists(src):
        return False

    ensure_dir(ARCHIVE_DIR)
    dest = os.path.join(ARCHIVE_DIR, os.path.basename(src))
    shutil.copy2(src, dest)
    print(f"  ARCHIVE: {os.path.relpath(src, REPO_ROOT)} ({reason})")
    return True


def main():
    print("=" * 70)
    print("GRIND FILE MIGRATION")
    print("=" * 70)
    print()

    # ── DTSS Signal Grinds ──
    print("── DTSS Signal Grinds ──")
    signal_dir = os.path.join(GRINDS_DIR, "dtss", "signal")

    # Current best: pyramid_dtss_sig576_pk4_20260226_104240.json
    copy_and_set_latest(
        os.path.join(CACHE_DIR, "pyramid_dtss_sig576_pk4_20260226_104240.json"),
        signal_dir, "signal", "20260226_104240")

    # Other pyramid runs (keep as history)
    for f in ["pyramid_dtss_sig747_pk5_20260226_102605.json",
              "pyramid_dtss_sig747_pk5_20260226_103042.json"]:
        src = os.path.join(CACHE_DIR, f)
        if os.path.exists(src):
            # Extract timestamp from filename
            ts = f.split("_")[-2] + "_" + f.split("_")[-1].replace(".json", "")
            dest = os.path.join(signal_dir, f"signal_{ts}.json")
            ensure_dir(signal_dir)
            shutil.copy2(src, dest)
            print(f"  COPY (history): {f}")
            print(f"    -> {os.path.relpath(dest, REPO_ROOT)}")

    print()

    # ── DTSS Exit Grind ──
    print("── DTSS Exit Grind ──")
    exit_dir = os.path.join(GRINDS_DIR, "dtss", "exit")

    copy_and_set_latest(
        os.path.join(DATA_DIR, "exit_grind", "exit_grind_dtss.json"),
        exit_dir, "exit", "20260225_000000")

    print()

    # ── DTSS Outcome Grind ──
    print("── DTSS Outcome Grind (outdated, archiving) ──")
    # These are from the old outcome_grinder.py, pre-rewrite. Archive them.
    archive(os.path.join(DATA_DIR, "outcome_grind", "outcome_signals_dtss.json"),
            "pre-rewrite outcome grinder output")
    archive(os.path.join(DATA_DIR, "outcome_grind", "phase0_signal_bars_dtss.json"),
            "old phase0 — pyramid now has example signals")

    print()

    # ── Legacy files → archive ──
    print("── Legacy Files → Archive ──")
    legacy_files = {
        os.path.join(CACHE_DIR, "grinder_results_dtss.json"):
            "old Phase 1 spiderweb output, superseded by pyramid",
        os.path.join(CACHE_DIR, "historical_results_dtss.json"):
            "old Phase 2 historical scorer, superseded by pyramid",
        os.path.join(CACHE_DIR, "pyramid_results_dtss.json"):
            "pre-data-fix pyramid run, superseded by sig576",
        os.path.join(CACHE_DIR, "dtss_expressions.json"):
            "old DTSS-specific expressions, stripped in Step 4.5",
        os.path.join(DATA_DIR, "dtss_expressions.json"):
            "duplicate of above",
        os.path.join(DATA_DIR, "pyramid_results_dtss.json"):
            "duplicate of cache version, outdated",
    }

    for src, reason in legacy_files.items():
        archive(src, reason)

    print()

    # ── Support files (stay in cache) ──
    print("── Support Files (staying in local_runner/cache/) ──")
    support_files = [
        "brute_expressions.json — expression library (4,017 exprs)",
        "classification.json — ETF classifier results",
        "expr_series/_manifest.json — expression cache manifest",
    ]
    for desc in support_files:
        print(f"  KEEP: {desc}")

    print()

    # ── Summary ──
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    print("New structure:")
    print("  local_runner/grinds/dtss/signal/   — pyramid grinder outputs")
    print("  local_runner/grinds/dtss/exit/     — exit grinder outputs")
    print("  local_runner/grinds/dtss/outcome/  — outcome grinder outputs (empty, ready)")
    print("  local_runner/grinds/dtss/market/   — market grinder outputs (empty, ready)")
    print()
    print("Archived to local_runner/cache/archive/:")
    print("  Legacy grinder outputs, outdated pyramid runs, old expressions")
    print()
    print("Still in local_runner/cache/:")
    print("  brute_expressions.json, classification.json, expr_series/, OHLCV caches")
    print()
    print("NEXT STEPS:")
    print("  1. Verify the grinds/ structure looks correct")
    print("  2. Optionally delete old files from their original locations")
    print("  3. All grinders will now read/write via GrindStorage")

    # Create empty outcome and market dirs so structure is visible
    ensure_dir(os.path.join(GRINDS_DIR, "dtss", "outcome"))
    ensure_dir(os.path.join(GRINDS_DIR, "dtss", "market"))


if __name__ == "__main__":
    main()
