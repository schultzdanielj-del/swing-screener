"""
Desktop Agent — Polls Railway for grind jobs, runs them, posts results.

Usage:
    python local_runner/agent.py

Runs forever. Checks Railway every 5 seconds for pending jobs.
When a job is found:
  1. Builds/refreshes OHLCV cache if needed
  2. Generates expressions if needed
  3. Computes value matrix if needed (fixed cost, cached)
  4. Runs spiderweb search at requested grind level
  5. Posts results + progress back to Railway

The frontend triggers jobs by POST /api/grinder/jobs
This agent picks them up and runs them.
"""

import os
import sys
import json
import time
import traceback
import requests
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
API_BASE = "https://web-production-e3025.up.railway.app"

POLL_INTERVAL = 5  # seconds


def post_status(job_id, status, message="", data=None):
    """Update job status on Railway."""
    try:
        payload = {
            "job_id": job_id,
            "status": status,
            "message": message,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if data:
            payload["data"] = data
        r = requests.post(f"{API_BASE}/api/grinder/status", json=payload, timeout=15)
        return r.status_code == 200
    except:
        return False


def post_progress(job_id, phase, progress_pct, detail=""):
    """Update job progress on Railway (for frontend progress bar)."""
    try:
        r = requests.post(f"{API_BASE}/api/grinder/progress", json={
            "job_id": job_id,
            "phase": phase,
            "progress_pct": progress_pct,
            "detail": detail,
            "timestamp": datetime.utcnow().isoformat(),
        }, timeout=10)
        return r.status_code == 200
    except:
        return False


def check_for_jobs():
    """Poll Railway for pending grind jobs."""
    try:
        r = requests.get(f"{API_BASE}/api/grinder/jobs/pending", timeout=10)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            return jobs
        return []
    except:
        return []


def run_cache_build(job_id):
    """Build/refresh OHLCV cache."""
    post_progress(job_id, "cache", 0, "Building OHLCV cache...")
    from local_runner.cache_builder import build_cache, cache_is_fresh

    if cache_is_fresh():
        from local_runner.cache_builder import load_cache
        data = load_cache()
        post_progress(job_id, "cache", 100, f"Cache fresh: {len(data)} tickers")
        return True

    data = build_cache(force=True)
    post_progress(job_id, "cache", 100, f"Cache built: {len(data)} tickers")
    return True


def run_expression_gen(job_id):
    """Generate brute force expressions."""
    post_progress(job_id, "expressions", 0, "Generating expressions...")
    from local_runner.brute_expressions import generate_all

    os.makedirs(CACHE_DIR, exist_ok=True)
    exprs = generate_all()

    out_path = os.path.join(CACHE_DIR, "brute_expressions.json")
    cats = {}
    for e in exprs:
        cat = e["category"]
        cats[cat] = cats.get(cat, 0) + 1

    with open(out_path, "w") as f:
        json.dump({"total": len(exprs), "by_category": cats, "expressions": exprs}, f)

    post_progress(job_id, "expressions", 100, f"Generated {len(exprs)} expressions")
    return True


def run_grind(job_id, setup_type, grind_level):
    """Run the full grind job."""
    import numpy as np
    import pandas as pd
    import pickle
    from scripts.expression_engine import ExpressionEngine
    from local_runner.spiderweb import SpiderwebSearch

    GRIND_LEVELS = {
        1: {"name": "Quick scan",    "beam_width": 10,  "depth": 5},
        2: {"name": "Light grind",   "beam_width": 25,  "depth": 8},
        3: {"name": "Medium grind",  "beam_width": 50,  "depth": 10},
        4: {"name": "Heavy grind",   "beam_width": 100, "depth": 12},
        5: {"name": "Overnight",     "beam_width": 250, "depth": 15},
    }

    level = GRIND_LEVELS.get(grind_level, GRIND_LEVELS[3])

    # Load expressions
    expr_path = os.path.join(CACHE_DIR, "brute_expressions.json")
    with open(expr_path) as f:
        expressions = json.load(f)["expressions"]

    # Check for precomputed matrix
    matrix_file = os.path.join(CACHE_DIR, f"value_matrix_{setup_type}.pkl")
    if os.path.exists(matrix_file):
        post_progress(job_id, "matrix", 50, "Loading precomputed matrix...")
        with open(matrix_file, "rb") as f:
            matrix = pickle.load(f)
        if matrix.get("n_exprs") == len(expressions):
            post_progress(job_id, "matrix", 100,
                          f"Matrix loaded: {matrix['n_examples']} examples, "
                          f"{matrix['n_universe']} universe")
        else:
            matrix = None
            post_progress(job_id, "matrix", 0, "Matrix stale, recomputing...")
    else:
        matrix = None

    if matrix is None:
        # Compute matrix from scratch
        post_progress(job_id, "matrix", 5, "Loading examples from API...")

        # Load examples
        r = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=15)
        raw_examples = r.json().get("examples", []) if r.status_code == 200 else []

        examples = []
        for ex in raw_examples:
            try:
                r2 = requests.get(
                    f"{API_BASE}/api/ohlcv/local/{setup_type}/{ex.get('id')}",
                    timeout=15
                )
                if r2.status_code != 200:
                    continue
                candles = r2.json().get("candles", [])
                if not candles:
                    continue
                df = pd.DataFrame(candles)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)

                entry_date = ex.get("entryDate") or ex.get("chartDate")
                target_idx = len(df) - 1
                if entry_date:
                    matches = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
                    if len(matches) > 0:
                        target_idx = matches.index[0]

                examples.append({
                    "ticker": ex["ticker"], "df": df,
                    "target_idx": target_idx, "id": ex["id"],
                    "entry_date": entry_date,
                })
            except:
                continue

        post_progress(job_id, "matrix", 10, f"Loaded {len(examples)} examples")

        # Example matrix
        example_matrix = np.full((len(examples), len(expressions)), np.nan)
        example_tickers = []
        for i, ex in enumerate(examples):
            engine = ExpressionEngine(ex["df"])
            engine.set_target(ex["target_idx"])
            for j, expr in enumerate(expressions):
                val = engine.compute(expr)
                if val is not None and not np.isnan(val):
                    example_matrix[i, j] = val
            example_tickers.append(f"{ex['ticker']}_{ex['id']}")
            pct = 10 + int(20 * (i + 1) / len(examples))
            post_progress(job_id, "matrix", pct,
                          f"Examples: {i+1}/{len(examples)} ({ex['ticker']})")

        # Universe matrix
        post_progress(job_id, "matrix", 30, "Loading OHLCV cache...")
        cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
        with open(cache_file, "rb") as f:
            universe_cache = pickle.load(f)

        uni_tickers = list(universe_cache.keys())
        universe_matrix = np.full((len(uni_tickers), len(expressions)), np.nan)

        for i, ticker in enumerate(uni_tickers):
            df = universe_cache[ticker]
            if df is None or len(df) < 50:
                continue
            engine = ExpressionEngine(df)
            engine.set_target(len(df) - 1)
            for j, expr in enumerate(expressions):
                val = engine.compute(expr)
                if val is not None and not np.isnan(val):
                    universe_matrix[i, j] = val

            if (i + 1) % 200 == 0 or (i + 1) == len(uni_tickers):
                pct = 30 + int(65 * (i + 1) / len(uni_tickers))
                post_progress(job_id, "matrix", pct,
                              f"Universe: {i+1}/{len(uni_tickers)} tickers")

        # Save matrix
        matrix = {
            "example_matrix": example_matrix,
            "universe_matrix": universe_matrix,
            "example_tickers": example_tickers,
            "universe_tickers": uni_tickers,
            "expr_names": [e["name"] for e in expressions],
            "expr_categories": [e.get("category", "unknown") for e in expressions],
            "n_exprs": len(expressions),
            "n_examples": len(examples),
            "n_universe": len(uni_tickers),
            "computed_at": datetime.utcnow().isoformat(),
        }
        with open(matrix_file, "wb") as f:
            pickle.dump(matrix, f, protocol=pickle.HIGHEST_PROTOCOL)
        post_progress(job_id, "matrix", 100, "Matrix saved")

    # Phase 2: Spiderweb search
    post_progress(job_id, "search", 0,
                  f"Starting spiderweb search (Level {grind_level}: {level['name']})")

    def search_progress(search_level, best_rate, nodes, elapsed):
        pct = min(95, int(search_level / level["depth"] * 95))
        post_progress(job_id, "search", pct,
                      f"Level {search_level}: {best_rate:.2%} pass | "
                      f"{nodes:,} nodes | {elapsed:.0f}s")

    search = SpiderwebSearch(
        example_values=matrix["example_matrix"],
        universe_values=matrix["universe_matrix"],
        expr_names=matrix["expr_names"],
        expr_categories=matrix["expr_categories"],
    )

    results = search.run(
        depth=level["depth"],
        beam_width=level["beam_width"],
        progress_callback=search_progress,
    )

    post_progress(job_id, "search", 100,
                  f"Done: {results.get('best_rate', 0):.2%} pass rate")

    return results


