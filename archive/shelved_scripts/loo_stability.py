"""
Leave-One-Out Pick Stability
============================

For each setup, rerun the grinder N times (one per example), each time holding
out one example from the resolved set. Record which (expression, direction,
threshold) tuple appears as pick 1 in each run, and what fraction of LOO runs
pick the same expression as the full-set run.

Picks that recur in >=70% of LOO runs are structurally grounded.
Picks that appear in <30% are fragile — selection is example-specific.

Writes:
  data/diagnostics/loo_stability_{setup}.json

This is pure stability testing — no new picks are "learned," just stability
is measured.

Usage:
  python scripts/loo_stability.py --setup htf [--setup bf] [--workers 4]
"""

import argparse
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_ROOT = os.environ.get("SCANPERFECT_READ_ROOT", REPO_ROOT)
CACHE_DIR = os.environ.get("SCANPERFECT_CACHE_DIR", os.path.join(REPO_ROOT, "local_runner", "cache"))
DB_PATH = os.path.join(READ_ROOT, "data", "scanperfect.db")
GRIND_DIR = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
OUT_DIR = os.path.join(REPO_ROOT, "data", "diagnostics")
os.makedirs(OUT_DIR, exist_ok=True)


def load_examples(setup):
    """Load resolved examples from the current earnings-cap JSON."""
    path = os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")
    with open(path) as f:
        d = json.load(f)
    return d["examples"]


def run_loo(setup, workers):
    """LOO approach: patch the SQLite query for the grinder via a sentinel env var.

    Simpler to implement via a small helper: pre-write a "conditions file" that
    the grinder accepts (--conditions-file) plus a holdout-ticker env var the
    grinder honors during example resolution.

    Even simpler: run the grinder but delete the one example's row from a
    temporary SQLite db passed as READ_ROOT. We'll copy the db, delete the row,
    and run against the temp db for each LOO iteration.
    """
    all_examples = load_examples(setup)
    n = len(all_examples)
    print(f"[{setup}] running LOO on {n} examples")

    results = []
    t0 = time.time()
    for i, ex in enumerate(all_examples):
        tmp_root = tempfile.mkdtemp(prefix=f"loo_{setup}_{i}_")
        tmp_data_dir = os.path.join(tmp_root, "data")
        os.makedirs(tmp_data_dir, exist_ok=True)
        tmp_db = os.path.join(tmp_data_dir, "scanperfect.db")
        # Copy main db and delete this example
        import shutil
        shutil.copy2(DB_PATH, tmp_db)
        # We also need the local_runner/cache/ structure for pyramid reads etc.
        # Since the grinder looks up READ_ROOT, we need to symlink or have the
        # full structure available. Use the original READ_ROOT for non-db paths
        # by letting the grinder fall back — but the grinder currently uses one
        # READ_ROOT for everything. Workaround: copy the db, set READ_ROOT to a
        # hybrid dir containing the modified db + symlinks to the originals.
        # Simpler: just mutate the original db in a guarded way is risky. We'll
        # use an env-var exclusion instead (grinder-side support).
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "DELETE FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
            (setup, ex["ticker"], ex["entry_date"]),
        )
        conn.commit()
        conn.close()

        # For non-db paths (local_runner/cache, pyramid jsons), use the main path.
        # We pass both the worktree env (SCANPERFECT_READ_ROOT = main) for cache
        # and override the db lookup by symlinking the main data dir contents
        # into tmp_root/data while replacing the db.
        main_data = os.path.join(READ_ROOT, "data")
        for fname in os.listdir(main_data):
            src = os.path.join(main_data, fname)
            dst = os.path.join(tmp_data_dir, fname)
            if fname == "scanperfect.db":
                continue  # already our modified version
            if os.path.isdir(src):
                continue  # skip subdirs (not needed for this grinder)
            if not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

        # Need local_runner/cache available at tmp_root. Point to main's path by
        # setting env vars for this child process.
        env = os.environ.copy()
        env["SCANPERFECT_READ_ROOT"] = tmp_root  # db lives here
        env["SCANPERFECT_CACHE_DIR"] = CACHE_DIR  # expression cache sandbox
        # Also need local_runner/cache to exist under tmp_root for pyramid JSONs:
        tmp_local = os.path.join(tmp_root, "local_runner")
        tmp_local_cache = os.path.join(tmp_local, "cache")
        os.makedirs(tmp_local_cache, exist_ok=True)
        main_local_cache = os.path.join(READ_ROOT, "local_runner", "cache")
        # copy only pyramid json pointers the grinder needs
        import glob
        for p in glob.glob(os.path.join(main_local_cache, f"pyramid_*{setup}*.json")):
            dst = os.path.join(tmp_local_cache, os.path.basename(p))
            if not os.path.exists(dst):
                shutil.copy2(p, dst)

        cmd = [
            sys.executable,
            os.path.join(REPO_ROOT, "scripts", "signal_exit_grinder.py"),
            "--setup", setup,
            "--earnings-cap",
            "--no-upload",
        ]
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, cwd=REPO_ROOT
        )
        # parse output to get picks
        out = proc.stdout + proc.stderr
        picks = []
        for line in out.splitlines():
            # PICK N (coverage): expr dir threshold
            if line.strip().startswith("PICK "):
                picks.append(line.strip())
        results.append({
            "held_out": {"ticker": ex["ticker"], "signal_date": ex["signal_date"]},
            "picks": picks,
            "ok": proc.returncode == 0,
        })
        # cleanup
        try:
            shutil.rmtree(tmp_root)
        except Exception:
            pass

        elapsed = time.time() - t0
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        eta = (n - i - 1) / rate if rate > 0 else 0
        print(
            f"  [{i+1}/{n}] {ex['ticker']}_{ex['signal_date']} "
            f"elapsed={elapsed:.0f}s eta={eta:.0f}s picks={len(picks)}"
        )

    out_path = os.path.join(OUT_DIR, f"loo_stability_{setup}.json")
    with open(out_path, "w") as f:
        json.dump(
            {
                "setup": setup,
                "n_runs": n,
                "timestamp": datetime.now().isoformat(),
                "results": results,
            },
            f,
            indent=2,
        )
    print(f"\n  Wrote {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default="htf")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    run_loo(args.setup, args.workers)


if __name__ == "__main__":
    main()
