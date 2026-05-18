"""
Regime meter daily refresh orchestrator (worktree-local).

Runs the 7-step regime-meter pipeline in sequence so the dashboard's
JSON payload is fresh by ~8 AM Eastern each morning:

  1. naaim_ingest
  2. vix_ingest
  3. breadth_ingest
  4. regime_vector_history
  5. normalization_build
  6. forward_paths_build
  7. regime_meter_today

Phase 1 polls main-repo market_ohlcv.pkl for cache freshness so this
orchestrator can be scheduled to start near nightly.py's start time
without knowing exactly when nightly.py finishes. Polls every
POLL_INTERVAL_S for up to POLL_MAX_RETRIES, then aborts.

Phase 2 runs each step in a subprocess, captures stdout+stderr into
the log, aborts on first non-zero exit. Final exit code propagates so
Task Scheduler shows failures.

Logs append to regime_meter/cache/refresh.log.
"""
import os
import pickle
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
LOG_PATH      = os.path.join(CACHE_DIR, "refresh.log")

MAIN_REPO_CACHE  = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
MARKET_OHLCV_PKL = os.path.join(MAIN_REPO_CACHE, "market_ohlcv.pkl")

STEPS = [
    "naaim_ingest.py",
    "vix_ingest.py",
    "breadth_ingest.py",
    "regime_vector_history.py",
    "normalization_build.py",
    "forward_paths_build.py",
    "regime_meter_today.py",
]

# Freshness gate: SPY's last bar in market_ohlcv.pkl must be at most
# FRESHNESS_MAX_LAG_DAYS calendar days behind today. Covers weekends
# (Mon morning -> last bar Fri = 3d) and a single-day holiday gap.
FRESHNESS_MAX_LAG_DAYS = 5
POLL_INTERVAL_S        = 60
POLL_MAX_RETRIES       = 60   # up to 60 minutes total wait


def _log(message):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
    print(line)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _spy_last_bar():
    with open(MARKET_OHLCV_PKL, "rb") as f:
        cache = pickle.load(f)
    last  = pd.to_datetime(cache["SPY"]["date"]).max()
    today = pd.Timestamp(datetime.now().date())
    return last, int((today - last).days)


def wait_for_fresh_cache():
    for attempt in range(1, POLL_MAX_RETRIES + 1):
        last, age = _spy_last_bar()
        if age <= FRESHNESS_MAX_LAG_DAYS:
            _log(f"  cache fresh: SPY last bar {last.date()} ({age}d old)")
            return
        _log(f"  attempt {attempt}/{POLL_MAX_RETRIES}: "
             f"cache stale (SPY last bar {last.date()}, {age}d old). "
             f"sleeping {POLL_INTERVAL_S}s.")
        time.sleep(POLL_INTERVAL_S)
    last, age = _spy_last_bar()
    raise RuntimeError(
        f"cache still stale after {POLL_MAX_RETRIES} polls "
        f"(SPY last bar {last.date()}, {age}d old)"
    )


def run_step(script_name):
    path = os.path.join(SCRIPT_DIR, script_name)
    _log(f"  step start: {script_name}")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, path],
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - t0

    if proc.stdout:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for line in proc.stdout.rstrip().splitlines():
                f.write(f"    [{script_name}] {line}\n")
    if proc.stderr:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for line in proc.stderr.rstrip().splitlines():
                f.write(f"    [{script_name}] STDERR: {line}\n")

    if proc.returncode != 0:
        _log(f"  step FAILED: {script_name} (rc={proc.returncode}, {elapsed:.1f}s)")
        raise RuntimeError(f"{script_name} exited with code {proc.returncode}")
    _log(f"  step done:  {script_name} ({elapsed:.1f}s)")


def main():
    _log("=" * 70)
    _log("REGIME METER REFRESH start")
    try:
        _log("Phase 1: cache freshness gate")
        wait_for_fresh_cache()

        _log(f"Phase 2: running {len(STEPS)} steps")
        t0 = time.time()
        for step in STEPS:
            run_step(step)
        _log(f"REGIME METER REFRESH done ({time.time() - t0:.1f}s total)")
        return 0
    except Exception as e:
        _log(f"REGIME METER REFRESH FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
