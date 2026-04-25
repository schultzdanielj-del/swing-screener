"""H2 iteration 2: explore multiple MA-foothold criteria.

Criteria tested per chart:
  C1. raw_adr: |close - MA(sig)| / ADR. Closest wins. (baseline, known to pick fastest MA)
  C2. zscore: (this chart's signed_adr - population mean for this MA) / population std.
     Smallest z-score wins (most unusually close vs typical).
  C3. longest_within_t: longest-period MA with distance below threshold t (sweep t).
  C4. ratio_to_next: signed_adr_MA(N) / signed_adr_MA(N/2). When ratio is small, the longer MA is
     barely further than the shorter one - the longer MA is the active support.

Want to see which criterion produces results aligned with Dan's stated mapping
(HTF→fast/EMA10, BF→EMA21, BASE→SMA50).
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

with sqlite3.connect(DB) as conn:
    rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
    ).fetchall()

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    return dates, h, l, c


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


def adr14(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


# Order matters for "longer MA" - sorted by period
MA_DEFS = [
    ("SMA10",  10,  lambda c, idx: sma_at(c, 10, idx)),
    ("EMA10",  10,  lambda c, idx: ema_at(c, 10, idx)),
    ("SMA21",  21,  lambda c, idx: sma_at(c, 21, idx)),
    ("EMA21",  21,  lambda c, idx: ema_at(c, 21, idx)),
    ("SMA50",  50,  lambda c, idx: sma_at(c, 50, idx)),
    ("EMA50",  50,  lambda c, idx: ema_at(c, 50, idx)),
    ("SMA200", 200, lambda c, idx: sma_at(c, 200, idx)),
    ("EMA200", 200, lambda c, idx: ema_at(c, 200, idx)),
]


# Phase 1: build per-example per-MA distances
records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe:
        continue
    dates, h, l, c = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 200:
        continue
    sig_close = float(c[sig_idx])
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0:
        continue

    ma_data = {}
    for name, period, fn in MA_DEFS:
        v = fn(c, sig_idx)
        if not np.isfinite(v):
            ma_data[name] = None
            continue
        signed = (sig_close - v) / adr  # positive => MA below sig
        ma_data[name] = (v, signed, period)

    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "adr": adr,
        "ma_data": ma_data,
    })

print(f"Records: {len(records)}")

# Phase 2: population stats per MA
print()
print("=== POPULATION STATS — signed_adr distance per MA across all examples ===")
print(f"{'MA':<8} {'mean':>8} {'std':>8} {'p25':>8} {'p50':>8} {'p75':>8} {'p90':>8}")
ma_pop = {}
for name, _, _ in MA_DEFS:
    arr = np.array([r["ma_data"][name][1] for r in records if r["ma_data"][name] is not None])
    if len(arr) == 0:
        continue
    ma_pop[name] = {"mean": float(arr.mean()), "std": float(arr.std()),
                    "p25": float(np.percentile(arr, 25)), "p50": float(np.percentile(arr, 50)),
                    "p75": float(np.percentile(arr, 75)), "p90": float(np.percentile(arr, 90))}
    s = ma_pop[name]
    print(f"{name:<8} {s['mean']:>8.2f} {s['std']:>8.2f} {s['p25']:>8.2f} {s['p50']:>8.2f} {s['p75']:>8.2f} {s['p90']:>8.2f}")
print()


def winner_zscore(r):
    """Pick MA with smallest z-score = most unusually close vs population."""
    best = None
    for name, _, _ in MA_DEFS:
        if r["ma_data"][name] is None:
            continue
        signed = r["ma_data"][name][1]
        if name not in ma_pop:
            continue
        z = (signed - ma_pop[name]["mean"]) / ma_pop[name]["std"]
        if best is None or z < best[1]:
            best = (name, z)
    return best


def winner_longest_within(r, t):
    """Longest-period MA with |signed_adr| <= t."""
    candidates = []
    for name, period, _ in MA_DEFS:
        if r["ma_data"][name] is None:
            continue
        signed = r["ma_data"][name][1]
        if abs(signed) <= t:
            candidates.append((period, name, signed))
    if not candidates:
        return None
    # Pick the longest period
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return (candidates[0][1], candidates[0][2])


def winner_ratio(r):
    """Find longer MAs that are barely further than shorter MAs (small ratio)."""
    pairs = [
        ("SMA10",  "SMA21"),
        ("SMA21",  "SMA50"),
        ("SMA50",  "SMA200"),
        ("EMA10",  "EMA21"),
        ("EMA21",  "EMA50"),
        ("EMA50",  "EMA200"),
    ]
    # Among (short, long) pairs: if (long_dist - short_dist) is small in ADR, the long MA
    # adds little distance for much more period - the longer MA is "in play."
    best = None
    for s, l in pairs:
        if r["ma_data"][s] is None or r["ma_data"][l] is None:
            continue
        s_signed = r["ma_data"][s][1]
        l_signed = r["ma_data"][l][1]
        if l_signed < 0:
            continue  # MA above sig - not a foothold
        diff = l_signed - s_signed  # how much further is the longer MA
        if best is None or diff < best[1]:
            best = (l, diff)
    return best


# Run criteria across all examples
print("=== CRITERION 2 (Z-SCORE — most unusually close MA) — per-setup distribution ===")
by_setup = {"htf": [], "bf": [], "base": []}
for r in records:
    w = winner_zscore(r)
    by_setup[r["setup"]].append(w[0] if w else None)
for setup in ["htf", "bf", "base"]:
    items = by_setup.get(setup, [])
    n = len(items)
    print(f"\n  {setup} (n={n}):")
    for ma, cnt in Counter(items).most_common():
        print(f"    {str(ma):<10} {cnt:>3} ({cnt/n*100:.1f}%)")
print()

print("=== CRITERION 3 (LONGEST MA WITHIN t ADR) — varying t ===")
for t in [0.5, 1.0, 1.5, 2.0, 3.0]:
    by_setup = {"htf": [], "bf": [], "base": []}
    for r in records:
        w = winner_longest_within(r, t)
        by_setup[r["setup"]].append(w[0] if w else "NONE")
    print(f"\n  t={t} ADR:")
    for setup in ["htf", "bf", "base"]:
        items = by_setup.get(setup, [])
        n = len(items)
        c = Counter(items)
        top3 = c.most_common(3)
        summary = ", ".join(f"{m}={cnt}({cnt/n*100:.0f}%)" for m, cnt in top3)
        print(f"    {setup} (n={n}):  {summary}")
print()

print("=== CRITERION 4 (RATIO — longer MA barely further than shorter) — per-setup distribution ===")
by_setup = {"htf": [], "bf": [], "base": []}
for r in records:
    w = winner_ratio(r)
    by_setup[r["setup"]].append(w[0] if w else "NONE")
for setup in ["htf", "bf", "base"]:
    items = by_setup.get(setup, [])
    n = len(items)
    print(f"\n  {setup} (n={n}):")
    for ma, cnt in Counter(items).most_common():
        print(f"    {str(ma):<10} {cnt:>3} ({cnt/n*100:.1f}%)")
