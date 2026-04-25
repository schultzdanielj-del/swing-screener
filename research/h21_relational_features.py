"""H21: relational features pyramid grinder wouldn't find in its single-expression search.

Pyramid grinds single-expression boolean thresholds on pre-cached per-bar features
(including per-MA signed distance from sig_close). What pyramid WON'T find natively:
- Multi-MA relational features (pairwise gaps between MAs, not MA-to-close)
- Discrete state features (MA ordering rank, stack alignment booleans)
- Multi-bar aggregations that require iteration over a window
- Composite scalars (stack compression, dispersion)

Explores these per-setup to identify common ground among HTF/BF/BASE example setups
that pyramid's single-feature search couldn't surface.

Features computed per signal bar (6 MAs: EMA3,8,21, SMA50,200,330):
1-4.   Ordering: monotone-stacked bool, Kendall tau(period,rank), # inversions, # MAs above sig_close
5-19.  All 15 pairwise MA gaps in ADR: (MA_i - MA_j) / ADR for every pair
20-22. Stack geometry: stack compression (max-min)/ADR, mean signed distance, MAs above/below sig_close
23-28. 5-day slope of each of 6 MAs in ADR
29-31. Signal bar shape: range/ADR, close position in range, green/red bool
32-37. Support touch count: lows within 0.2 ADR of each MA in last 10 bars
38-43. Bars-above streak: consecutive bars with close >= MA back from sig for each MA
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from itertools import combinations

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 200), ("SMA", 330)]
MA_PERIODS = [3, 8, 21, 50, 200, 330]
MA_NAMES = [f"{k}{n}" for k, n in MA_SET]

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
print(f"Cache ticker count: {len(universe)}")
if len(universe) < 11200:
    raise SystemExit(f"ABORT: ticker count {len(universe)} < 11200")

with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples "
                        "WHERE setup_type IN ('htf','bf','base') "
                        "ORDER BY setup_type, ticker, entry_date").fetchall()


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy(); df["date"] = pd.to_datetime(df["date"])
    d = df["date"].dt.strftime("%Y-%m-%d").values
    return (d, df["high"].values.astype(float), df["low"].values.astype(float),
            df["close"].values.astype(float), df["open"].values.astype(float))


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")


def sma_series(arr, n, end_idx):
    """SMA at indices end_idx-k..end_idx. Returns array of SMA values."""
    out = np.full(end_idx+1, np.nan)
    for i in range(n-1, end_idx+1):
        out[i] = np.mean(arr[i-n+1:i+1])
    return out


def ema_series(arr, n, end_idx):
    out = np.full(end_idx+1, np.nan)
    alpha = 2.0/(n+1)
    e = arr[0]
    out[0] = e
    for i in range(1, end_idx+1):
        e = alpha*arr[i] + (1-alpha)*e
        if i >= n-1:
            out[i] = e
    return out


def compute_ma_values(c, end_idx):
    """Return dict of MA arrays up through end_idx."""
    result = {}
    for kind, n in MA_SET:
        if kind == "SMA":
            result[f"{kind}{n}"] = sma_series(c, n, end_idx)
        else:
            result[f"{kind}{n}"] = ema_series(c, n, end_idx)
    return result


def kendall_tau(ranks):
    """Kendall's tau between natural order (1..n) and given ranks."""
    n = len(ranks)
    concordant = discordant = 0
    for i in range(n):
        for j in range(i+1, n):
            if (ranks[i] - ranks[j]) > 0: concordant += 1
            elif (ranks[i] - ranks[j]) < 0: discordant += 1
    total = n*(n-1)/2
    return (concordant - discordant) / total if total > 0 else 0.0


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c, o = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue

    # Compute all MA series up through sig
    mas = compute_ma_values(c, i)
    ma_vals = {name: float(series[i]) if not np.isnan(series[i]) else float("nan")
               for name, series in mas.items()}

    sc = c[i]; sh = h[i]; sl = l[i]; so = o[i]
    ftr = {"setup": setup, "ticker": ticker, "entry": entry_date, "adr": adr,
           "sig_close": sc, "sig_high": sh, "sig_low": sl, "sig_open": so,
           "sig_idx": i}

    # --- Feature 1-4: ordering / rank ---
    # Collect available MAs (drop NaN)
    present = [(name, ma_vals[name]) for name in MA_NAMES if not np.isnan(ma_vals[name])]
    if len(present) >= 2:
        # sort by PRICE descending; rank 1 = highest MA, rank n = lowest
        sorted_by_price = sorted(present, key=lambda x: -x[1])
        price_rank = {name: idx+1 for idx, (name, _) in enumerate(sorted_by_price)}
        # For each present MA, its period
        periods_present = [MA_PERIODS[MA_NAMES.index(name)] for name, _ in present]
        # Ranks in order of MA period (so we can compare "period order" to "price order")
        ranks_by_period_order = [price_rank[name] for name, _ in
                                  sorted(present, key=lambda x: MA_PERIODS[MA_NAMES.index(x[0])])]
        # Monotone: are ranks strictly increasing in period order? (short MAs price highest)
        monotone = all(ranks_by_period_order[k] < ranks_by_period_order[k+1]
                       for k in range(len(ranks_by_period_order)-1))
        # Inversions: pairs out of "expected" order
        inversions = sum(1 for a, b in combinations(ranks_by_period_order, 2) if a > b)
        ktau = kendall_tau(ranks_by_period_order)
    else:
        monotone = None; inversions = None; ktau = None
    ftr["monotone_stacked"] = bool(monotone) if monotone is not None else None
    ftr["inversions"] = inversions
    ftr["kendall_tau"] = ktau
    ftr["n_ma_above_sig"] = sum(1 for name in MA_NAMES
                                 if not np.isnan(ma_vals[name]) and ma_vals[name] > sc)

    # --- Feature 5-19: pairwise MA gaps in ADR ---
    for name_i, name_j in combinations(MA_NAMES, 2):
        vi, vj = ma_vals[name_i], ma_vals[name_j]
        if np.isnan(vi) or np.isnan(vj):
            ftr[f"gap_{name_i}_{name_j}"] = float("nan")
        else:
            ftr[f"gap_{name_i}_{name_j}"] = (vi - vj) / adr

    # --- Feature 20-22: stack geometry ---
    present_vals = [v for _, v in present]
    if present_vals:
        ftr["stack_compression"] = (max(present_vals) - min(present_vals)) / adr
        ftr["mean_ma_signed_dist"] = float(np.mean([(sc - v)/adr for v in present_vals]))
        ftr["n_ma_below_sig"] = sum(1 for v in present_vals if v < sc)
    else:
        ftr["stack_compression"] = float("nan")
        ftr["mean_ma_signed_dist"] = float("nan")
        ftr["n_ma_below_sig"] = 0

    # --- Feature 23-28: 5-day MA slope in ADR ---
    for name in MA_NAMES:
        series = mas[name]
        if i < 5 or np.isnan(series[i]) or np.isnan(series[i-5]):
            ftr[f"slope5_{name}"] = float("nan")
        else:
            ftr[f"slope5_{name}"] = (series[i] - series[i-5]) / adr

    # --- Feature 29-31: sig bar shape ---
    bar_range = sh - sl
    ftr["bar_range_adr"] = bar_range / adr
    ftr["bar_close_position"] = (sc - sl) / bar_range if bar_range > 0 else 0.5
    ftr["bar_green"] = bool(sc > so)

    # --- Feature 32-37: support touch count, lows within 0.2 ADR in last 10 bars ---
    for name in MA_NAMES:
        series = mas[name]
        cnt = 0
        for k in range(max(0, i-9), i+1):
            if np.isnan(series[k]): continue
            if abs(l[k] - series[k]) / adr <= 0.2: cnt += 1
        ftr[f"touches02_{name}"] = cnt

    # --- Feature 38-43: bars-above streak (consecutive bars with close >= MA) ---
    for name in MA_NAMES:
        series = mas[name]
        streak = 0
        k = i
        while k >= 0 and not np.isnan(series[k]) and c[k] >= series[k]:
            streak += 1
            k -= 1
        ftr[f"streak_above_{name}"] = streak

    records.append(ftr)

