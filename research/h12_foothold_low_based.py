"""H12: foothold criterion using sig bar LOW instead of close.

For each example:
  - Auto-detect support MA (longest in {SMA50/100/200, EMA3/8/21} within t_lso=2.0 ADR of sig_close)
  - Compute foothold = (sig_low - MA(sig)) / ADR
  - Smaller = closer = stronger foothold
  - Find max value across all examples (= T_foothold to pass T1.1)

Output:
  - Distribution of foothold values
  - Worst (largest) foothold among examples
  - That value = derived T_foothold threshold
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

MA_SET = [("EMA",3),("EMA",8),("EMA",21),("SMA",50),("SMA",100),("SMA",200)]

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
    return dates, h, l, c, v


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


def lso_period(c, sig_idx, adr, t_lso=2.0):
    sig_close = c[sig_idx]
    qualifying = []
    for kind, n in MA_SET:
        if sig_idx < n - 1: continue
        v = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
        if not np.isfinite(v): continue
        signed = (sig_close - v) / adr
        if abs(signed) <= t_lso:
            qualifying.append((n, kind, v, signed))
    if not qualifying:
        return None, None, None
    qualifying.sort(key=lambda x: (-x[0], abs(x[3])))
    return qualifying[0][0], qualifying[0][1], qualifying[0][2]


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    dates, h, l, c, v = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 14: continue
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: continue
    n, kind, ma_val = lso_period(c, sig_idx, adr)
    if n is None: continue
    sig_low = l[sig_idx]
    foothold_low = (sig_low - ma_val) / adr  # positive => low above MA (good foothold)
    sig_close = c[sig_idx]
    foothold_close = (sig_close - ma_val) / adr
    sig_range = (h[sig_idx] - l[sig_idx]) / adr
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "sig_low": sig_low,
        "adr": adr, "ma_kind": kind, "ma_period": n, "ma_val": ma_val,
        "foothold_low": foothold_low, "foothold_close": foothold_close,
        "sig_range_adr": sig_range,
    })

print(f"Total records: {len(records)}")
print()

# Distribution
print("=== FOOTHOLD (sig_low - MA) / ADR distribution ===")
foothold_arr = np.array([r["foothold_low"] for r in records])
print(f"  min={foothold_arr.min():.3f}  p25={np.percentile(foothold_arr,25):.3f}  "
      f"median={np.percentile(foothold_arr,50):.3f}  p75={np.percentile(foothold_arr,75):.3f}  "
      f"p90={np.percentile(foothold_arr,90):.3f}  max={foothold_arr.max():.3f}")
print()

# By setup
print("=== FOOTHOLD distribution by setup ===")
for s in ["htf","bf","base"]:
    arr = np.array([r["foothold_low"] for r in records if r["setup"] == s])
    if len(arr) == 0: continue
    print(f"  {s} (n={len(arr)}): min={arr.min():.3f} median={np.percentile(arr,50):.3f} "
          f"max={arr.max():.3f}")
print()

# Worst-foothold examples
print("=== TOP 10 WORST FOOTHOLD (largest sig_low - MA in ADR) ===")
sorted_records = sorted(records, key=lambda r: -r["foothold_low"])
print(f"{'setup':<6} {'ticker':<8} {'entry':<12} {'MA':<10} {'foothold_low':>14} {'foothold_close':>16} {'sig_range_ADR':>14}")
for r in sorted_records[:10]:
    ma_label = f"{r['ma_kind']}{r['ma_period']}"
    print(f"{r['setup']:<6} {r['ticker']:<8} {r['entry_date']:<12} {ma_label:<10} "
          f"{r['foothold_low']:>14.3f} {r['foothold_close']:>16.3f} {r['sig_range_adr']:>14.3f}")
print()

print("=== T_foothold derivation ===")
print(f"Min T_foothold to pass all 112 examples (using sig_low) = {foothold_arr.max():.3f} ADR")
print(f"vs using sig_close: {max(r['foothold_close'] for r in records):.3f} ADR")
print()

# Sanity check on DRN: should be ~1/2 ADR per Dan
drn = [r for r in records if r["ticker"] == "DRN" and r["entry_date"] == "2024-07-11"]
if drn:
    r = drn[0]
    print(f"DRN sanity: sig_low={r['sig_low']:.3f}, MA={r['ma_kind']}{r['ma_period']}={r['ma_val']:.3f}, "
          f"ADR={r['adr']:.3f}, foothold_low={r['foothold_low']:.3f} ADR, sig_range={r['sig_range_adr']:.3f} ADR")
