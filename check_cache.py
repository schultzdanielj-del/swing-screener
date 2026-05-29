# Dan: run this from your repo root
# python check_cache.py

import pickle
import pandas as pd

cache_file = "local_runner/cache/universe_ohlcv_daily.pkl"
with open(cache_file, "rb") as f:
    universe = pickle.load(f)

total = len(universe)
spy_last = str(universe["SPY"]["date"].iloc[-1])[:10]

current = 0
behind = 0
behind_tickers = []
empty = 0

for ticker, df in universe.items():
    if len(df) == 0:
        empty += 1
        continue
    last = str(df["date"].iloc[-1])[:10]
    if last >= spy_last:
        current += 1
    else:
        behind += 1
        if len(behind_tickers) < 20:
            behind_tickers.append(f"{ticker} (last={last})")

print(f"Cache: {total} tickers")
print(f"SPY last bar: {spy_last}")
print(f"Current (have {spy_last}): {current}")
print(f"Behind: {behind}")
print(f"Empty: {empty}")
if behind_tickers:
    print(f"\nSample behind tickers:")
    for t in behind_tickers:
        print(f"  {t}")

# Check a few specific tickers for sanity
for t in ["AAPL", "MSFT", "SPY", "TSLA", "AMD"]:
    df = universe.get(t)
    if df is not None:
        last_date = str(df["date"].iloc[-1])[:10]
        last_close = float(df["close"].iloc[-1])
        bars = len(df)
        print(f"\n{t}: {bars} bars, last={last_date}, close={last_close:.2f}")
