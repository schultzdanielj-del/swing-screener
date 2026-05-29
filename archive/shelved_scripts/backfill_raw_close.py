"""SUPERSEDED 2026-04-22 — kept for reference; do not run.

The OHLCV cache builder was changed on 2026-04-22 to write raw `close`
(split-forward-adjusted, NOT dividend-adjusted) at write time. Under that
policy this repair script is redundant: every ticker's history is correct
at the moment it lands in the cache. There is no missed-split state for
this script to repair.

Original behavior (preserved below for historical reference): a one-shot
manual repair that detected tickers with missed-split state in the cooked
adjusted-close cache and rewrote their OHLC using the EODHD
`adjusted_close / close` ratio. The docstring claimed it added a
`raw_close` column and recomputed `dvol_20d`; the actual code did neither.

This script:
  1. Discovers tickers that ever split via EODHD bulk-splits-per-day endpoint.
  2. Refetches raw close from EODHD for those tickers via _batched_fetch.
  3. Adds raw_close column to every ticker's DataFrame:
       - split tickers: raw_close = refetched raw close (date-aligned)
       - non-split tickers: raw_close = close (adjusted == raw absent splits)
  4. Recomputes dvol_20d as 20-bar rolling mean of (raw_close * volume).
  5. Saves the daily pickle (with backup beforehand).

Resumable: writes intermediate state to local_runner/cache/_backfill_state/.
Idempotent: skips work already done.

Run:
    python local_runner/backfill_raw_close.py --discover     # step 1 only
    python local_runner/backfill_raw_close.py --refetch      # step 2 only
    python local_runner/backfill_raw_close.py --apply        # steps 3-5 only
    python local_runner/backfill_raw_close.py --all          # everything
"""

import argparse
import json
import os
import pickle
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache_builder import (  # noqa: E402
    EODHD_API_TOKEN,
    EODHD_BASE,
    HISTORY_START,
    CACHE_DAILY_FILE,
    CACHE_DIR,
    _batched_fetch,
    _eodhd_fetch_json,
)

STATE_DIR = os.path.join(CACHE_DIR, "_backfill_state")
SPLIT_TICKERS_FILE = os.path.join(STATE_DIR, "split_tickers.json")
RAW_CLOSE_DIR = os.path.join(STATE_DIR, "raw_close")
BACKUP_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_daily_pre_dvol_fix.pkl")


def _ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(RAW_CLOSE_DIR, exist_ok=True)


def _fetch_splits_one(ticker):
    """Per-ticker splits endpoint. Returns (ticker, list_of_splits) or
    (ticker, None) on transport failure. Empty list = no splits (success).
    """
    url = (f"{EODHD_BASE}/splits/{ticker}.US"
           f"?api_token={EODHD_API_TOKEN}&fmt=json")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ScanPerfect/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        if not raw.startswith("["):
            # Empty response or "Ticker Not Found" -- treat as no splits
            return ticker, []
        data = json.loads(raw)
        # Wrap empty list in a sentinel so _batched_fetch counts it as success
        return ticker, data if data else []
    except Exception:
        return ticker, None


