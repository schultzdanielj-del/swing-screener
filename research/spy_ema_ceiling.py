"""Investigate the apparent +$7 ceiling on SPY EMA8 - EMA21.

Confirms how often the spread breaks above various levels, identifies
discrete 'ceiling-break' episodes (continuous runs above a threshold),
and reports what happened to SPY after each.
"""
from __future__ import annotations
import os, pickle, numpy as np, pandas as pd

PKL = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"

with open(PKL, "rb") as f:
    cache = pickle.load(f)
df = cache["SPY"].copy()
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)
df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()
df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
df["spread"] = df["ema8"] - df["ema21"]

# Drop EMA warmup.
df = df.iloc[60:].reset_index(drop=True)

# Last 5y slice.
cutoff = df["date"].max() - pd.Timedelta(days=365 * 5)
last5 = df[df["date"] >= cutoff].reset_index(drop=True)

print(f"SPY {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}   n={len(df)}")
print(f"Last 5y: {last5['date'].iloc[0].date()} -> {last5['date'].iloc[-1].date()}   n={len(last5)}")
print(f"Current spread (last bar): ${df['spread'].iloc[-1]:+.2f}")

# --- 1. Frequency above various $ levels ---------------------------------
print("\n[1] How rare is each $ level (last 5y)?")
print(f"  {'level':>8}  {'bars_above':>10}  {'% of time':>10}")
for lvl in (5, 6, 7, 8, 9, 10, 12, 14, 16):
    n = int((last5["spread"] > lvl).sum())
    print(f"  >${lvl:<6}  {n:>10d}  {n/len(last5)*100:>9.2f}%")

# --- 2. Episodes: continuous runs where spread > $7 ----------------------
def find_episodes(s: pd.Series, level: float) -> list[tuple[int, int]]:
    above = s > level
    eps = []
    i = 0
    n = len(s)
    while i < n:
        if above.iloc[i]:
            j = i
            while j < n and above.iloc[j]:
                j += 1
            eps.append((i, j - 1))
            i = j
        else:
            i += 1
    return eps

LEVEL = 7.0
eps = find_episodes(last5["spread"], LEVEL)
print(f"\n[2] Episodes where spread > ${LEVEL} (last 5y)")
print(f"   total episodes: {len(eps)}")
print(f"   {'start':10}  {'end':10}  {'days':>4}  {'max$':>7}  {'close_at_start':>14}  {'close_at_max':>13}  {'close_after_20d':>16}  {'fwd20%':>8}")

n_last5 = len(last5)
for i0, i1 in eps:
    sub = last5.iloc[i0:i1+1]
    start_date = sub["date"].iloc[0].date()
    end_date = sub["date"].iloc[-1].date()
    duration = i1 - i0 + 1
    max_spread = sub["spread"].max()
    close_start = sub["close"].iloc[0]
    close_max_bar = last5.iloc[i0 + sub["spread"].values.argmax()]["close"]
    j20 = min(i1 + 20, n_last5 - 1)
    close_20 = last5.iloc[j20]["close"]
    fwd20 = (close_20 / close_max_bar - 1) * 100
    print(f"   {str(start_date):10}  {str(end_date):10}  {duration:>4d}  ${max_spread:>6.2f}  ${close_start:>12.2f}  ${close_max_bar:>11.2f}  ${close_20:>14.2f}  {fwd20:>+7.2f}%")

# --- 3. What follows a fresh break above $7? -----------------------------
# Define a breakout bar: spread crosses from <=$7 yesterday to >$7 today,
# with at least 5 prior bars at <=$7.
print(f"\n[3] Breakout bars (first close above ${LEVEL} after >=5 bars below)")
spread = last5["spread"].values
close = last5["close"].values
date = last5["date"].values
breakouts = []
for i in range(5, len(spread) - 20):
    if spread[i] > LEVEL and (spread[i-5:i] <= LEVEL).all():
        breakouts.append(i)

print(f"   {len(breakouts)} breakout bars")
print(f"   {'date':10}  {'spread@brk':>10}  {'close@brk':>10}  {'spread+5':>9}  {'spread+10':>9}  {'spread+20':>9}  {'fwd5%':>7}  {'fwd10%':>7}  {'fwd20%':>7}")
for i in breakouts:
    d = pd.Timestamp(date[i]).date()
    s_b = spread[i]
    c_b = close[i]
    s5 = spread[i+5] if i+5 < len(spread) else np.nan
    s10 = spread[i+10] if i+10 < len(spread) else np.nan
    s20 = spread[i+20] if i+20 < len(spread) else np.nan
    f5 = (close[i+5] / c_b - 1) * 100 if i+5 < len(close) else np.nan
    f10 = (close[i+10] / c_b - 1) * 100 if i+10 < len(close) else np.nan
    f20 = (close[i+20] / c_b - 1) * 100 if i+20 < len(close) else np.nan
    print(f"   {str(d):10}  ${s_b:>8.2f}  ${c_b:>8.2f}  ${s5:>+7.2f}  ${s10:>+7.2f}  ${s20:>+7.2f}  {f5:>+6.2f}%  {f10:>+6.2f}%  {f20:>+6.2f}%")

# --- 4. Aggregate: 20-day forward when breakout vs when spread under $7 --
print(f"\n[4] Forward returns conditioned on whether spread > ${LEVEL}")
mask_above = last5["spread"] > LEVEL
mask_below = ~mask_above
for h in (5, 10, 20):
    fwd = last5["close"].shift(-h) / last5["close"] - 1.0
    above = fwd[mask_above].dropna() * 100
    below = fwd[mask_below].dropna() * 100
    print(f"  fwd{h:>2}d:  above>${LEVEL}: n={len(above):>4d} mean={above.mean():+.3f}% pos={(above>0).mean()*100:.1f}%   "
          f"below<=${LEVEL}: n={len(below):>4d} mean={below.mean():+.3f}% pos={(below>0).mean()*100:.1f}%")

# --- 5. Full-history reference for the same level ------------------------
print(f"\n[5] Same level across full history ({df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()})")
print(f"  bars above ${LEVEL}: {int((df['spread']>LEVEL).sum())} of {len(df)} = {(df['spread']>LEVEL).mean()*100:.2f}%")
for lvl in (7, 8, 10, 12, 14):
    print(f"  >${lvl:<3}: {int((df['spread']>lvl).sum()):>5d} bars  ({(df['spread']>lvl).mean()*100:.2f}%)")