print(f"Evaluable examples: {len(records)}")
print()


def summarize_feature(setup_records, key, categorical=False, boolean=False):
    vals = [r[key] for r in setup_records if r.get(key) is not None and not (
        isinstance(r[key], float) and np.isnan(r[key]))]
    if not vals: return None
    if boolean:
        true_count = sum(1 for v in vals if v)
        return {"n": len(vals), "true": true_count, "false": len(vals)-true_count,
                "pct_true": 100*true_count/len(vals)}
    if categorical:
        from collections import Counter
        cnt = Counter(vals)
        return {"n": len(vals), "top": cnt.most_common(5)}
    arr = np.array(vals, dtype=float)
    return {
        "n": len(arr),
        "min": float(arr.min()), "p25": float(np.percentile(arr, 25)),
        "p50": float(np.percentile(arr, 50)), "p75": float(np.percentile(arr, 75)),
        "max": float(arr.max()),
        "iqr": float(np.percentile(arr, 75) - np.percentile(arr, 25)),
    }


def print_numeric_by_setup(key, header=None):
    print(f"\n--- {header or key} ---")
    print(f"  {'setup':<6} {'n':>3} {'min':>8} {'p25':>8} {'p50':>8} {'p75':>8} {'max':>8} {'iqr':>8}")
    for setup in ("htf", "bf", "base"):
        sub = [r for r in records if r["setup"] == setup]
        s = summarize_feature(sub, key)
        if s is None: continue
        print(f"  {setup:<6} {s['n']:>3} {s['min']:>+8.3f} {s['p25']:>+8.3f} {s['p50']:>+8.3f} "
              f"{s['p75']:>+8.3f} {s['max']:>+8.3f} {s['iqr']:>8.3f}")


