"""Diagnose validation failures — check actual data in cache."""
import pickle, json

CACHE = r"local_runner\cache\universe_ohlcv_daily.pkl"
REF   = r"local_runner\cache\ticker_reference.json"
HISTORY_START = "2016-01-01"

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with open(REF, "r") as f:
    ref = json.load(f)

spy_df = universe["SPY"]
spy_dates = sorted(str(d)[:10] for d in spy_df["date"].values)

# Pick 10 worst negative mismatches
worst = []
for ticker, df in universe.items():
    ftd = ref.get(ticker)
    if ftd is None:
        continue
    start = max(ftd, HISTORY_START)
    expected = sum(1 for d in spy_dates if d >= start)
    actual = len(df)
    if actual != expected:
        worst.append((ticker, ftd, start, expected, actual, actual - expected))

worst.sort(key=lambda x: x[5])

print("10 worst NEGATIVE mismatches (actual << expected):")
print(f"{'Ticker':<10} {'Ref FTD':<12} {'Data 1st':<12} {'Data Last':<12} {'Exp':>6} {'Act':>6} {'Diff':>6}")
print("-" * 72)
for t, ftd, start, exp, act, diff in worst[:10]:
    df = universe[t]
    d_first = str(df["date"].iloc[0])[:10] if len(df) > 0 else "EMPTY"
    d_last = str(df["date"].iloc[-1])[:10] if len(df) > 0 else "EMPTY"
    print(f"{t:<10} {ftd:<12} {d_first:<12} {d_last:<12} {exp:>6} {act:>6} {diff:>+6}")

print("\n10 worst POSITIVE mismatches (actual >> expected):")
print(f"{'Ticker':<10} {'Ref FTD':<12} {'Data 1st':<12} {'Data Last':<12} {'Exp':>6} {'Act':>6} {'Diff':>6}")
print("-" * 72)
for t, ftd, start, exp, act, diff in sorted(worst, key=lambda x: x[5], reverse=True)[:10]:
    df = universe[t]
    d_first = str(df["date"].iloc[0])[:10] if len(df) > 0 else "EMPTY"
    d_last = str(df["date"].iloc[-1])[:10] if len(df) > 0 else "EMPTY"
    print(f"{t:<10} {ftd:<12} {d_first:<12} {d_last:<12} {exp:>6} {act:>6} {diff:>+6}")