def discover_split_tickers():
    """Hit per-ticker splits endpoint for every ticker in the cache.

    Bulk endpoints (eod-bulk-last-day with type=splits or full bulk OHLCV)
    return 402 on Dan's current plan. Per-ticker /splits/{TICKER}.US works
    and is 1 quota call each. Total ~11,823 quota for full discovery.

    Uses _batched_fetch for rate-limit handling.
    Saves split_tickers.json: {ticker: [{date, split_str}, ...]} -- only
    tickers with at least one split are included.
    """
    _ensure_state()
    if os.path.exists(SPLIT_TICKERS_FILE):
        with open(SPLIT_TICKERS_FILE) as f:
            existing = json.load(f)
        print(f"  Already discovered: {len(existing)} split tickers "
              f"in {SPLIT_TICKERS_FILE}")
        print("  Delete that file to force re-discovery.")
        return existing

    print("  Loading universe ticker list from cache...")
    with open(CACHE_DAILY_FILE, "rb") as f:
        universe = pickle.load(f)
    tickers = sorted(universe.keys())
    print(f"  Universe size: {len(tickers)}")
    del universe

    print(f"\n  Querying /splits/{{TICKER}}.US for {len(tickers)} tickers...")
    results, failed = _batched_fetch(
        tickers,
        _fetch_splits_one,
        label="Splits",
        batch_size=80,
        min_sleep=0.2,
        max_sleep=10.0,
        max_retries=3,
        max_workers=40,
    )

    # results: {ticker: list_of_splits} for both empty and non-empty
    splits_by_ticker = {t: splits for t, splits in results.items() if splits}

    print(f"\n  Discovered {len(splits_by_ticker)} tickers with splits "
          f"(of {len(results)} successfully queried)")
    print(f"  Failed (transport): {len(failed)}")
    if failed:
        with open(os.path.join(STATE_DIR, "discovery_failed.json"), "w") as f:
            json.dump(failed, f, indent=2)

    with open(SPLIT_TICKERS_FILE, "w") as f:
        json.dump(splits_by_ticker, f, indent=2)
    print(f"  Saved -> {SPLIT_TICKERS_FILE}")

    return splits_by_ticker


def _fetch_raw_close_one(ticker):
    """Fetch full history raw OHLCV + adjusted_close for one ticker.

    Returns (ticker, dict {date: [ro, rh, rl, rc, ac, v]}) or (ticker, None).
    ro/rh/rl/rc = raw open/high/low/close, ac = adjusted close, v = volume.
    """
    end_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    data = _eodhd_fetch_json(ticker, HISTORY_START, end_date, "d")
    if not data:
        return ticker, None
    out = {}
    for bar in data:
        d = bar.get("date", "")[:10]
        try:
            ro = float(bar.get("open", 0) or 0)
            rh = float(bar.get("high", 0) or 0)
            rl = float(bar.get("low", 0) or 0)
            rc = float(bar.get("close", 0) or 0)
            ac = float(bar.get("adjusted_close", 0) or 0)
            v = float(bar.get("volume", 0) or 0)
            if rc > 0 and ac > 0:
                out[d] = [ro, rh, rl, rc, ac, v]
        except (TypeError, ValueError):
            continue
    return ticker, out if out else None


def refetch_raw_close(split_tickers):
    """Refetch raw close + volume for tickers with splits.

    Skips tickers whose raw_close JSON already exists in RAW_CLOSE_DIR.
    """
    _ensure_state()

    # Filter against actual cache
    print("  Loading universe to filter against cache...")
    with open(CACHE_DAILY_FILE, "rb") as f:
        universe = pickle.load(f)
    cache_set = set(universe.keys())
    del universe

    target_tickers = sorted(t for t in split_tickers if t in cache_set)
    print(f"  Split tickers in cache: {len(target_tickers)} "
          f"(of {len(split_tickers)} discovered)")

    # Skip already-fetched
    already = {os.path.splitext(f)[0]
               for f in os.listdir(RAW_CLOSE_DIR)
               if f.endswith(".json")}
    to_fetch = [t for t in target_tickers if t not in already]
    print(f"  Already cached locally: {len(already)}, to fetch: {len(to_fetch)}")

    if not to_fetch:
        print("  Nothing to refetch.")
        return target_tickers

    # Use _batched_fetch with rate-limit handling
    results, failed = _batched_fetch(
        to_fetch,
        _fetch_raw_close_one,
        label="RawClose",
        batch_size=80,
        min_sleep=0.2,
        max_sleep=10.0,
        max_retries=3,
        max_workers=40,
    )

    # Save each ticker's raw close map to disk for resumability
    for ticker, raw_map in results.items():
        out = os.path.join(RAW_CLOSE_DIR, f"{ticker}.json")
        with open(out, "w") as f:
            json.dump(raw_map, f)

    print(f"\n  Refetch complete: {len(results)} ok, {len(failed)} failed")
    if failed:
        print(f"  Failed tickers (first 20): {failed[:20]}")
        with open(os.path.join(STATE_DIR, "refetch_failed.json"), "w") as f:
            json.dump(failed, f, indent=2)

    return target_tickers


