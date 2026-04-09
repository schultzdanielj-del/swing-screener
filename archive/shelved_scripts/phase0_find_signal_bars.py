"""
Phase 0: Find Example Signal Bars

Runs Step 3 pyramid conditions against each example ticker's OHLCV history.
For each example, finds the signal bar(s) that fire on or before the entry date.
Picks the latest one as the anchor point for all outcome analysis.

Two modes:
  --api     Fetch OHLCV from Railway API (sandbox/remote, 23 tickers only)
  --cache   Load from local 5yr pickle cache (desktop, full universe)

Usage:
    python scripts/phase0_find_signal_bars.py --setup dtss --api
    python scripts/phase0_find_signal_bars.py --setup dtss --cache

Output:
    data/outcome_grind/phase0_signal_bars_{setup}.json
"""

import argparse
import sys
import os
import json
import time
import numpy as np
import pandas as pd
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series

RAILWAY_URL = "https://web-production-e3025.up.railway.app"


# ============================================================
# Data Loading
# ============================================================

def load_pyramid_conditions(path: str) -> list:
    """Load conditions from pyramid_results JSON."""
    with open(path) as f:
        data = json.load(f)
    conditions = data["all_conditions"]
    # Normalize: conditions use "compute" key with op/params + low/high thresholds
    for c in conditions:
        if "compute" not in c and "op" in c:
            c["compute"] = {k: v for k, v in c.items() if k not in ("name", "expr", "category", "low", "high", "tier")}
    print(f"Loaded {len(conditions)} conditions from {os.path.basename(path)}")
    return conditions


