"""H14: Distance in ADR between resistance AVWAP and support MA.

For each signal (examples + wild):
  - Detect support MA per the locked rule (longest in restricted set within t=2.0 ADR)
  - Compute resistance AVWAP per locked rule (argmax AVWAP in MA-period window)
  - gap = (resistance_AVWAP - MA_support) / ADR
  - Compare distributions: examples vs wild

Hypothesis: examples have TIGHTER gaps (compression complete, "12th round of boxing match"),
wild has WIDER gaps ("needs more sideways" — premature signals where compression hasn't finished).

If true, gap-based filter could discriminate beyond what foothold alone does.
"""
import json
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
POOL_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier/research/classifier_pool"

MA_SET = [("EMA",3),("EMA",8),("EMA",21),("SMA",50),("SMA",100),("SMA",200)]
T_LSO = 2.0
T_FOOTHOLD = 1.856

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
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0
    return h, l, c, v, tp


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
    if not qualifying: return None, None, None
    qualifying.sort(key=lambda x: (-x[0], abs((c[sig_idx] - x[2])/adr)))
    return qualifying[0]


def avwap_argmax_in_window(tp, v, sig_idx, n_window):
    A_start = max(0, sig_idx - n_window)
    A_end = sig_idx
    if A_start >= A_end: return None, float("nan")
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(A_start, A_end)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)
    best_local = int(np.argmax(avwaps))
    return int(A_range[best_local]), float(avwaps[best_local])


def compute_signal(ticker, sig_idx):
    """Return dict of: support MA, resistance AVWAP, gap_adr (resistance - support in ADR)."""
    if ticker not in universe: return None
    h, l, c, v, tp = load(ticker)
    if sig_idx < 14 or sig_idx >= len(c): return None
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: return None
    n_lso, kind_lso, ma_val = lso_period(c, sig_idx, adr)
    if n_lso is None: return None
    sig_low = l[sig_idx]
    foothold = (sig_low - ma_val) / adr
    if abs(foothold) > T_FOOTHOLD: return {"foothold_fail": True}
    A, av = avwap_argmax_in_window(tp, v, sig_idx, n_lso)
    if A is None or not np.isfinite(av): return None
    gap_adr = (av - ma_val) / adr
    return {
        "ma_kind": kind_lso, "ma_period": n_lso, "ma_val": ma_val,
        "anchor_A": A, "avwap": av,
        "foothold": foothold, "gap_adr": gap_adr,
    }


# Pull example sigs
with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples WHERE setup_type IN ('htf','bf','base') ORDER BY ticker, entry_date").fetchall()

example_data = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    h, l, c, v, tp = load(ticker)
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    sig_idx = int(hits[0]) - 1
    r = compute_signal(ticker, sig_idx)
    if r is None or r.get("foothold_fail"): continue
    r["setup"] = setup; r["ticker"] = ticker; r["entry_date"] = entry_date; r["is_example"] = True
    example_data.append(r)

# Pull wild sigs
wild_data = []
for setup_name in ["htf", "bf", "base"]:
    with open(f"{POOL_DIR}/{setup_name}_pool.json", encoding="utf-8") as f:
        pool = json.load(f)
    for cl in pool["clusters"]:
        if cl.get("is_example"): continue
        ticker = cl["ticker"]
        sig_idx = cl["rightmost"]["bar_idx"]
        r = compute_signal(ticker, sig_idx)
        if r is None or r.get("foothold_fail"): continue
        r["setup"] = setup_name; r["ticker"] = ticker; r["is_example"] = False
        wild_data.append(r)

print(f"Examples evaluated: {len(example_data)}")
print(f"Wild evaluated: {len(wild_data)}")
print()

# Distributions overall
print("=== GAP_ADR (resistance_AVWAP - support_MA) / ADR — overall ===")
ex_gaps = np.array([r["gap_adr"] for r in example_data])
wd_gaps = np.array([r["gap_adr"] for r in wild_data])
print(f"  Examples: n={len(ex_gaps)}, min={ex_gaps.min():.2f}, p10={np.percentile(ex_gaps,10):.2f}, "
      f"p25={np.percentile(ex_gaps,25):.2f}, median={np.percentile(ex_gaps,50):.2f}, "
      f"p75={np.percentile(ex_gaps,75):.2f}, p90={np.percentile(ex_gaps,90):.2f}, max={ex_gaps.max():.2f}")
print(f"  Wild:     n={len(wd_gaps)}, min={wd_gaps.min():.2f}, p10={np.percentile(wd_gaps,10):.2f}, "
      f"p25={np.percentile(wd_gaps,25):.2f}, median={np.percentile(wd_gaps,50):.2f}, "
      f"p75={np.percentile(wd_gaps,75):.2f}, p90={np.percentile(wd_gaps,90):.2f}, max={wd_gaps.max():.2f}")
print()

# Per setup
print("=== GAP_ADR by setup ===")
for s in ["htf","bf","base"]:
    ex_s = np.array([r["gap_adr"] for r in example_data if r["setup"] == s])
    wd_s = np.array([r["gap_adr"] for r in wild_data if r["setup"] == s])
    if len(ex_s) > 0 and len(wd_s) > 0:
        print(f"\n  {s}:")
        print(f"    Examples (n={len(ex_s)}): median={np.percentile(ex_s,50):.2f}  p75={np.percentile(ex_s,75):.2f}  max={ex_s.max():.2f}")
        print(f"    Wild     (n={len(wd_s)}): median={np.percentile(wd_s,50):.2f}  p75={np.percentile(wd_s,75):.2f}  max={wd_s.max():.2f}")

# Discrimination: at the worst-example threshold (T_GAP), how many wild are filtered?
print()
print("=== If we add T_GAP filter (gap_adr <= T) — example survival vs wild rejection ===")
T_gap = float(ex_gaps.max())
wild_pass = int((wd_gaps <= T_gap).sum())
wild_fail = int((wd_gaps > T_gap).sum())
print(f"  T_GAP set to worst-example gap = {T_gap:.3f} ADR")
print(f"  Examples passing: {len(example_data)}/{len(example_data)} (100% by construction)")
print(f"  Wild passing: {wild_pass}/{len(wild_data)} ({wild_pass/len(wild_data)*100:.1f}%)")
print(f"  Wild rejected: {wild_fail}/{len(wild_data)} ({wild_fail/len(wild_data)*100:.1f}%)")
print()

print("=== If we tighten T_GAP to example p75 — additional rejection ===")
T_gap_p75 = float(np.percentile(ex_gaps, 75))
ex_pass_p75 = int((ex_gaps <= T_gap_p75).sum())
wild_pass_p75 = int((wd_gaps <= T_gap_p75).sum())
print(f"  T_GAP set to example p75 = {T_gap_p75:.3f} ADR")
print(f"  Examples passing: {ex_pass_p75}/{len(example_data)} ({ex_pass_p75/len(example_data)*100:.1f}%) - would BREAK T1.1 if used as filter")
print(f"  Wild passing: {wild_pass_p75}/{len(wild_data)} ({wild_pass_p75/len(wild_data)*100:.1f}%)")
