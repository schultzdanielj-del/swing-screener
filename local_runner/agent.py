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


def get_et_now():
    """Get current time in US/Eastern, handling EST vs EDT automatically."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        # Fallback: approximate — EST in winter, EDT in summer
        # DST starts 2nd Sunday March, ends 1st Sunday November
        utc_now = datetime.now(timezone.utc)
        month = utc_now.month
        # EDT (UTC-4): March–November roughly; EST (UTC-5): November–March
        offset = -4 if 3 <= month <= 10 else -5
        return datetime.now(timezone(timedelta(hours=offset)))


def nightly_rebuild_needed():
    """Check if it's after 4:05pm ET and matrix hasn't been rebuilt today."""
    from matrix_builder import _universe_matrix_fresh
    now_et = get_et_now()

    # Only rebuild after market close (4:05pm ET) on weekdays
    if now_et.weekday() >= 5:  # Weekend
        return False
    if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 5):
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
        from matrix_builder import get_universe_matrix, get_example_matrix, get_bespoke_candidate_matrix
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

        # Align matrices to shared expressions (example matrix may have bespoke
        # setup-specific columns that the universe matrix doesn't have)
        ex_names = ex.get("expr_names") or uni["expr_names"]  # fallback
        uni_names = uni["expr_names"]
        ex_mat = ex["example_matrix"]
        uni_mat = uni["universe_matrix"]

        if ex_mat.shape[1] != uni_mat.shape[1]:
            # Build index of shared expression names
            uni_name_idx = {n: i for i, n in enumerate(uni_names)}
            # Load full DTSS expression list to get example column names
            import json, os
            ex_expr_path = os.path.join(os.path.dirname(__file__), "cache",
                                        f"{setup_type}_expressions.json")
            if os.path.exists(ex_expr_path):
                with open(ex_expr_path) as f:
                    all_ex_names = [e["name"] for e in json.load(f)["expressions"]]
            else:
                all_ex_names = uni_names  # fallback

            shared_uni_cols = []
            shared_ex_cols = []
            shared_names = []
            shared_cats = []
            for ex_col, name in enumerate(all_ex_names):
                if name in uni_name_idx:
                    shared_uni_cols.append(uni_name_idx[name])
                    shared_ex_cols.append(ex_col)
                    shared_names.append(name)
                    shared_cats.append(uni["expr_categories"][uni_name_idx[name]])

            ex_mat = ex_mat[:, shared_ex_cols]
            uni_mat = uni_mat[:, shared_uni_cols]
            print(f"    [agent] Aligned matrices: {len(shared_names)} shared expressions "
                  f"(example had {ex['example_matrix'].shape[1]}, universe has {len(uni_names)})")
        else:
            shared_names = uni_names
            shared_cats = uni["expr_categories"]

        search = SpiderwebSearch(
            example_values=ex_mat,
            universe_values=uni_mat,
            expr_names=shared_names,
            expr_categories=shared_cats,
            universe_tickers=uni["universe_tickers"],
        )

        results = search.run(
            depth=level["depth"],
            beam_width=level["beam_width"],
            progress_callback=search_progress,
        )

        post_progress(job_id, "search", 100,
                      f"Done: {results.get('best_rate', 0):.2%}")

        # ── Phase 2: Bespoke re-filter on candidates ─────────────────────────
        # For setups with bespoke expressions (e.g. DTSS LSP/AVWAP), run LSP
        # detection on the small candidate pool and apply bespoke conditions.
        # This is fast because we only process tickers that passed Phase 1.
        bespoke_results = None
        has_bespoke = os.path.exists(
            os.path.join(CACHE_DIR, f"{setup_type}_expressions.json")
        )
        phase1_tickers = results.get("passing_tickers", [])

        if has_bespoke and phase1_tickers:
            progress("bespoke", 0,
                     f"Phase 2: LSP filter on {len(phase1_tickers)} candidates...")
            print(f"\n  Phase 2: bespoke re-filter on {len(phase1_tickers)} candidates")

            def bespoke_progress(phase, pct, detail):
                print(f"    [{phase}:{pct:3d}%] {detail}")
                post_progress(job_id, "bespoke", pct, detail)

            bespoke = get_bespoke_candidate_matrix(
                setup_type, phase1_tickers,
                scan_date=None,  # uses most recent bar
                progress_fn=bespoke_progress,
            )

            # Get bespoke example values (columns from example matrix that are bespoke)
            import json as _json
            ex_expr_path = os.path.join(CACHE_DIR, f"{setup_type}_expressions.json")
            with open(ex_expr_path) as f:
                all_ex_exprs = _json.load(f)["expressions"]
            all_ex_names = [e["name"] for e in all_ex_exprs]

            bespoke_ex_cols = [
                all_ex_names.index(n)
                for n in bespoke["bespoke_names"]
                if n in all_ex_names
            ]
            ex_bespoke_vals = ex["example_matrix"][:, bespoke_ex_cols]

            # Run spiderweb on bespoke expressions, candidate pool as "universe"
            def bespoke_search_progress(search_level, best_rate, nodes, elapsed):
                post_progress(job_id, "bespoke", min(95, search_level * 15),
                              f"Bespoke level {search_level}: {best_rate:.2%} | {nodes} nodes")

            bespoke_search = SpiderwebSearch(
                example_values=ex_bespoke_vals,
                universe_values=bespoke["candidate_matrix"],
                expr_names=bespoke["bespoke_names"],
                expr_categories=bespoke["bespoke_categories"],
                universe_tickers=phase1_tickers,
            )
            bespoke_results = bespoke_search.run(
                depth=min(5, level["depth"]),
                beam_width=min(25, level["beam_width"]),
                progress_callback=bespoke_search_progress,
            )

            # Final passing tickers = intersection of Phase 1 and Phase 2
            final_tickers = bespoke_results.get("passing_tickers", phase1_tickers)
            final_rate = len(final_tickers) / len(uni["universe_tickers"])

            progress("bespoke", 100,
                     f"Phase 2 done: {len(final_tickers)} tickers ({final_rate:.2%})")
            print(f"\n  Phase 2 result: {len(final_tickers)} tickers "
                  f"({final_rate:.2%}) after bespoke filter")

            # Merge bespoke conditions into results
            results["bespoke_conditions"] = bespoke_results.get("best_thresholds", [])
            results["bespoke_path"] = bespoke_results.get("best_path", [])
            results["passing_tickers"] = final_tickers
            results["best_passing"] = len(final_tickers)
            results["best_rate"] = final_rate
            results["phase1_passing"] = len(phase1_tickers)
            results["phase1_rate"] = len(phase1_tickers) / len(uni["universe_tickers"])

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
