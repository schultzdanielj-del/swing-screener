"""H1 test: anchor = A minimizing |AVWAP(A..sig) - MA(sig)| for some MA.

For each of 113 breakout examples:
  - Compute SMA10/20/50, EMA10/20/50 at sig.
  - For each MA: find A* in [0, sig-1] minimizing |AVWAP(A..sig) - MA(sig)|.
  - Report A*, AVWAP(A*..sig), MA(sig), |AVWAP - MA| in ADR units.
  - For the 8 known failures, compare A* to Dan's pick.

Selects the MA whose A* matches Dan's pick anchor most consistently across the 8.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    ("AR",   "2020-12-17"): "2020-12-11",
    ("BB",   "2024-12-20"): "2024-12-17",
    ("DRN",  "2024-07-11"): "2023-12-08",  # plateau midpoint
    ("HTT",  "2021-01-12"): "2020-06-19",
    ("LMND", "2024-11-05"): "2024-11-01",
    ("LMND", "2024-11-06"): "2024-11-01",
    ("PTON", "2024-10-14"): "2024-09-20",
    ("REAL", "2024-11-13"): "2024-11-06",
}

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
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0
    return dates, o, h, l, c, v, tp


def adr14(h, l, sig_idx):
    if sig_idx < 14:
        return float("nan")
    rng = h[sig_idx-13:sig_idx+1] - l[sig_idx-13:sig_idx+1]
    return float(np.mean(rng))


def sma(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    return float(np.mean(arr[idx-n+1:idx+1]))


def ema(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx+1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)


def avwap_curve(tp, v, sig_idx):
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(0, sig_idx)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, np.nan)
    return A_range, avwaps


def find_argmin_anchor(avwaps, target):
    diff = np.abs(avwaps - target)
    return int(np.nanargmin(diff))


MA_DEFS = [
    ("SMA10", lambda c, h, l, idx: sma(c, 10, idx)),
    ("SMA20", lambda c, h, l, idx: sma(c, 20, idx)),
    ("SMA50", lambda c, h, l, idx: sma(c, 50, idx)),
    ("EMA10", lambda c, h, l, idx: ema(c, 10, idx)),
    ("EMA20", lambda c, h, l, idx: ema(c, 20, idx)),
    ("EMA50", lambda c, h, l, idx: ema(c, 50, idx)),
]


# Phase 1: per-example results
results = []
for setup, ticker, entry_date in rows:
    if ticker not in universe:
        continue
    dates, o, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 50:
        continue

    sig_close = float(c[sig_idx])
    adr = adr14(h, l, sig_idx)
    A_range, avwaps = avwap_curve(tp, v, sig_idx)

    # Argmax baseline
    argmax_local = int(np.nanargmax(avwaps))
    argmax_A = int(A_range[argmax_local])

    # For each MA: find A* matching MA at sig
    per_ma = {}
    for ma_name, ma_fn in MA_DEFS:
        ma_val = ma_fn(c, h, l, sig_idx)
        if not np.isfinite(ma_val):
            per_ma[ma_name] = (None, None, None, None)
            continue
        astar = int(A_range[find_argmin_anchor(avwaps, ma_val)])
        avwap_at_astar = float(avwaps[astar])
        gap_adr = abs(avwap_at_astar - ma_val) / adr if (adr and adr > 0) else float("nan")
        bars_back = sig_idx - astar
        per_ma[ma_name] = (astar, avwap_at_astar, ma_val, gap_adr, bars_back, dates[astar])

    record = {
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "adr": adr,
        "argmax_A": argmax_A, "argmax_date": dates[argmax_A],
        "per_ma": per_ma,
    }
    results.append(record)

print(f"Evaluated: {len(results)} examples")
print()

# Phase 2: For 8 failures, compare each MA's A* to Dan's pick
print("=" * 110)
print("DAN PICK MATCH RATE PER MA (8 failures)")
print("=" * 110)
ma_match_counts = {ma_name: 0 for ma_name, _ in MA_DEFS}
ma_close_counts = {ma_name: 0 for ma_name, _ in MA_DEFS}  # within 5 bars
ma_avg_bars_off = {ma_name: [] for ma_name, _ in MA_DEFS}
detail_rows = []
for r in results:
    key = (r["ticker"], r["entry_date"])
    if key not in DAN_PICKS:
        continue
    dan_anchor = DAN_PICKS[key]
    dates, _, _, _, _, _, _ = load(r["ticker"])
    dan_a_idx = np.where(dates == dan_anchor)[0]
    if len(dan_a_idx) == 0:
        continue
    dan_A = int(dan_a_idx[0])
    detail_rows.append((r, dan_A, dan_anchor))
    for ma_name, _ in MA_DEFS:
        astar = r["per_ma"][ma_name][0]
        if astar is None:
            continue
        bars_off = abs(astar - dan_A)
        ma_avg_bars_off[ma_name].append(bars_off)
        if astar == dan_A:
            ma_match_counts[ma_name] += 1
        if bars_off <= 5:
            ma_close_counts[ma_name] += 1

print(f"{'MA':<8} {'exact match':>12} {'within 5 bars':>14} {'mean bars off':>14} {'median bars off':>16}")
for ma_name, _ in MA_DEFS:
    bars_off_arr = np.array(ma_avg_bars_off[ma_name])
    mean_off = float(bars_off_arr.mean()) if len(bars_off_arr) else float("nan")
    med_off = float(np.median(bars_off_arr)) if len(bars_off_arr) else float("nan")
    print(f"{ma_name:<8} {ma_match_counts[ma_name]:>12d} {ma_close_counts[ma_name]:>14d} {mean_off:>14.1f} {med_off:>16.1f}")
print()

# Phase 3: Per-failure detail
print("=" * 110)
print("PER-FAILURE DETAIL")
print("=" * 110)
for r, dan_A, dan_anchor in detail_rows:
    sig = r["sig_idx"]
    print(f"\n{r['setup']:<6} {r['ticker']:<6} entry={r['entry_date']}  sig_idx={sig}  sig_close={r['sig_close']:.2f}  ADR={r['adr']:.3f}")
    print(f"   Dan anchor: {dan_anchor} (A={dan_A}, {sig - dan_A} bars back)")
    for ma_name, _ in MA_DEFS:
        tup = r["per_ma"][ma_name]
        if tup[0] is None:
            print(f"   {ma_name}: N/A")
            continue
        astar, avw, ma_v, gap, bars_back, astar_date = tup
        bars_off = astar - dan_A
        match = "**MATCH**" if astar == dan_A else f"{bars_off:+d} bars from Dan"
        print(f"   {ma_name}: A*={astar_date} ({bars_back}b back), AVWAP={avw:.2f}, MA={ma_v:.2f}, gap={gap:.3f} ADR  [{match}]")

# Phase 4: Average gap at sig in ADR — distribution across all 113 (vs across 8 failures)
print()
print("=" * 110)
print("|AVWAP - MA| / ADR  AT SIG (gap), distribution by MA across all examples")
print("=" * 110)
print(f"{'MA':<8} {'median gap':>12} {'mean gap':>12} {'p90 gap':>10} {'finite N':>10}")
for ma_name, _ in MA_DEFS:
    gaps = []
    for r in results:
        tup = r["per_ma"][ma_name]
        if tup[0] is None:
            continue
        if np.isfinite(tup[3]):
            gaps.append(tup[3])
    if not gaps:
        continue
    arr = np.array(gaps)
    print(f"{ma_name:<8} {float(np.median(arr)):>12.3f} {float(arr.mean()):>12.3f} {float(np.percentile(arr, 90)):>10.3f} {len(arr):>10d}")
