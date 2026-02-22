"""
Desktop Agent v2 — Polls Railway for grind jobs + nightly matrix auto-rebuild.

Usage:
    python local_runner/agent.py

Leave running 24/7. It will:
  - Poll Railway every 5s for grind jobs
  - Auto-rebuild OHLCV cache daily after 4:30pm ET
  - Auto-rebuild universe matrix daily (once, ~30 min)
  - Pick up grind jobs and run spiderweb search (fast if matrix cached)

After first run, daily grinds are fast. The matrix rebuilds in background.
"""

import os
import sys
import json
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import requests
from datetime import datetime, timezone, timedelta

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

API_BASE = "https://web-production-e3025.up.railway.app"
POLL_INTERVAL = 5

GRIND_LEVELS = {
    1: {"name": "Quick scan",    "beam_width": 10,  "depth": 5},
    2: {"name": "Light grind",   "beam_width": 25,  "depth": 8},
    3: {"name": "Medium grind",  "beam_width": 50,  "depth": 10},
    4: {"name": "Heavy grind",   "beam_width": 100, "depth": 12},
    5: {"name": "Overnight",     "beam_width": 250, "depth": 15},
}


def now_utc():
    return datetime.now(timezone.utc)


def post_status(job_id, status, message="", data=None):
    try:
        payload = {"job_id": job_id, "status": status, "message": message,
                   "timestamp": now_utc().isoformat()}
        if data:
            payload["data"] = data
        requests.post(f"{API_BASE}/api/grinder/status", json=payload, timeout=15)
    except:
        pass


def post_progress(job_id, phase, pct, detail=""):
    try:
        requests.post(f"{API_BASE}/api/grinder/progress", json={
            "job_id": job_id, "phase": phase, "progress_pct": pct,
            "detail": detail, "timestamp": now_utc().isoformat(),
        }, timeout=10)
    except:
        pass


def check_for_jobs():
    try:
        r = requests.get(f"{API_BASE}/api/grinder/jobs/pending", timeout=10)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except:
        pass
    return []


def heartbeat():
    try:
        requests.post(f"{API_BASE}/api/grinder/agent/heartbeat", json={
            "agent_id": "desktop", "timestamp": now_utc().isoformat(),
        }, timeout=5)
    except:
        pass


def nightly_rebuild_needed():
    """Check if it's after 4:30pm ET and matrix hasn't been rebuilt today."""
    from matrix_builder import _universe_matrix_fresh
    et = timezone(timedelta(hours=-5))
    now_et = datetime.now(et)

    # Only rebuild after market close (4:30pm ET) on weekdays
    if now_et.weekday() >= 5:  # Weekend
        return False
    if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 30):
        return False

    return not _universe_matrix_fresh()


def run_nightly_rebuild():
    """Rebuild OHLCV cache and universe matrix."""
    print(f"\n  🌙 Nightly rebuild starting...")

    # Rebuild OHLCV cache
    print(f"  Refreshing OHLCV cache...")
    from cache_builder import build_cache
    build_cache(force=True)

    # Rebuild universe matrix
    print(f"  Rebuilding universe matrix (this takes ~30 min)...")
    from matrix_builder import get_universe_matrix

    def progress(phase, pct, detail):
        print(f"    [{pct:3d}%] {detail}")

    get_universe_matrix(progress_fn=progress, force=True)
    print(f"  🌙 Nightly rebuild complete!\n")


