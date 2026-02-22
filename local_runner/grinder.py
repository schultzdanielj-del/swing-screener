"""
THE GRINDER — Brute force every expression against examples + universe.

Usage:
    python local_runner/grinder.py [--setup dtss] [--test]

Workflow:
  1. Load OHLCV cache (build if needed)
  2. Load expressions
  3. Compute all expressions on all examples → example_values matrix
  4. Compute all expressions on all universe tickers → universe_values matrix
  5. Score each expression: how well does it separate examples from universe?
  6. Save results + upload to Railway

--test flag: run with 25 test expressions on 50 universe tickers
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

# Add parent dir so we can import from scripts/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.expression_engine import ExpressionEngine

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
API_BASE = "https://web-production-e3025.up.railway.app"


def load_ohlcv_cache():
    """Load the universe OHLCV cache."""
    cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        print("Cache not found. Building...")
        from local_runner.cache_builder import build_cache
        return build_cache()
    with open(cache_file, "rb") as f:
        return pickle.load(f)


def load_expressions(test=False):
    """Load expression definitions."""
    if test:
        path = os.path.join(REPO_ROOT, "data", "dtss_expressions_test.json")
    else:
        path = os.path.join(CACHE_DIR, "brute_expressions.json")
    with open(path) as f:
        data = json.load(f)
    return data["expressions"]


def load_examples(setup_type):
    """Load example data from Railway API."""
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

        # Fetch OHLCV for this example
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

            # Find entry index
            target_idx = len(df) - 1
            if entry_date:
                matches = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
                if len(matches) > 0:
                    target_idx = matches.index[0]

            result.append({
                "ticker": ticker,
                "entry_date": entry_date,
                "df": df,
                "target_idx": target_idx,
                "id": ex_id,
            })
        except Exception as e:
            print(f"  ✗ {ticker}: {e}")

    return result


def compute_values(df, target_idx, expressions):
    """Compute all expression values for one ticker."""
    engine = ExpressionEngine(df)
    engine.set_target(target_idx)
    values = {}
    for expr in expressions:
        val = engine.compute(expr)
        values[expr["name"]] = float(val) if val is not None and not np.isnan(val) else None
    return values


def score_expression(example_vals, universe_vals, expr_name):
    """Score how well an expression separates examples from universe.

    Returns dict with:
      - ex_mean, ex_std, ex_min, ex_max: example statistics
      - uni_mean, uni_std: universe statistics
      - separation: |ex_mean - uni_mean| / pooled_std (Cohen's d style)
      - consistency: 1 - (ex_std / |ex_range|) — tight clustering in examples
      - selectivity: what % of universe falls within example range
      - score: combined metric
    """
    # Gather example values (skip None)
    ex_vals = [example_vals[t][expr_name] for t in example_vals
               if example_vals[t].get(expr_name) is not None]
    uni_vals = [universe_vals[t][expr_name] for t in universe_vals
                if universe_vals[t].get(expr_name) is not None]

    if len(ex_vals) < 3 or len(uni_vals) < 10:
        return None

    ex = np.array(ex_vals)
    uni = np.array(uni_vals)

    ex_mean = np.mean(ex)
    ex_std = np.std(ex)
    ex_min = np.min(ex)
    ex_max = np.max(ex)
    ex_range = ex_max - ex_min
    uni_mean = np.mean(uni)
    uni_std = np.std(uni)

    # Separation (Cohen's d)
    pooled_std = np.sqrt((ex_std**2 + uni_std**2) / 2)
    separation = abs(ex_mean - uni_mean) / pooled_std if pooled_std > 0 else 0

    # Consistency: how tight are examples? (0 = scattered, 1 = identical)
    consistency = 1 - (ex_std / abs(ex_range)) if ex_range != 0 else 1.0
    consistency = max(0, min(1, consistency))

    # Selectivity: what % of universe falls within example range?
    # Lower = more selective = better
    in_range = np.sum((uni >= ex_min) & (uni <= ex_max))
    selectivity = in_range / len(uni) if len(uni) > 0 else 1.0

    # Combined score: high separation + high consistency + low selectivity
    score = separation * (1 + consistency) * (1 - selectivity)

    return {
        "expr_name": expr_name,
        "ex_count": len(ex_vals),
        "uni_count": len(uni_vals),
        "ex_mean": round(ex_mean, 6),
        "ex_std": round(ex_std, 6),
        "ex_min": round(ex_min, 6),
        "ex_max": round(ex_max, 6),
        "uni_mean": round(uni_mean, 6),
        "uni_std": round(uni_std, 6),
        "separation": round(separation, 4),
        "consistency": round(consistency, 4),
        "selectivity": round(selectivity, 4),
        "score": round(score, 4),
    }


def upload_results(setup_type, results):
    """Upload grinder results to Railway API."""
    try:
        r = requests.post(f"{API_BASE}/api/analysis/grinder-results", json={
            "setup_type": setup_type,
            "timestamp": datetime.now().isoformat(),
            "total_expressions": results["total_expressions"],
            "total_tickers": results["total_tickers"],
            "total_examples": results["total_examples"],
            "compute_time_s": results["compute_time_s"],
            "top_expressions": results["top_expressions"][:100],
        }, timeout=30)
        return r.status_code == 200
    except:
        return False


def main():
    parser = argparse.ArgumentParser(description="THE GRINDER")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--test", action="store_true", help="Test mode (25 expr, 50 tickers)")
    parser.add_argument("--max-universe", type=int, default=0, help="Limit universe tickers (0=all)")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  ╔════════════════════════════════════════════╗")
    print("  ║          T H E   G R I N D E R             ║")
    print("  ║   Brute Force Expression Discovery Engine  ║")
    print("  ╚════════════════════════════════════════════╝")
    print("=" * 70)
    print(f"\n  Setup: {args.setup.upper()}")
    print(f"  Mode:  {'TEST (25 expr, 50 tickers)' if args.test else 'FULL BRUTE FORCE'}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Step 1: Load cache ───────────────────────────────
    print(f"\n{'─'*60}")
    print("  Step 1: Loading OHLCV cache")
    print(f"{'─'*60}")
    t0 = time.time()
    universe_cache = load_ohlcv_cache()
    t_cache = time.time() - t0
    print(f"  Loaded {len(universe_cache):,} tickers ({t_cache:.1f}s)")

    if args.test and args.max_universe == 0:
        args.max_universe = 50

    if args.max_universe > 0:
        tickers = list(universe_cache.keys())[:args.max_universe]
        universe_cache = {t: universe_cache[t] for t in tickers}
        print(f"  Limited to {len(universe_cache)} tickers (--max-universe)")

    # ── Step 2: Load expressions ─────────────────────────
    print(f"\n{'─'*60}")
    print("  Step 2: Loading expressions")
    print(f"{'─'*60}")
    expressions = load_expressions(test=args.test)
    print(f"  Loaded {len(expressions):,} expressions")

    # ── Step 3: Compute examples ─────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Step 3: Computing examples ({args.setup.upper()})")
    print(f"{'─'*60}")
    t0 = time.time()
    examples = load_examples(args.setup)
    print(f"  Loaded {len(examples)} examples from API")

    example_values = {}
    for ex in examples:
        ticker = ex["ticker"]
        values = compute_values(ex["df"], ex["target_idx"], expressions)
        non_null = sum(1 for v in values.values() if v is not None)
        example_values[f"{ticker}_{ex['id']}"] = values
        print(f"  ✓ {ticker:8s} ({ex['entry_date']}) — {non_null}/{len(expressions)}")

    t_examples = time.time() - t0
    print(f"\n  Examples done: {len(example_values)} in {t_examples:.1f}s")

    # ── Step 4: Compute universe ─────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Step 4: Computing universe ({len(universe_cache):,} tickers)")
    print(f"{'─'*60}")
    t0 = time.time()
    universe_values = {}
    errors = 0
    total = len(universe_cache)

    for i, (ticker, df) in enumerate(universe_cache.items()):
        if df is None or len(df) < 50:
            errors += 1
            continue

        target_idx = len(df) - 1
        try:
            values = compute_values(df, target_idx, expressions)
            universe_values[ticker] = values
        except Exception:
            errors += 1

        # Progress every 100 tickers
        if (i + 1) % 100 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate if rate > 0 else 0
            pct = (i + 1) / total * 100
            print(f"  {i+1:,}/{total:,} ({pct:.0f}%) "
                  f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining, "
                  f"{rate:.1f} tickers/s]")

    t_universe = time.time() - t0
    print(f"\n  Universe done: {len(universe_values):,} tickers in {t_universe:.0f}s "
          f"({t_universe/60:.1f} min)")
    if errors:
        print(f"  Skipped: {errors}")

    # ── Step 5: Score expressions ────────────────────────
    print(f"\n{'─'*60}")
    print("  Step 5: Scoring expressions")
    print(f"{'─'*60}")
    t0 = time.time()
    scores = []
    for expr in expressions:
        result = score_expression(example_values, universe_values, expr["name"])
        if result:
            result["category"] = expr.get("category", "unknown")
            scores.append(result)

    scores.sort(key=lambda x: x["score"], reverse=True)
    t_score = time.time() - t0
    print(f"  Scored {len(scores):,} expressions in {t_score:.1f}s")

    # ── Results ──────────────────────────────────────────
    total_time = t_cache + t_examples + t_universe + t_score
    print(f"\n{'='*70}")
    print(f"  R E S U L T S")
    print(f"{'='*70}")
    print(f"\n  Total time:    {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Expressions:   {len(expressions):,}")
    print(f"  Examples:      {len(example_values)}")
    print(f"  Universe:      {len(universe_values):,}")
    print(f"\n  TOP 30 EXPRESSIONS:")
    print(f"  {'Rank':>4} {'Score':>7} {'Sep':>6} {'Con':>5} {'Sel':>5} {'Category':>20}  Name")
    print(f"  {'─'*4} {'─'*7} {'─'*6} {'─'*5} {'─'*5} {'─'*20}  {'─'*30}")

    for i, s in enumerate(scores[:30]):
        print(f"  {i+1:4d} {s['score']:7.3f} {s['separation']:6.2f} "
              f"{s['consistency']:5.2f} {s['selectivity']:5.2f} "
              f"{s['category']:>20}  {s['expr_name']}")

    # Category breakdown of top 50
    print(f"\n  TOP 50 BY CATEGORY:")
    cat_counts = {}
    for s in scores[:50]:
        cat = s["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat:30s} {n:3d}")

    # ── Save results ─────────────────────────────────────
    results = {
        "setup_type": args.setup,
        "timestamp": datetime.now().isoformat(),
        "total_expressions": len(expressions),
        "total_tickers": len(universe_values),
        "total_examples": len(example_values),
        "compute_time_s": round(total_time, 1),
        "top_expressions": scores[:200],
        "all_scores": scores,
    }

    out_path = os.path.join(CACHE_DIR, f"grinder_results_{args.setup}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Also save full value matrices for later analysis
    matrix_path = os.path.join(CACHE_DIR, f"grinder_matrix_{args.setup}.pkl")
    with open(matrix_path, "wb") as f:
        pickle.dump({
            "example_values": example_values,
            "universe_values": universe_values,
            "expressions": [e["name"] for e in expressions],
        }, f, protocol=pickle.HIGHEST_PROTOCOL)
    matrix_mb = os.path.getsize(matrix_path) / 1024 / 1024
    print(f"  Saved matrix: {matrix_path} ({matrix_mb:.1f} MB)")

    # Upload to Railway
    print(f"\n  Uploading top results to Railway...")
    if upload_results(args.setup, results):
        print(f"  ✓ Uploaded successfully")
    else:
        print(f"  ✗ Upload failed (results saved locally)")

    print(f"\n{'='*70}")
    print(f"  DONE. {len(scores):,} expressions scored in {total_time/60:.1f} minutes.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