def _detect_missed_splits(df, ticker_splits):
    """For each known split, compare cache close[split_day]/close[prior_day] to
    1.0 (applied) vs B/A (missed). Classify by log-distance. Returns a list of
    missed split events; empty list means every split was applied correctly.
    """
    import math
    date_strs = df["date"].astype(str).str[:10].tolist()
    closes = df["close"].values.astype(np.float64)
    date_to_idx = {d: i for i, d in enumerate(date_strs)}
    all_dates = date_strs  # already ordered

    missed = []
    for s in ticker_splits:
        split_date = s.get("date", "")
        ratio_str = s.get("split", "")
        try:
            a_str, b_str = ratio_str.split("/")
            a = float(a_str)
            b = float(b_str)
        except (ValueError, AttributeError):
            continue
        if a <= 0 or b <= 0:
            continue
        ratio_b_a = b / a  # observed close[D]/close[D-1] ratio if the split was NOT applied

        # Map split_date to actual trading day (next trading day if split_date is weekend/holiday)
        if split_date in date_to_idx:
            split_idx = date_to_idx[split_date]
        else:
            candidates = [i for i, d in enumerate(all_dates) if d >= split_date]
            if not candidates:
                continue
            split_idx = candidates[0]

        if split_idx == 0:
            continue  # No prior bar to compare

        close_d = closes[split_idx]
        close_prior = closes[split_idx - 1]
        if close_d <= 0 or close_prior <= 0:
            continue

        observed = close_d / close_prior
        log_obs = math.log(observed)
        dist_applied = abs(log_obs)  # log(1) = 0
        dist_missed = abs(log_obs - math.log(ratio_b_a))

        if dist_missed < dist_applied:
            missed.append({
                "date": split_date,
                "ratio": ratio_str,
                "observed": observed,
                "expected_if_missed": ratio_b_a,
            })

    return missed