def handle_job(job):
    job_id = job["job_id"]
    setup_type = job.get("setup_type", "dtss")
    grind_level = job.get("grind_level", 3)
    level = GRIND_LEVELS.get(grind_level, GRIND_LEVELS[3])

    print(f"\n{'='*60}")
    print(f"  JOB: {job_id}")
    print(f"  Setup: {setup_type} | Level {grind_level}: {level['name']}")
    print(f"{'='*60}")

    post_status(job_id, "running", "Agent picked up job")

    try:
        from matrix_builder import get_universe_matrix, get_example_matrix
        from spiderweb import SpiderwebSearch

        # Progress helper
        def progress(phase, pct, detail):
            print(f"    [{phase}:{pct:3d}%] {detail}")
            post_progress(job_id, phase, pct, detail)

        # Get universe matrix (cached daily, fast if fresh)
        progress("matrix", 0, "Loading universe matrix...")
        uni = get_universe_matrix(progress_fn=progress)

        # Get example matrix (fast, per-setup)
        progress("examples", 0, f"Loading {setup_type} examples...")
        ex = get_example_matrix(setup_type, progress_fn=progress)

        # Run spiderweb search
        progress("search", 0, f"Starting search (depth={level['depth']}, beam={level['beam_width']})")

        def search_progress(search_level, best_rate, nodes, elapsed):
            pct = min(95, int(search_level / level["depth"] * 95))
            post_progress(job_id, "search", pct,
                          f"Level {search_level}: {best_rate:.2%} | {nodes:,} nodes | {elapsed:.0f}s")

        search = SpiderwebSearch(
            example_values=ex["example_matrix"],
            universe_values=uni["universe_matrix"],
            expr_names=uni["expr_names"],
            expr_categories=uni["expr_categories"],
        )

        results = search.run(
            depth=level["depth"],
            beam_width=level["beam_width"],
            progress_callback=search_progress,
        )

        post_progress(job_id, "search", 100,
                      f"Done: {results.get('best_rate', 0):.2%}")

        # Save and upload
        out = {
            "setup_type": setup_type,
            "grind_level": grind_level,
            "grind_name": level["name"],
            "timestamp": now_utc().isoformat(),
            **results,
        }

        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, f"grinder_results_{setup_type}.json"), "w") as f:
            json.dump(out, f, indent=2, default=str)

        post_status(job_id, "complete",
                    f"Done: {results.get('best_rate', 0):.2%} pass ({results.get('best_passing', 0)} tickers)",
                    data=out)

        print(f"\n  ✓ Complete: {results.get('best_rate', 0):.2%} pass rate")

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"\n  ✗ Failed: {error_msg}")
        traceback.print_exc()
        post_status(job_id, "error", error_msg)


def main():
    print("\n" + "=" * 60)
    print("  GRINDER DESKTOP AGENT v2")
    print("=" * 60)
    print(f"  API:  {API_BASE}")
    print(f"  Poll: every {POLL_INTERVAL}s")
    print(f"  Time: {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Check if universe matrix exists
    from matrix_builder import _universe_matrix_fresh
    if _universe_matrix_fresh():
        print(f"  Matrix: ✓ Fresh (today)")
    else:
        print(f"  Matrix: ✗ Needs rebuild (will build on first grind or at 4:30pm ET)")

    print(f"\n  Waiting for jobs...\n")

    # Register
    try:
        requests.post(f"{API_BASE}/api/grinder/agent/register", json={
            "agent_id": "desktop", "timestamp": now_utc().isoformat(), "status": "online",
        }, timeout=10)
    except:
        pass

    last_heartbeat = time.time()
    last_nightly_check = 0

    while True:
        try:
            # Poll for jobs
            jobs = check_for_jobs()
            for job in jobs:
                handle_job(job)

            # Heartbeat every 30s
            if time.time() - last_heartbeat > 30:
                heartbeat()
                last_heartbeat = time.time()

            # Check nightly rebuild every 5 min
            if time.time() - last_nightly_check > 300:
                if nightly_rebuild_needed():
                    run_nightly_rebuild()
                last_nightly_check = time.time()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n  Agent stopped.")
            try:
                requests.post(f"{API_BASE}/api/grinder/agent/register", json={
                    "agent_id": "desktop", "timestamp": now_utc().isoformat(), "status": "offline",
                }, timeout=5)
            except:
                pass
            break
        except Exception as e:
            import traceback
            print(f"  Agent error: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
