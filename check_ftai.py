"""Check FTAI exit condition behavior after 2025-01-08 signal date."""
import pickle, pandas as pd, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scripts.expression_engine import compute_expression_series

cache = pickle.load(open('local_runner/cache/universe_ohlcv_5yr.pkl', 'rb'))
df = cache['FTAI']
mask = df.index >= '2025-01-08'
bars_after = mask.sum()

# Exit expression
vals = compute_expression_series(df, 'adx_7_declining_count_true_10b')
exit_vals = vals[mask].head(125)

# Check if it ever goes below 3.0
below_3 = exit_vals[exit_vals < 3.0]

print(f"FTAI — bars after 2025-01-08: {bars_after}")
print(f"\nadx_7_declining_count_true_10b — first 30 values:")
print(exit_vals.head(30).to_string())
print(f"\nEver below 3.0 in 120 bars? {'YES' if len(below_3) > 0 else 'NO'}")
if len(below_3) > 0:
    print(f"First occurrence: {below_3.index[0]} = {below_3.iloc[0]:.2f}")
else:
    print(f"Min value in window: {exit_vals.min():.2f}")

# Also show OHLCV for context
print(f"\nOHLCV after signal (first 30 bars):")
print(df[mask].head(30)[['open','high','low','close']].to_string())
