"""Check what R3 v2 (corrected) says for PTON with Dan's entry 2024-10-11, sig 2024-10-10."""
import pickle
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
MA_SET = [("EMA",3),("EMA",8),("EMA",21),("SMA",50),("SMA",100),("SMA",200)]

with open(CACHE, "rb") as f:
    universe = pickle.load(f)

df = universe["PTON"].copy()
if not pd.api.types.is_datetime64_any_dtype(df["date"]):
    df["date"] = pd.to_datetime(df["date"])
dates = df["date"].dt.strftime("%Y-%m-%d").values
h = df["high"].values.astype(float)
l = df["low"].values.astype(float)
c = df["close"].values.astype(float)
v = df["volume"].values.astype(float)
tp = (h + l + c) / 3.0

sig_date = "2024-10-10"
sig_idx = int(np.where(dates == sig_date)[0][0])
sig_close = c[sig_idx]
sig_low = l[sig_idx]
sig_high = h[sig_idx]
adr = float(np.mean(h[sig_idx-13:sig_idx+1] - l[sig_idx-13:sig_idx+1]))
print(f"PTON sig={sig_date}, sig_idx={sig_idx}")
print(f"  sig OHLC: O={df['open'].values[sig_idx]:.3f} H={sig_high:.3f} L={sig_low:.3f} C={sig_close:.3f}")
print(f"  ADR(14): {adr:.4f}")
print(f"  Sig bar range / ADR: {(sig_high-sig_low)/adr:.3f}")
print()


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


print("=== MA values at sig and ADR distances ===")
qualifying = []
for kind, n in MA_SET:
    if sig_idx < n - 1: continue
    val = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
    if not np.isfinite(val): continue
    signed_close = (sig_close - val) / adr
    signed_low = (sig_low - val) / adr
    in_t2 = abs(signed_close) <= 2.0
    print(f"  {kind}{n:>3}: value={val:.4f}  (close-MA)/ADR={signed_close:+.3f}  (low-MA)/ADR={signed_low:+.3f}  {'WITHIN t=2.0' if in_t2 else ''}")
    if in_t2:
        qualifying.append((n, kind, val, signed_close))
print()

# Auto-detect: longest period within t=2.0
qualifying.sort(key=lambda x: (-x[0], abs(x[3])))
if qualifying:
    n_lso = qualifying[0][0]
    kind_lso = qualifying[0][1]
    ma_lso = qualifying[0][2]
    print(f"=== Auto-detected support MA: {kind_lso}{n_lso} = {ma_lso:.3f} (signed {qualifying[0][3]:+.3f} ADR) ===")
else:
    print("=== No MA within t=2.0 — fallback ===")
    n_lso = 21
    kind_lso = "fallback"

print()
# Argmax AVWAP in [sig-N, sig-1]
A_start = max(0, sig_idx - n_lso)
A_end = sig_idx
print(f"=== Argmax AVWAP in [sig-{n_lso}, sig-1] = [{dates[A_start]}, {dates[A_end-1]}] ===")
cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
cum_v = np.concatenate([[0.0], np.cumsum(v)])
A_range = np.arange(A_start, A_end)
total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
total_v = cum_v[sig_idx + 1] - cum_v[A_range]
with np.errstate(invalid="ignore", divide="ignore"):
    avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)

# All candidates ranked
ranked = sorted(zip(A_range, avwaps), key=lambda x: -x[1])
print(f"  Top 10 anchors by AVWAP value:")
for A, av in ranked[:10]:
    print(f"    {dates[A]} (idx={A}, {sig_idx-A}b back) AVWAP={av:.4f}  {'< sig_close' if av < sig_close else '> sig_close'}")

best_A = ranked[0][0]
best_av = ranked[0][1]
print()
print(f"=== RULE OUTPUT ===")
print(f"Resistance anchor: {dates[best_A]} (idx={best_A}, {sig_idx-best_A}b back)")
print(f"Resistance AVWAP: {best_av:.4f}")
print(f"sig_close: {sig_close:.4f}")
print(f"AVWAP {'>' if best_av > sig_close else '<'} sig_close by {abs(best_av-sig_close):.4f} ({(best_av-sig_close)/adr:+.3f} ADR)")
print()
print(f"Dan's anchor: 2024-09-20 (idx={int(np.where(dates=='2024-09-20')[0][0])}, "
      f"{sig_idx - int(np.where(dates=='2024-09-20')[0][0])}b back)")
dan_idx = int(np.where(dates=='2024-09-20')[0][0])
seg_v = v[dan_idx:sig_idx+1]
dan_avwap = float((tp[dan_idx:sig_idx+1] * seg_v).sum() / seg_v.sum())
print(f"Dan's AVWAP: {dan_avwap:.4f}")
print(f"Difference rule vs Dan: {best_av - dan_avwap:+.4f} ({(best_av-dan_avwap)/adr:+.3f} ADR)")