def handle_job(job):
    """Execute a single grind job."""
    job_id = job["job_id"]
    setup_type = job.get("setup_type", "dtss")
    grind_level = job.get("grind_level", 3)
    action = job.get("action", "grind")

    print(f"\n{'='*60}")
    print(f"  JOB: {job_id}")
    print(f"  Setup: {setup_type} | Level: {grind_level} | Action: {action}")
    print(f"  Time: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    post_status(job_id, "running", f"Agent picked up job: {action}")

    try:
        if action == "cache":
            run_cache_build(job_id)
            post_status(job_id, "complete", "Cache built successfully")

        elif action == "grind":
            # Ensure cache exists
            cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
            if not os.path.exists(cache_file):
                run_cache_build(job_id)

            # Ensure expressions exist
            expr_path = os.path.join(CACHE_DIR, "brute_expressions.json")
            if not os.path.exists(expr_path):
                run_expression_gen(job_id)

            # Run grind
            results = run_grind(job_id, setup_type, grind_level)

            # Save and upload
            out = {
                "setup_type": setup_type,
                "grind_level": grind_level,
                "timestamp": datetime.utcnow().isoformat(),
                **results,
            }

            out_path = os.path.join(CACHE_DIR, f"grinder_results_{setup_type}.json")
            with open(out_path, "w") as f:
                json.dump(out, f, indent=2, default=str)

            post_status(job_id, "complete",
                        f"Done: {results.get('best_rate', 0):.2%} pass rate",
                        data=out)

            print(f"\n  ✓ Job complete: {results.get('best_rate', 0):.2%} pass rate")

        else:
            post_status(job_id, "error", f"Unknown action: {action}")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"\n  ✗ Job failed: {error_msg}")
        traceback.print_exc()
        post_status(job_id, "error", error_msg)


