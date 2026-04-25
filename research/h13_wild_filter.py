"""H13: Apply the locked ruleset to wild deduplicated double signals.

For each wild cluster (is_example=0):
  sig = rightmost.bar_idx
  Auto-detect support MA (longest in {SMA50/100/200, EMA3/8/21} within t=2.0 ADR of sig_close)
  Compute foothold = (sig_low - MA(sig)) / ADR
  Categorize:
    - 'mature_ready'         : foothold <= T_foothold (= 1.856 ADR derived from examples)
    - 'needs_more_sideways'  : foothold > T_foothold
    - 'no_support_in_range'  : no MA qualifies (no auto-detected support)
    - 'insufficient_history' : sig_idx too small for ADR or MA computation

Tally per-setup.
"""
import json
import pickle
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
POOL_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/research/classifier_pool"
T_LSO = 2.0  # ADR threshold for auto-detect support MA
T_FOOTHOLD = 1.856  # ADR threshold for foothold (derived from examples max)

MA_SET = [("EMA",3),("EMA",8),("EMA",21),("SMA",50),("SMA",100),("SMA",200)]

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    return h, l, c


def adr14(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def sma_at(arr, n, idx):
    if idx < n - 1: return float("nan")
    return float(np.mean(arr[idx-n+1:idx+1]))


def ema_at(arr, n, idx):
    if idx < n - 1: return float("nan")
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx+1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)


def lso_period(c, sig_idx, adr):
    sig_close = c[sig_idx]
    qualifying = []
    for kind, n in MA_SET:
        if sig_idx < n - 1: continue
        v = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
        if not np.isfinite(v): continue
        signed = (sig_close - v) / adr
        if abs(signed) <= T_LSO:
            qualifying.append((n, kind, v))
    if not qualifying:
        return None, None, None
    qualifying.sort(key=lambda x: -x[0])  # longest period
    return qualifying[0]


for setup_name in ["htf", "bf", "base"]:
    pool_path = f"{POOL_DIR}/{setup_name}_pool.json"
    with open(pool_path, encoding="utf-8") as f:
        pool = json.load(f)
    wild_clusters = [c for c in pool["clusters"] if not c.get("is_example")]
    print(f"\n=== {setup_name.upper()} — {len(wild_clusters)} wild clusters ===")

    bucket = Counter()
    foothold_vals = []
    ma_winners = Counter()
    insufficient_examples = []

    for cl in wild_clusters:
        ticker = cl["ticker"]
        sig_idx = cl["rightmost"]["bar_idx"]
        if ticker not in universe:
            bucket["missing_ticker"] += 1
            continue
        h, l, c = load(ticker)
        if sig_idx >= len(c) or sig_idx < 14:
            bucket["insufficient_history"] += 1
            continue
        adr = adr14(h, l, sig_idx)
        if not np.isfinite(adr) or adr <= 0:
            bucket["insufficient_history"] += 1
            continue
        n_lso, kind_lso, ma_val = lso_period(c, sig_idx, adr)
        if n_lso is None:
            bucket["no_support_in_range"] += 1
            continue
        ma_winners[f"{kind_lso}{n_lso}"] += 1
        sig_low = l[sig_idx]
        foothold = (sig_low - ma_val) / adr
        foothold_vals.append(foothold)
        if foothold <= T_FOOTHOLD:
            bucket["mature_ready"] += 1
        else:
            bucket["needs_more_sideways"] += 1

    total = sum(bucket.values())
    print(f"  Total wild evaluated: {total}")
    for label in ["mature_ready", "needs_more_sideways", "no_support_in_range",
                  "insufficient_history", "missing_ticker"]:
        cnt = bucket[label]
        if cnt > 0:
            pct = cnt / total * 100 if total else 0
            print(f"    {label:<26} {cnt:>4} ({pct:.1f}%)")

    # Foothold distribution among evaluable
    if foothold_vals:
        arr = np.array(foothold_vals)
        print(f"  Foothold distribution: min={arr.min():.2f} median={np.median(arr):.2f} "
              f"p75={np.percentile(arr,75):.2f} p90={np.percentile(arr,90):.2f} max={arr.max():.2f}")

    # MA distribution
    print(f"  MA winners (top 6):")
    for ma, cnt in ma_winners.most_common(6):
        print(f"    {ma:<10} {cnt:>4}")

# Overall totals
print()
print("=" * 50)
print("OVERALL ACROSS ALL 3 SETUPS")
print("=" * 50)
all_buckets = Counter()
for setup_name in ["htf", "bf", "base"]:
    pool_path = f"{POOL_DIR}/{setup_name}_pool.json"
    with open(pool_path, encoding="utf-8") as f:
        pool = json.load(f)
    wild_clusters = [c for c in pool["clusters"] if not c.get("is_example")]
    for cl in wild_clusters:
        ticker = cl["ticker"]
        sig_idx = cl["rightmost"]["bar_idx"]
        if ticker not in universe:
            all_buckets["missing_ticker"] += 1
            continue
        h, l, c = load(ticker)
        if sig_idx >= len(c) or sig_idx < 14:
            all_buckets["insufficient_history"] += 1
            continue
        adr = adr14(h, l, sig_idx)
        if not np.isfinite(adr) or adr <= 0:
            all_buckets["insufficient_history"] += 1
            continue
        n_lso, kind_lso, ma_val = lso_period(c, sig_idx, adr)
        if n_lso is None:
            all_buckets["no_support_in_range"] += 1
            continue
        sig_low = l[sig_idx]
        foothold = (sig_low - ma_val) / adr
        if foothold <= T_FOOTHOLD:
            all_buckets["mature_ready"] += 1
        else:
            all_buckets["needs_more_sideways"] += 1
total = sum(all_buckets.values())
for label in ["mature_ready", "needs_more_sideways", "no_support_in_range",
              "insufficient_history", "missing_ticker"]:
    cnt = all_buckets[label]
    pct = cnt / total * 100 if total else 0
    print(f"  {label:<26} {cnt:>4} ({pct:.1f}%)")
print(f"  TOTAL                       {total:>4}")
print()
print(f"Carve rate (mature / total) = {all_buckets['mature_ready']/total*100:.1f}%")
print(f"Reject rate (premature + no-support) = "
      f"{(all_buckets['needs_more_sideways']+all_buckets['no_support_in_range'])/total*100:.1f}%")
