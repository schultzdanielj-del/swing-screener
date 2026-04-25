"""Diagnostic on QUBT 2024-11-20 — why corridor_width = 3.75 ADR."""
import pickle
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
with open(CACHE, "rb") as f:
    universe = pickle.load(f)

df = universe["QUBT"].copy()
df["date"] = pd.to_datetime(df["date"])
df["date_s"] = df["date"].dt.strftime("%Y-%m-%d")

entry_idx = int(np.where(df["date_s"].values == "2024-11-20")[0][0])
sig_idx = entry_idx - 1

print(f"entry_date = 2024-11-20  entry_idx = {entry_idx}  sig_idx = {sig_idx}  sig_date = {df['date_s'].iloc[sig_idx]}")
print()

# Show bars from sig-25 to entry+2
lo = max(0, sig_idx - 25)
hi = min(len(df), sig_idx + 3)
print("=== bars around signal ===")
print(f"{'idx':>4} {'date':<11} {'open':>9} {'high':>9} {'low':>9} {'close':>9} {'volume':>14}")
for i in range(lo, hi):
    tag = ""
    if i == sig_idx: tag = "  <- SIG"
    if i == entry_idx: tag = "  <- ENTRY"
    print(f"{i:>4} {df['date_s'].iloc[i]:<11} {df['open'].iloc[i]:>9.4f} {df['high'].iloc[i]:>9.4f} "
          f"{df['low'].iloc[i]:>9.4f} {df['close'].iloc[i]:>9.4f} {int(df['volume'].iloc[i]):>14}{tag}")
print()

# ADR
h = df["high"].values.astype(float)
l = df["low"].values.astype(float)
c = df["close"].values.astype(float)
v = df["volume"].values.astype(float)
tp = (h + l + c) / 3.0

adr = float(np.mean(h[sig_idx-13:sig_idx+1] - l[sig_idx-13:sig_idx+1]))
print(f"sig_close = {c[sig_idx]:.4f}")
print(f"ADR(14) = mean high-low over last 14 bars = {adr:.4f}")
print(f"  per-bar H-L contributions:")
for i in range(sig_idx-13, sig_idx+1):
    print(f"    {df['date_s'].iloc[i]} H={h[i]:.4f} L={l[i]:.4f} H-L={h[i]-l[i]:.4f}")
print()

# EMA21 at sig
def ema_at(arr, n, idx):
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx+1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)

ema21 = ema_at(c, 21, sig_idx)
print(f"EMA21 at sig = {ema21:.4f}")
print(f"  (sig_close - EMA21) / ADR = {(c[sig_idx] - ema21)/adr:+.3f}")
print()

# Check all MA candidates
def sma_at(arr, n, idx):
    return float(np.mean(arr[idx-n+1:idx+1]))

print("=== MA candidates at sig ===")
for kind, n in [("EMA",3),("EMA",8),("EMA",21),("SMA",50),("SMA",100),("SMA",200)]:
    if sig_idx < n-1:
        print(f"  {kind}{n}: not enough data")
        continue
    val = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
    signed = (c[sig_idx] - val) / adr
    qual = "QUAL" if abs(signed) <= 2.0 else "out"
    print(f"  {kind}{n}: val={val:.4f}  signed={signed:+.3f} ADR  [{qual}]")
print()

# AVWAP scan over EMA21 lookback (21 bars back from sig)
print("=== AVWAP(A..sig) for A in [sig-21, sig-1] ===")
cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
cum_v = np.concatenate([[0.0], np.cumsum(v)])
print(f"{'A_idx':>5} {'A_date':<12} {'bars':>4} {'avwap':>10} {'dist_adr':>10}")
A_start = max(0, sig_idx - 21)
for A in range(A_start, sig_idx):
    total_tpv = cum_tpv[sig_idx+1] - cum_tpv[A]
    total_v = cum_v[sig_idx+1] - cum_v[A]
    av = total_tpv/total_v if total_v > 0 else float("nan")
    bars = sig_idx - A + 1
    dist = (av - c[sig_idx]) / adr
    print(f"{A:>5} {df['date_s'].iloc[A]:<12} {bars:>4} {av:>10.4f} {dist:>+10.3f}")
