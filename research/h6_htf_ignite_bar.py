"""H6: HTF ignite bar detection.

Hypothesis: HTF anchor = bar with largest single-bar (close - prev_close) / ADR in [sig - N, sig - 1].
For HTT (Dan picked 141 back), check if there's an ignite-bar candidate near 141 back.

For comparison:
  - 3-8 EMA tightness: most recent bar where |EMA3 - EMA8| / ADR < threshold.

Test on HTT specifically; if either works, layer on top of R3 for HTF setups.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    ("AR",   "2020-12-17"): ("2020-12-11", 5.01),
    ("BB",   "2024-12-20"): ("2024-12-17", 3.07),
    ("HTT",  "2021-01-12"): ("2020-06-19", 1.69),
    ("LMND", "2024-11-05"): ("2024-11-01", 24.50),
    ("LMND", "2024-11-06"): ("2024-11-01", 24.51),
    ("REAL", "2024-11-13"): ("2024-11-06", 3.79),
    # Excluding DRN (gap-up) and PTON (debatable)
}


def load(ticker, universe):
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


def adr14(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def ema_series(arr, n):
    alpha = 2.0 / (n + 1)
    out = np.full_like(arr, np.nan, dtype=float)
    e = arr[0]
    out[0] = e
    for i in range(1, len(arr)):
        e = alpha * arr[i] + (1 - alpha) * e
        out[i] = e
    return out


with open(CACHE, "rb") as f:
    universe = pickle.load(f)

with sqlite3.connect(DB) as conn:
    htf_rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples WHERE setup_type = 'htf' ORDER BY ticker, entry_date"
    ).fetchall()

# Focus on HTT first; if pattern emerges, check all HTF
print("=== HTT 2021-01-12 ignite-bar candidates ===")
ticker, entry_date = "HTT", "2021-01-12"
dates, h, l, c, v, tp = load(ticker, universe)
entry_idx = int(np.where(dates == entry_date)[0][0])
sig_idx = entry_idx - 1
adr = adr14(h, l, sig_idx)
print(f"sig_idx={sig_idx} ({dates[sig_idx]}), sig_close={c[sig_idx]:.2f}, ADR={adr:.3f}")
dan_anchor_idx = int(np.where(dates == "2020-06-19")[0][0])
print(f"Dan anchor: idx={dan_anchor_idx} ({dates[dan_anchor_idx]}), {sig_idx-dan_anchor_idx} bars back")
print()

# Method A: largest single-bar (close - prev_close) / ADR in [sig - 200, sig - 1]
window_start = max(1, sig_idx - 200)
gains = (c[window_start:sig_idx] - c[window_start-1:sig_idx-1]) / adr
top_n = 10
top_idx = np.argsort(-gains)[:top_n]
print(f"Top 10 single-bar gains in [{dates[window_start]} .. {dates[sig_idx-1]}]:")
for ti in top_idx:
    actual_idx = window_start + ti
    print(f"  {dates[actual_idx]} (idx={actual_idx}, {sig_idx - actual_idx}b back) gain={gains[ti]:.3f} ADR  [c={c[actual_idx]:.2f} v={v[actual_idx]:.0f}]")
print()

# Method B: largest single-bar range / ADR
ranges = (h[window_start:sig_idx] - l[window_start:sig_idx]) / adr
top_idx = np.argsort(-ranges)[:top_n]
print(f"Top 10 single-bar ranges:")
for ti in top_idx:
    actual_idx = window_start + ti
    print(f"  {dates[actual_idx]} (idx={actual_idx}, {sig_idx - actual_idx}b back) range={ranges[ti]:.3f} ADR  [c={c[actual_idx]:.2f}]")
print()

# Method C: largest volume spike (z-score vs trailing 20-bar mean volume)
vol_z = []
for i in range(window_start, sig_idx):
    if i >= 20:
        prior = v[i-20:i]
        mean_v = float(np.mean(prior))
        std_v = float(np.std(prior))
        if std_v > 0:
            vol_z.append((i, (v[i] - mean_v) / std_v))
        else:
            vol_z.append((i, float('nan')))
vol_z.sort(key=lambda x: -x[1] if not np.isnan(x[1]) else -np.inf)
print(f"Top 10 volume spike z-scores (vs 20-bar trailing mean):")
for actual_idx, z in vol_z[:top_n]:
    print(f"  {dates[actual_idx]} (idx={actual_idx}, {sig_idx - actual_idx}b back) vol_z={z:.2f}  [v={v[actual_idx]:.0f}, c={c[actual_idx]:.2f}]")
print()

# Method D: 3 EMA - 8 EMA tightness
ema3 = ema_series(c, 3)
ema8 = ema_series(c, 8)
tightness = np.abs(ema3 - ema8) / adr  # ADR-normalized gap
print(f"Most recent bars walking back from sig where |EMA3 - EMA8| / ADR is small:")
tight_data = []
for i in range(sig_idx - 1, max(7, sig_idx - 50), -1):
    tight_data.append((i, tightness[i]))
tight_data.sort(key=lambda x: x[1])
for actual_idx, tight in tight_data[:10]:
    print(f"  {dates[actual_idx]} (idx={actual_idx}, {sig_idx - actual_idx}b back) tightness={tight:.3f} ADR")