def main():
    print("\n" + "=" * 60)
    print("  ╔══════════════════════════════════════╗")
    print("  ║     GRINDER DESKTOP AGENT v1         ║")
    print("  ║   Polling for jobs from Railway...   ║")
    print("  ╚══════════════════════════════════════╝")
    print("=" * 60)
    print(f"\n  API:  {API_BASE}")
    print(f"  Poll: every {POLL_INTERVAL}s")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\n  Waiting for jobs...\n")

    # Register agent
    try:
        requests.post(f"{API_BASE}/api/grinder/agent/register", json={
            "agent_id": "desktop",
            "timestamp": datetime.utcnow().isoformat(),
            "status": "online",
        }, timeout=10)
    except:
        pass

    last_heartbeat = time.time()

    while True:
        try:
            # Poll for jobs
            jobs = check_for_jobs()
            if jobs:
                for job in jobs:
                    handle_job(job)

            # Heartbeat every 30s
            if time.time() - last_heartbeat > 30:
                try:
                    requests.post(f"{API_BASE}/api/grinder/agent/heartbeat", json={
                        "agent_id": "desktop",
                        "timestamp": datetime.utcnow().isoformat(),
                    }, timeout=5)
                except:
                    pass
                last_heartbeat = time.time()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n  Agent stopped by user.")
            try:
                requests.post(f"{API_BASE}/api/grinder/agent/register", json={
                    "agent_id": "desktop",
                    "timestamp": datetime.utcnow().isoformat(),
                    "status": "offline",
                }, timeout=5)
            except:
                pass
            break
        except Exception as e:
            print(f"  Agent error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