def print_boolean_by_setup(key, header=None):
    print(f"\n--- {header or key} ---")
    print(f"  {'setup':<6} {'n':>3} {'true':>5} {'false':>6} {'%true':>6}")
    for setup in ("htf", "bf", "base"):
        sub = [r for r in records if r["setup"] == setup]
        s = summarize_feature(sub, key, boolean=True)
        if s is None: continue
        print(f"  {setup:<6} {s['n']:>3} {s['true']:>5} {s['false']:>6} {s['pct_true']:>5.1f}%")


def print_categorical_by_setup(key, header=None):
    print(f"\n--- {header or key} ---")
    for setup in ("htf", "bf", "base"):
        sub = [r for r in records if r["setup"] == setup]
        s = summarize_feature(sub, key, categorical=True)
        if s is None: continue
        top = s["top"]
        line = ", ".join(f"{v}:{c}" for v, c in top[:5])
        print(f"  {setup:<6} n={s['n']}  top: {line}")


print("=" * 105)
print("SECTION 1 — ORDERING / RANK FEATURES (pyramid doesn't enumerate discrete orderings)")
print("=" * 105)
print_boolean_by_setup("monotone_stacked", "is MA stack strictly monotone (EMA3 > EMA8 > ... > SMA330)?")
print_numeric_by_setup("inversions", "# of inversions in MA stack (pairs out of period order)")
print_numeric_by_setup("kendall_tau", "Kendall tau between MA period and price rank (+1 = perfect, -1 = reversed)")
print_numeric_by_setup("n_ma_above_sig", "# of MAs above sig_close (how many MAs price is below)")
print_categorical_by_setup("n_ma_above_sig", "distribution of # MAs above sig_close")

print()
print("=" * 105)
print("SECTION 2 — PAIRWISE MA GAPS (not in cache — all 15 pairs)")
print("=" * 105)
for name_i, name_j in combinations(MA_NAMES, 2):
    print_numeric_by_setup(f"gap_{name_i}_{name_j}", f"({name_i} - {name_j}) / ADR")

print()
print("=" * 105)
print("SECTION 3 — STACK GEOMETRY (composite scalars)")
print("=" * 105)
print_numeric_by_setup("stack_compression", "(max MA - min MA) / ADR  [how spread the stack is]")
print_numeric_by_setup("mean_ma_signed_dist", "mean of (sig_close - MA) / ADR across 6 MAs")
print_numeric_by_setup("n_ma_below_sig", "# of MAs below sig_close")

print()
print("=" * 105)
print("SECTION 4 — MA SLOPES (5-day rate of change in ADR units)")
print("=" * 105)
for name in MA_NAMES:
    print_numeric_by_setup(f"slope5_{name}", f"5-bar slope of {name} in ADR")

print()
print("=" * 105)
print("SECTION 5 — SIGNAL BAR SHAPE")
print("=" * 105)
print_numeric_by_setup("bar_range_adr", "sig bar range / ADR (small = tight)")
print_numeric_by_setup("bar_close_position", "sig bar close position in its range (0=at low, 1=at high)")
print_boolean_by_setup("bar_green", "sig bar is green (close > open)?")

print()
print("=" * 105)
print("SECTION 6 — SUPPORT TOUCHES (# of last-10 bar lows within 0.2 ADR of each MA)")
print("=" * 105)
for name in MA_NAMES:
    print_numeric_by_setup(f"touches02_{name}", f"lows within 0.2 ADR of {name} in last 10 bars")

print()
print("=" * 105)
print("SECTION 7 — BARS-ABOVE STREAK (consecutive bars with close >= MA, back from sig)")
print("=" * 105)
for name in MA_NAMES:
    print_numeric_by_setup(f"streak_above_{name}", f"bars with close >= {name} streak")
