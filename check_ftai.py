"""Check FTAI exit condition behavior after 2025-01-08 signal date.
Uses same computation as outcome_grinder.py for exact parity."""
import pickle, pandas as pd, numpy as np, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scripts.outcome_grinder import _compute_adx

cache = pickle.load(open('local_runner/cache/universe_ohlcv_5yr.pkl', 'rb'))
df = cache['FTAI']

high = df['high'].values
low = df['low'].values
close = df['close'].values

adx7 = _compute_adx(high, low, close, 7)

# ADX declining: today < 3 bars ago (same as outcome_grinder)
n = len(adx7)
adx_declining = np.zeros(n)
adx_declining[3:] = (adx7[3:] < adx7[:-3]).astype(float)

# Count true rolling 10-bar
cumsum = np.cumsum(adx_declining)
count_true_10 = np.full(n, np.nan)
window = 10
count_true_10[window-1:] = cumsum[window-1:] - np.concatenate([[0], cumsum[:n-window]])

# Map to dates
vals = pd.Series(count_true_10, index=df.index)
mask = df.index >= '2025-01-08'
bars_after = mask.sum()
exit_after = vals[mask].head(125)
below_3 = exit_after[exit_after < 3.0]

print(f"FTAI — bars after 2025-01-08: {bars_after}")
print(f"\ncount_true_10 (adx7 declining) — first 30 values:")
print(exit_after.head(30).to_string())
print(f"\nEver below 3.0 in 120 bars? {'YES' if len(below_3) > 0 else 'NO'}")
if len(below_3) > 0:
    print(f"First occurrence: {below_3.index[0]} = {below_3.iloc[0]:.2f}")
else:
    print(f"Min value in window: {exit_after.min():.2f}")

print(f"\nOHLCV after signal (first 20 bars):")
print(df[mask].head(20)[['open','high','low','close']].to_string())
