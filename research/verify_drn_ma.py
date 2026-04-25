"""Verify DRN's SMA100 vs SMA200 distance computation. Check which MA the consolidation is riding.

If candles are 'riding' SMA200 (lows touching it) but SMA100 is the auto-detected closest within
1.5 ADR, then either:
  a. The 1.5 ADR threshold is too restrictive,
  b. The 'closest in ADR' criterion is wrong (need 'riding' criterion),
  c. There's a bug in MA computation.
"""
import pickle
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"

with open(CACHE, "rb") as f:
    universe = pickle.load(f)

df = universe["DRN"].copy()
if not pd.api.types.is_datetime64_any_dtype(df["date"]):
    df["date"] = pd.to_datetime(df["date"])
dates = df["date"].dt.strftime("%Y-%m-%d").values
h = df["high"].values.astype(float)
l = df["low"].values.astype(float)
c = df["close"].values.astype(float)

entry_date = "2024-07-11"
entry_idx = int(np.where(dates == entry_date)[0][0])
sig_idx = entry_idx - 1
sig_close = c[sig_idx]
sig_date = dates[sig_idx]
print(f"DRN entry={entry_date}, sig={sig_date}, sig_idx={sig_idx}, sig_close={sig_close:.4f}")

# ADR(14)
adr = float(np.mean(h[sig_idx-13:sig_idx+1] - l[sig_idx-13:sig_idx+1]))
print(f"ADR(14): {adr:.4f}")
print()

# All restricted MAs
print("=== MA values at sig and ADR distances ===")
for kind, n in [("SMA", 50), ("SMA", 100), ("SMA", 200), ("EMA", 3), ("EMA", 8), ("EMA", 21)]:
    if kind == "SMA":
        if sig_idx < n - 1:
            print(f"  {kind}{n}: insufficient history")
            continue
        ma = float(np.mean(c[sig_idx-n+1:sig_idx+1]))
    else:
        alpha = 2.0 / (n + 1)
        e = c[0]
        for i in range(1, sig_idx+1):
            e = alpha * c[i] + (1 - alpha) * e
        ma = e
    signed = (sig_close - ma) / adr
    print(f"  {kind}{n:>3}: value={ma:.4f}  (close - MA)/ADR = {signed:+.3f}")

print()
print("=== Last 60 bars before sig (consolidation period) ===")
print(f"{'idx':>5} {'date':<12} {'low':>8} {'close':>8} {'high':>8} | {'SMA100':>9} {'SMA200':>9} | {'low-SMA100':>11} {'low-SMA200':>11}")

# Compute SMA100 and SMA200 series
sma100 = np.full_like(c, np.nan)
sma200 = np.full_like(c, np.nan)
for i in range(99, len(c)):
    sma100[i] = float(np.mean(c[i-99:i+1]))
for i in range(199, len(c)):
    sma200[i] = float(np.mean(c[i-199:i+1]))

start = max(0, sig_idx - 60)
for i in range(start, sig_idx + 1):
    s100 = sma100[i] if np.isfinite(sma100[i]) else float('nan')
    s200 = sma200[i] if np.isfinite(sma200[i]) else float('nan')
    print(f"{i:>5} {dates[i]:<12} {l[i]:>8.3f} {c[i]:>8.3f} {h[i]:>8.3f} | {s100:>9.3f} {s200:>9.3f} | "
          f"{l[i]-s100:>+11.3f} {l[i]-s200:>+11.3f}")

print()
print(f"Dan's anchor: 2023-12-08 (idx={int(np.where(dates=='2023-12-08')[0][0])}, "
      f"{sig_idx - int(np.where(dates=='2023-12-08')[0][0])} bars back from sig)")
print(f"R3 anchor at t=2.0: SMA200 win, lookback=200, picked argmax close in [{sig_idx-200}..{sig_idx-1}]")

# Where does the highest close in [sig-200, sig-1] land?
window_close = c[sig_idx-200:sig_idx]
hi_idx = sig_idx - 200 + int(np.argmax(window_close))
print(f"Highest close in 200-bar window: idx={hi_idx} ({dates[hi_idx]}, close={c[hi_idx]:.3f}), "
      f"{sig_idx - hi_idx} bars back")

# What's the consolidation's lowest close, and where does the rally start?
# Using Dan's anchor 2023-12-08 as ground truth
dan_idx = int(np.where(dates=='2023-12-08')[0][0])
print(f"Dan anchor: {dates[dan_idx]} close={c[dan_idx]:.3f}")
print(f"Bars [{dates[dan_idx-5]} .. {dates[dan_idx+5]}]:")
for i in range(dan_idx-5, dan_idx+6):
    print(f"  {dates[i]} c={c[i]:.3f} l={l[i]:.3f} h={h[i]:.3f}")
