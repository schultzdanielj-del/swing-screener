"""
THE GRINDER v2 — Spiderweb expression discovery engine.

Two phases:
  Phase 1 (fixed): Compute all expressions across examples + universe → value matrix
  Phase 2 (slider): Spiderweb search for optimal condition combos

Grind levels (controlled by slider):
  1 - Quick scan    : beam_width=10,  depth=5   (~30s)
  2 - Light grind   : beam_width=25,  depth=8   (~2 min)
  3 - Medium grind  : beam_width=50,  depth=10  (~10 min)
  4 - Heavy grind   : beam_width=100, depth=12  (~30 min)
  5 - Overnight     : beam_width=250, depth=15  (~2-8 hours)

Usage:
    python local_runner/grinder.py --setup dtss --level 3
    python local_runner/grinder.py --setup dtss --test  (quick validation)
"""

import os
import sys
import json
import time
import pickle
import argparse
import numpy as np
import pandas as pd
import requests
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.expression_engine import ExpressionEngine
from local_runner.spiderweb import SpiderwebSearch

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
API_BASE = "https://web-production-e3025.up.railway.app"

# Grind level presets
GRIND_LEVELS = {
    1: {"name": "Quick scan",    "beam_width": 10,  "depth": 5,  "est_time": "~30s"},
    2: {"name": "Light grind",   "beam_width": 25,  "depth": 8,  "est_time": "~2 min"},
    3: {"name": "Medium grind",  "beam_width": 50,  "depth": 10, "est_time": "~10 min"},
    4: {"name": "Heavy grind",   "beam_width": 100, "depth": 12, "est_time": "~30 min"},
    5: {"name": "Overnight",     "beam_width": 250, "depth": 15, "est_time": "~2-8 hours"},
}


def load_ohlcv_cache():
    cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        print("Cache not found. Run: python local_runner/cache_builder.py")
        sys.exit(1)
    with open(cache_file, "rb") as f:
        return pickle.load(f)


def load_expressions(test=False):
    if test:
        path = os.path.join(REPO_ROOT, "data", "dtss_expressions_test.json")
    else:
        path = os.path.join(CACHE_DIR, "brute_expressions.json")

    if not os.path.exists(path):
        if not test:
            print("Expressions not found. Run: python local_runner/brute_expressions.py")
            sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    return data["expressions"]


def load_examples(setup_type):
    r = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    examples = data.get("examples", [])

    result = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate") or ex.get("chartDate")
        ex_id = ex.get("id")
        try:
            r2 = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{ex_id}", timeout=15)
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

            target_idx = len(df) - 1
            if entry_date:
                matches = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
                if len(matches) > 0:
                    target_idx = matches.index[0]

            result.append({
                "ticker": ticker, "entry_date": entry_date,
                "df": df, "target_idx": target_idx, "id": ex_id,
            })
        except Exception as e:
            print(f"  ✗ {ticker}: {e}")
    return result


def compute_ticker_values(df, target_idx, expressions):
    """Compute all expression values for one ticker. Returns numpy array."""
    engine = ExpressionEngine(df)
    engine.set_target(target_idx)
    values = np.full(len(expressions), np.nan)
    for j, expr in enumerate(expressions):
        val = engine.compute(expr)
        if val is not None and not np.isnan(val):
            values[j] = val
    return values