def apply_backfill():
    """Fix split-adjusted prices by directly verifying each known split.

    For every (ticker, split_date, ratio) in split_tickers.json, test whether
    the cache's close[split_day]/close[prior_day] is closer to 1.0 (applied)
    or to B/A (missed). Only tickers with at least one missed split get their
    OHLC rewritten from the refetched data.

    Does not modify volume, raw_close, or dvol_20d.
    """
    if not os.path.exists(BACKUP_FILE):
        print(f"  Backing up pickle -> {BACKUP_FILE}")
        shutil.copy2(CACHE_DAILY_FILE, BACKUP_FILE)
        size_mb = os.path.getsize(BACKUP_FILE) / 1024 / 1024
        print(f"  Backup size: {size_mb:.0f} MB")
    else:
        print(f"  Backup already exists: {BACKUP_FILE}")

    print("\n  Loading split registry...")
    with open(SPLIT_TICKERS_FILE) as f:
        split_registry = json.load(f)
    print(f"  Split registry has {len(split_registry)} tickers")

    print("\n  Loading universe pickle...")
    with open(CACHE_DAILY_FILE, "rb") as f:
        universe = pickle.load(f)
    print(f"  Loaded {len(universe)} tickers")

    raw_files = {os.path.splitext(f)[0]: os.path.join(RAW_CLOSE_DIR, f)
                 for f in os.listdir(RAW_CLOSE_DIR)
                 if f.endswith(".json")}
    print(f"  Refetched bar maps available: {len(raw_files)}")

    missed_by_ticker = {}
    t0 = time.time()

    print("\n  Verifying each known split...")
    tickers = sorted(split_registry.keys())
    for i, ticker in enumerate(tickers):
        if ticker not in universe:
            continue
        df = universe[ticker]
        missed = _detect_missed_splits(df, split_registry[ticker])
        if missed:
            missed_by_ticker[ticker] = missed
        if (i + 1) % 500 == 0:
            print(f"    {i+1}/{len(tickers)} tickers checked "
                  f"({len(missed_by_ticker)} have missed splits) "
                  f"[{time.time()-t0:.0f}s]")

    print(f"\n  Tickers with at least one missed split: {len(missed_by_ticker)}")

    if not missed_by_ticker:
        print("  No missed splits detected. Nothing to apply.")
        return

    # Rewrite OHLC for only the missed-split tickers
    applied_count = 0
    for ticker, missed in missed_by_ticker.items():
        if ticker not in raw_files:
            print(f"  WARN: {ticker} has missed splits but no refetch data — skipping")
            continue
        df = universe[ticker]
        with open(raw_files[ticker]) as f:
            bar_map = json.load(f)
        date_strs = df["date"].astype(str).str[:10].tolist()

        new_open = df["open"].values.astype(np.float32).copy()
        new_high = df["high"].values.astype(np.float32).copy()
        new_low = df["low"].values.astype(np.float32).copy()
        new_close = df["close"].values.astype(np.float32).copy()
        for idx, d in enumerate(date_strs):
            bar = bar_map.get(d)
            if not bar:
                continue
            ro, rh, rl, rc, ac, _v = bar
            if rc > 0 and ac > 0:
                k = ac / rc
                new_open[idx] = ro * k
                new_high[idx] = rh * k
                new_low[idx] = rl * k
                new_close[idx] = ac
        df["open"] = new_open
        df["high"] = new_high
        df["low"] = new_low
        df["close"] = new_close
        applied_count += 1

    print(f"\n  Rewrote OHLC for {applied_count} tickers with missed splits.")
    print("  Missed-split detail (ticker: [split_date ratio obs->expected_if_missed]):")
    for ticker, missed in sorted(missed_by_ticker.items(),
                                 key=lambda kv: -max(m['expected_if_missed']
                                                     for m in kv[1])):
        parts = [f"{m['date']} {m['ratio']} obs={m['observed']:.3f} "
                 f"exp_miss={m['expected_if_missed']:.3f}" for m in missed]
        print(f"    {ticker}: {'; '.join(parts)}")

    tmp = CACHE_DAILY_FILE + ".tmp"
    print(f"\n  Saving pickle -> {tmp} (then atomic rename)...")
    with open(tmp, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, CACHE_DAILY_FILE)
    size_mb = os.path.getsize(CACHE_DAILY_FILE) / 1024 / 1024
    print(f"  Saved {CACHE_DAILY_FILE} ({size_mb:.0f} MB)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true",
                        help="Step 1: discover split tickers")
    parser.add_argument("--refetch", action="store_true",
                        help="Step 2: refetch raw close for split tickers")
    parser.add_argument("--apply", action="store_true",
                        help="Steps 3-5: backfill raw_close column + dvol_20d, save")
    parser.add_argument("--all", action="store_true",
                        help="Run all steps in order")
    args = parser.parse_args()

    if not (args.discover or args.refetch or args.apply or args.all):
        parser.print_help()
        return

    if args.discover or args.all:
        print("\n" + "=" * 70 + "\nSTEP 1: Discover split tickers\n" + "=" * 70)
        splits = discover_split_tickers()
    else:
        if not os.path.exists(SPLIT_TICKERS_FILE):
            print("ERROR: split_tickers.json missing -- run --discover first")
            sys.exit(1)
        with open(SPLIT_TICKERS_FILE) as f:
            splits = json.load(f)

    if args.refetch or args.all:
        print("\n" + "=" * 70 + "\nSTEP 2: Refetch raw close\n" + "=" * 70)
        refetch_raw_close(splits)

    if args.apply or args.all:
        print("\n" + "=" * 70 + "\nSTEP 3-5: Apply + save\n" + "=" * 70)
        apply_backfill()


if __name__ == "__main__":
    main()
