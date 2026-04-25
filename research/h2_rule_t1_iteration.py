"""H2 iteration: try several end-to-end anchor rules. Test = T1.1 pass rate on 113 examples.

Each rule: chart -> AVWAP value at sig (the overhead level the entry must clear).
T1.1 pass = exists k in [1,10] such that close[sig+k] > close[sig] AND close[sig+k] > AVWAP_rule.

Rules tested (pure math, no eye-picked thresholds where avoidable):
  R1. Anchor at bar (sig - N) where N = period of longest SMA within 1.5 ADR of sig (LSO).
  R2. Anchor at the most recent bar where SMA(N) bottomed in [sig - 2N, sig - 1] (LSO).
  R3. Anchor at the highest-close bar in [sig - N, sig - 1] (LSO).
  R4. Anchor at the bar where close last crossed above SMA(N) going backward.
  R5. Anchor at sig - N (fixed; very simple sanity).

Goal: find rules where T1.1 pass = 113/113.
Report wild-signal filter strength as a secondary check (more filtering on wild = more useful rule).
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

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
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0
    return dates, h, l, c, v, tp


def sma_series(arr, n):
    out = np.full_like(arr, np.nan, dtype=float)
    if n <= 0 or n > len(arr): return out
    cs = np.concatenate([[0.0], np.cumsum(arr)])
    out[n-1:] = (cs[n:] - cs[:-n+1] if False else (cs[n:] - cs[:len(arr)-n+1])) / n
    # safer simple loop
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(n-1, len(arr)):
        out[i] = float(np.mean(arr[i-n+1:i+1]))
    return out


def adr14_at(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def lso_period(c, sig_idx, adr, t=1.5):
    """Longest SMA period N in {10,21,50,200} such that (close[sig] - SMA(N)(sig)) / ADR <= t."""
    sig_close = c[sig_idx]
    candidates = []
    for n in [10, 21, 50, 200]:
        if sig_idx < n - 1: continue
        ma = float(np.mean(c[sig_idx-n+1:sig_idx+1]))
        signed = (sig_close - ma) / adr
        if abs(signed) <= t:
            candidates.append((n, ma))
    if not candidates:
        return 10  # fallback
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][0]


def avwap(tp, v, A, sig_idx):
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return float("nan")
    return float((tp[A:sig_idx+1] * seg_v).sum() / sv)


def rule_r1(c, h, l, v, tp, sig_idx, adr):
    """Anchor at sig - N where N = LSO period."""
    n = lso_period(c, sig_idx, adr)
    A = max(0, sig_idx - n)
    return A, avwap(tp, v, A, sig_idx), n


def rule_r2(c, h, l, v, tp, sig_idx, adr):
    """Anchor at the bar in [sig - 2N, sig - 1] where SMA(N) bottomed."""
    n = lso_period(c, sig_idx, adr)
    sma = sma_series(c, n)
    lo_window_start = max(n - 1, sig_idx - 2*n)
    if lo_window_start >= sig_idx: return None, float("nan"), n
    sma_window = sma[lo_window_start:sig_idx]
    A = lo_window_start + int(np.argmin(sma_window))
    return A, avwap(tp, v, A, sig_idx), n


def rule_r3(c, h, l, v, tp, sig_idx, adr):
    """Anchor at the highest-close bar in [sig - N, sig - 1]."""
    n = lso_period(c, sig_idx, adr)
    window_start = max(0, sig_idx - n)
    if window_start >= sig_idx: return None, float("nan"), n
    cw = c[window_start:sig_idx]
    A = window_start + int(np.argmax(cw))
    return A, avwap(tp, v, A, sig_idx), n


def rule_r4(c, h, l, v, tp, sig_idx, adr):
    """Anchor at the most recent bar (going backward from sig-1) where close < SMA(N) at that bar.
    The 'last bar below MA before the rise.'"""
    n = lso_period(c, sig_idx, adr)
    sma = sma_series(c, n)
    A = None
    for i in range(sig_idx - 1, max(n - 1, 0), -1):
        if not np.isfinite(sma[i]): continue
        if c[i] < sma[i]:
            A = i
            break
    if A is None:
        A = max(0, sig_idx - n)
    return A, avwap(tp, v, A, sig_idx), n


def rule_r5(c, h, l, v, tp, sig_idx, adr):
    """Sanity: anchor at sig - 21 fixed."""
    A = max(0, sig_idx - 21)
    return A, avwap(tp, v, A, sig_idx), 21


RULES = [
    ("R1_anchor_sig_minus_N", rule_r1),
    ("R2_sma_bottom",         rule_r2),
    ("R3_highest_close_in_N", rule_r3),
    ("R4_last_below_sma",     rule_r4),
    ("R5_fixed_21",           rule_r5),
]


def evaluate_t1(c, sig_idx, avwap_val):
    """T1: any k in [1,10] with close[sig+k] > close[sig] AND close[sig+k] > avwap_val."""
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx: return False
    fwd = c[sig_idx+1:end+1]
    pass_close = fwd > sig_close
    pass_avwap = fwd > avwap_val
    return bool((pass_close & pass_avwap).any())


# Run
results = {name: {"pass": 0, "fail": 0, "fail_examples": [], "anchor_dates": [], "avwap_vals": []} for name, _ in RULES}

for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 200: continue
    adr = adr14_at(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: continue

    for rname, rfn in RULES:
        A, av, n = rfn(c, h, l, v, tp, sig_idx, adr)
        if not np.isfinite(av):
            results[rname]["fail"] += 1
            results[rname]["fail_examples"].append((setup, ticker, entry_date, "NaN AVWAP"))
            continue
        ok = evaluate_t1(c, sig_idx, av)
        if ok:
            results[rname]["pass"] += 1
        else:
            results[rname]["fail"] += 1
            sig_close = c[sig_idx]
            max_fwd = float(c[sig_idx+1:min(sig_idx+11, len(c))].max())
            results[rname]["fail_examples"].append(
                (setup, ticker, entry_date, f"sig_cl={sig_close:.2f} avw={av:.2f} max_fwd={max_fwd:.2f} N={n}"))

print("=== T1.1 PASS RATE PER RULE (113 examples target) ===")
for name, _ in RULES:
    r = results[name]
    total = r["pass"] + r["fail"]
    print(f"  {name:<30} pass={r['pass']:>4d}/{total:<4d} fail={r['fail']:>3d}")

print()
print("=== FAILURES PER RULE ===")
for name, _ in RULES:
    r = results[name]
    if r["fail"] == 0: continue
    print(f"\n  {name}:")
    for setup, ticker, entry_date, info in r["fail_examples"]:
        print(f"    {setup:<6} {ticker:<8} {entry_date:<12}  {info}")