def load_examples(setup_type: str) -> list:
    """Load validated examples from Railway."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    d = r.json()
    examples = d["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def fetch_ohlcv_api(ticker: str, lookback: int = 1500) -> pd.DataFrame:
    """Fetch OHLCV from Railway API."""
    r = requests.get(f"{RAILWAY_URL}/api/ohlcv/bulk/{ticker}",
                     params={"lookback": lookback})
    r.raise_for_status()
    data = r.json()
    if "error" in data or not data.get("results"):
        raise ValueError(f"No OHLCV for {ticker}")
    df = pd.DataFrame(data["results"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_cache(cache_path: str) -> dict:
    """Load local 5yr pickle cache."""
    import pickle
    print(f"Loading cache from {cache_path}...")
    t0 = time.time()
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)
    print(f"  {len(cache):,} tickers in {time.time()-t0:.1f}s")
    return cache


# ============================================================
# Core: Find signal bars for one ticker
# ============================================================

def find_signal_bars(df: pd.DataFrame, conditions: list, before_date: str = None) -> list:
    """Find all bars where ALL conditions pass.

    Args:
        df: OHLCV DataFrame for one ticker
        conditions: list of condition dicts with 'compute', 'low', 'high'
        before_date: if set, only return signals on or before this date

    Returns:
        list of dicts: {bar_idx, date}
    """
    if len(df) < 100:
        return []

    engine = ExpressionEngine(df)
    n_bars = len(df)
    pass_mask = np.ones(n_bars, dtype=bool)
    pass_mask[:50] = False  # skip warmup

    for cond in conditions:
        series = compute_series(engine, cond["compute"])
        low, high = cond["low"], cond["high"]
        in_range = (series >= low) & (series <= high)
        in_range[np.isnan(series)] = False
        pass_mask &= in_range

    # Filter by date if specified
    if before_date:
        dates = df["date"].astype(str).str[:10].values
        date_mask = dates <= before_date
        pass_mask &= date_mask

    signal_indices = np.where(pass_mask)[0]
    results = []
    for idx in signal_indices:
        d = df["date"].iloc[idx]
        date_str = str(d)[:10] if not hasattr(d, "date") else str(d.date())
        results.append({
            "bar_idx": int(idx),
            "date": date_str,
        })

    return results


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Phase 0: Find Example Signal Bars")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--pyramid-results", default=None,
                        help="Path to pyramid_results JSON (default: auto-detect)")
    parser.add_argument("--api", action="store_true",
                        help="Fetch OHLCV from Railway API (sandbox mode)")
    parser.add_argument("--cache", action="store_true",
                        help="Load from local 5yr pickle cache (desktop mode)")
    parser.add_argument("--cache-path", default=None,
                        help="Path to 5yr cache pickle")
    parser.add_argument("--max-gap", type=int, default=30,
                        help="Max calendar days between signal and entry (default: 30)")
    args = parser.parse_args()

    if not args.api and not args.cache:
        print("ERROR: Specify --api or --cache")
        return

    print(f"Phase 0: Find Example Signal Bars")
    print(f"Setup: {args.setup.upper()}")
    print()

    # ── 1. Load pyramid conditions ──
    if args.pyramid_results:
        pyramid_path = args.pyramid_results
    else:
        # Auto-detect: check local_runner/cache first, then data/
        candidates = [
            os.path.join(REPO_ROOT, "local_runner", "cache", f"pyramid_results_{args.setup}.json"),
            os.path.join(REPO_ROOT, "data", f"pyramid_results_{args.setup}.json"),
        ]
        pyramid_path = None
        for p in candidates:
            if os.path.exists(p):
                pyramid_path = p
                break
        if not pyramid_path:
            print(f"ERROR: No pyramid results found. Tried: {candidates}")
            print(f"  Provide --pyramid-results <path>")
            return

    conditions = load_pyramid_conditions(pyramid_path)

    # ── 2. Load examples ──
    examples = load_examples(args.setup)
    if not examples:
        print("ERROR: No examples found")
        return

    # ── 3. Load OHLCV data ──
    universe_cache = None
    if args.cache:
        cache_path = args.cache_path or os.path.join(
            REPO_ROOT, "local_runner", "cache", "universe_ohlcv_5yr.pkl")
        if not os.path.exists(cache_path):
            print(f"ERROR: Cache not found at {cache_path}")
            return
        universe_cache = load_cache(cache_path)

    # ── 4. Process each example ──
    print(f"\nScanning {len(examples)} example tickers...")
    print(f"{'='*80}")

    results = []
    matched = 0
    unmatched = 0

    for ex in examples:
        ticker = ex["ticker"]
        entry_date = ex["entryDate"]
        print(f"\n  {ticker} (entry: {entry_date})")

        # Fetch OHLCV
        try:
            if args.cache:
                df = universe_cache.get(ticker)
                if df is None:
                    print(f"    ✗ Not in cache")
                    results.append({
                        "ticker": ticker,
                        "entry_date": entry_date,
                        "status": "not_in_cache",
                        "signal_date": None,
                        "gap_bars": None,
                    })
                    unmatched += 1
                    continue
            else:
                df = fetch_ohlcv_api(ticker)
        except Exception as e:
            print(f"    ✗ OHLCV fetch failed: {e}")
            results.append({
                "ticker": ticker,
                "entry_date": entry_date,
                "status": "fetch_error",
                "signal_date": None,
                "gap_bars": None,
                "error": str(e),
            })
            unmatched += 1
            continue

        # Find all signal bars on or before entry date
        signal_bars = find_signal_bars(df, conditions, before_date=entry_date)

        if not signal_bars:
            print(f"    ✗ No signal bars found on or before {entry_date}")
            # Also check: any signals at ALL for this ticker?
            all_signals = find_signal_bars(df, conditions, before_date=None)
            if all_signals:
                print(f"    ℹ {len(all_signals)} signals exist but all AFTER entry date")
                print(f"      First: {all_signals[0]['date']}, Last: {all_signals[-1]['date']}")
            else:
                print(f"    ℹ Zero signals for this ticker across entire history")

            results.append({
                "ticker": ticker,
                "entry_date": entry_date,
                "status": "no_signal_found",
                "signal_date": None,
                "gap_bars": None,
                "total_signals_in_history": len(all_signals),
            })
            unmatched += 1
            continue

        # Pick the LATEST signal bar on or before entry
        best = signal_bars[-1]
        signal_date = best["date"]
        signal_idx = best["bar_idx"]

        # Compute gap in trading days
        dates = df["date"].astype(str).str[:10].values
        entry_idx = None
        for j in range(signal_idx, min(signal_idx + 60, len(df))):
            if dates[j] == entry_date:
                entry_idx = j
                break

        if entry_idx is not None:
            gap_bars = entry_idx - signal_idx
        else:
            # Entry date not in OHLCV (might be a date format mismatch)
            # Estimate gap from calendar days
            from datetime import datetime
            sig_dt = datetime.strptime(signal_date, "%Y-%m-%d")
            ent_dt = datetime.strptime(entry_date, "%Y-%m-%d")
            gap_cal = (ent_dt - sig_dt).days
            gap_bars = gap_cal  # approximate
            print(f"    ⚠ Entry date {entry_date} not found in OHLCV, gap ~{gap_cal} cal days")

        # Check gap is reasonable
        from datetime import datetime
        sig_dt = datetime.strptime(signal_date, "%Y-%m-%d")
        ent_dt = datetime.strptime(entry_date, "%Y-%m-%d")
        gap_cal = (ent_dt - sig_dt).days

        if gap_cal > args.max_gap:
            print(f"    ⚠ Signal {signal_date} is {gap_cal} cal days before entry — too far? (max: {args.max_gap})")
            # Still record it but flag it
            status = "match_distant"
        elif gap_cal < 0:
            print(f"    ⚠ Signal {signal_date} is AFTER entry {entry_date}??")
            status = "match_error"
        else:
            status = "matched"

        # Count total signals for this ticker (context)
        n_total_signals = len(signal_bars)

        print(f"    ✓ Signal: {signal_date} (gap: {gap_bars} bars, {gap_cal} cal days)"
              f"  [{n_total_signals} total signals on/before entry]")

        results.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "status": status,
            "signal_date": signal_date,
            "signal_bar_idx": signal_idx,
            "gap_bars": gap_bars,
            "gap_cal_days": gap_cal,
            "n_signals_before_entry": n_total_signals,
        })
        matched += 1

    # ── 5. Summary ──
    print(f"\n{'='*80}")
    print(f"PHASE 0 RESULTS")
    print(f"{'='*80}")
    print(f"  Examples:  {len(examples)}")
    print(f"  Matched:   {matched}")
    print(f"  Unmatched: {unmatched}")

    if matched > 0:
        matched_results = [r for r in results if r["status"] in ("matched", "match_distant")]
        gaps = [r["gap_bars"] for r in matched_results if r["gap_bars"] is not None]
        if gaps:
            print(f"\n  Gap (signal → entry) stats:")
            print(f"    Min:    {min(gaps)} bars")
            print(f"    Max:    {max(gaps)} bars")
            print(f"    Avg:    {np.mean(gaps):.1f} bars")
            print(f"    Median: {np.median(gaps):.0f} bars")

        # Distribution
        print(f"\n  Per-example results:")
        for r in results:
            status_icon = "✓" if r["status"] == "matched" else "⚠" if "match" in (r.get("status") or "") else "✗"
            sig = r.get("signal_date", "—")
            gap = r.get("gap_bars", "—")
            print(f"    {status_icon} {r['ticker']:8s} entry={r['entry_date']}  "
                  f"signal={sig}  gap={gap}")

    # ── 6. Save results ──
    outdir = os.path.join(REPO_ROOT, "data", "outcome_grind")
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, f"phase0_signal_bars_{args.setup}.json")

    output = {
        "setup_type": args.setup,
        "n_conditions": len(conditions),
        "n_examples": len(examples),
        "n_matched": matched,
        "n_unmatched": unmatched,
        "pyramid_results_file": os.path.basename(pyramid_path),
        "results": results,
    }

    with open(outpath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {outpath}")

    from file_mirror import mirror_file
    mirror_file(outpath)


if __name__ == "__main__":
    main()