def load_or_compute_matrix(setup_type, expressions, test=False):
    """Load precomputed matrix or compute from scratch."""
    matrix_file = os.path.join(CACHE_DIR, f"value_matrix_{setup_type}{'_test' if test else ''}.pkl")

    if os.path.exists(matrix_file):
        with open(matrix_file, "rb") as f:
            data = pickle.load(f)
        if data.get("n_exprs") == len(expressions):
            print(f"  Loaded precomputed matrix ({data['n_examples']} examples, "
                  f"{data['n_universe']} universe)")
            return data
        print(f"  Matrix stale (had {data.get('n_exprs')} exprs, need {len(expressions)})")

    print(f"\n  Computing value matrix (this is the fixed-cost phase)...")

    # Examples
    print(f"  Loading examples...")
    examples = load_examples(setup_type)
    print(f"  {len(examples)} examples loaded")

    print(f"  Computing example values...")
    t0 = time.time()
    example_matrix = np.full((len(examples), len(expressions)), np.nan)
    example_tickers = []
    for i, ex in enumerate(examples):
        example_matrix[i] = compute_ticker_values(ex["df"], ex["target_idx"], expressions)
        n_valid = np.sum(~np.isnan(example_matrix[i]))
        print(f"    ✓ {ex['ticker']:8s} ({ex['entry_date']}) — {n_valid}/{len(expressions)}")
        example_tickers.append(f"{ex['ticker']}_{ex['id']}")
    t_ex = time.time() - t0
    print(f"  Examples done: {t_ex:.1f}s")

    # Universe
    print(f"\n  Loading universe cache...")
    universe_cache = load_ohlcv_cache()
    if test:
        tickers = list(universe_cache.keys())[:100]
        universe_cache = {t: universe_cache[t] for t in tickers}
    print(f"  {len(universe_cache)} tickers")

    print(f"  Computing universe values...")
    t0 = time.time()
    uni_tickers = list(universe_cache.keys())
    universe_matrix = np.full((len(uni_tickers), len(expressions)), np.nan)

    for i, ticker in enumerate(uni_tickers):
        df = universe_cache[ticker]
        if df is None or len(df) < 50:
            continue
        universe_matrix[i] = compute_ticker_values(df, len(df) - 1, expressions)

        if (i + 1) % 200 == 0 or (i + 1) == len(uni_tickers):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(uni_tickers) - i - 1) / rate if rate > 0 else 0
            print(f"    {i+1:,}/{len(uni_tickers):,} ({(i+1)/len(uni_tickers)*100:.0f}%) "
                  f"[{elapsed:.0f}s, ~{eta:.0f}s left, {rate:.0f}/s]")

    t_uni = time.time() - t0
    print(f"  Universe done: {t_uni:.0f}s ({t_uni/60:.1f} min)")

    data = {
        "example_matrix": example_matrix,
        "universe_matrix": universe_matrix,
        "example_tickers": example_tickers,
        "universe_tickers": uni_tickers,
        "expr_names": [e["name"] for e in expressions],
        "expr_categories": [e.get("category", "unknown") for e in expressions],
        "n_exprs": len(expressions),
        "n_examples": len(examples),
        "n_universe": len(uni_tickers),
        "computed_at": datetime.now().isoformat(),
    }
    with open(matrix_file, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = os.path.getsize(matrix_file) / 1024 / 1024
    print(f"  Saved matrix: {matrix_file} ({size_mb:.1f} MB)")

    return data


def upload_results(setup_type, results):
    try:
        payload = json.loads(json.dumps(results, default=str))
        r = requests.post(f"{API_BASE}/api/analysis/grinder-results", json={
            "setup_type": setup_type,
            "results": payload,
        }, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"  Upload error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="THE GRINDER v2 — Spiderweb Search")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--level", type=int, default=3, choices=[1, 2, 3, 4, 5],
                        help="Grind level 1-5")
    parser.add_argument("--test", action="store_true", help="Test mode (25 expr, 100 tickers)")
    args = parser.parse_args()

    if args.test:
        args.level = 1

    level = GRIND_LEVELS[args.level]

    print("\n" + "=" * 70)
    print("  ╔════════════════════════════════════════════╗")
    print("  ║          T H E   G R I N D E R  v2        ║")
    print("  ║       Spiderweb Combination Search         ║")
    print("  ╚════════════════════════════════════════════╝")
    print("=" * 70)
    print(f"\n  Setup:  {args.setup.upper()}")
    print(f"  Level:  {args.level} — {level['name']} ({level['est_time']})")
    print(f"  Depth:  {level['depth']}  |  Beam width: {level['beam_width']}")
    print(f"  Mode:   {'TEST' if args.test else 'FULL'}")
    print(f"  Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Phase 1: Value matrix ────────────────────────────
    print(f"\n{'━'*70}")
    print(f"  PHASE 1: Expression Value Matrix")
    print(f"{'━'*70}")

    expressions = load_expressions(test=args.test)
    print(f"  {len(expressions):,} expressions loaded")

    t0_total = time.time()
    matrix = load_or_compute_matrix(args.setup, expressions, test=args.test)

    # ── Phase 2: Spiderweb search ────────────────────────
    print(f"\n{'━'*70}")
    print(f"  PHASE 2: Spiderweb Search (Level {args.level}: {level['name']})")
    print(f"{'━'*70}")

    search = SpiderwebSearch(
        example_values=matrix["example_matrix"],
        universe_values=matrix["universe_matrix"],
        expr_names=matrix["expr_names"],
        expr_categories=matrix["expr_categories"],
    )

    results = search.run(
        depth=level["depth"],
        beam_width=level["beam_width"],
    )

    total_time = time.time() - t0_total

    # ── Results ──────────────────────────────────────────
    print(f"\n{'━'*70}")
    print(f"  R E S U L T S")
    print(f"{'━'*70}")

    if "error" in results:
        print(f"\n  ✗ {results['error']}")
        return

    stats = results["stats"]
    print(f"\n  Total time:        {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Nodes explored:    {stats['nodes_explored']:,}")
    print(f"  Nodes/second:      {stats['nodes_per_second']:,}")
    print(f"  Depth reached:     {stats['depth_reached']}")
    print(f"  Universe tickers:  {stats['n_universe']:,}")
    print(f"  Examples:          {stats['n_examples']}")

    print(f"\n  ═══ BEST COMBO ═══")
    print(f"  Pass rate: {results['best_rate']:.2%} ({results['best_passing']:,} / "
          f"{stats['n_universe']:,} tickers)")
    print(f"  Conditions ({results['best_depth']}):")
    for t in results["best_thresholds"]:
        print(f"    [{t['category']:>20}]  {t['expr']:40s}  "
              f"[{t['low']:.4f} — {t['high']:.4f}]")

    print(f"\n  ═══ PROGRESSION ═══")
    print(f"  {'Level':>5} {'Pass%':>7} {'Tickers':>8} {'Paths':>6} {'Time':>6}")
    print(f"  {'─'*5} {'─'*7} {'─'*8} {'─'*6} {'─'*6}")
    for lv in results["levels"]:
        print(f"  {lv['level']:5d} {lv['best_rate']:7.2%} {lv['best_passing']:8,} "
              f"{lv['paths_explored']:6,} {lv['elapsed_s']:6.1f}s")

    # ── Save ─────────────────────────────────────────────
    out = {
        "setup_type": args.setup,
        "grind_level": args.level,
        "grind_name": level["name"],
        "timestamp": datetime.now().isoformat(),
        "total_time_s": round(total_time, 1),
        **results,
    }

    out_path = os.path.join(CACHE_DIR, f"grinder_results_{args.setup}.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    print(f"  Uploading to Railway...")
    if upload_results(args.setup, out):
        print(f"  ✓ Uploaded")
    else:
        print(f"  ✗ Upload failed (results saved locally)")

    print(f"\n{'━'*70}")
    print(f"  DONE. Best filter: {results['best_rate']:.2%} of universe passes "
          f"({results['best_passing']:,} tickers)")
    print(f"{'━'*70}\n")


if __name__ == "__main__":
    main()
