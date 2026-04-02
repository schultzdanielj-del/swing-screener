"""Diagnose validation failures — run from repo root."""
import pickle, json, os

CACHE = r"local_runner\cache\universe_ohlcv_daily.pkl"
REF   = r"local_runner\cache\ticker_reference.json"

# Load cache
with open(CACHE, "rb") as f:
    universe = pickle.load(f)

# Load reference
with open(REF, "r") as f:
    ref = json.load(f)

# Build SPY dates
spy_df = universe["SPY"]
spy_dates = sorted(str(d)[:10] for d in spy_df["date"].values)

HISTORY_START = "2016-01-01"

# Check every ticker, collect mismatches
mismatches = []
for ticker, df in universe.items():
    ftd = ref.get(ticker)
    if ftd is None:
        continue
    start = max(ftd, HISTORY_START)
    expected = sum(1 for d in spy_dates if d >= start)
    actual = len(df)
    if actual != expected:
        diff = actual - expected
        mismatches.append((ticker, ftd, start, expected, actual, diff))

print(f"Total mismatches: {len(mismatches)}")
print(f"\nSample (first 20):")
print(f"{'Ticker':<12} {'FTD':<12} {'Start':<12} {'Expected':>8} {'Actual':>8} {'Diff':>6}")
print("-" * 62)
for t, ftd, start, exp, act, diff in sorted(mismatches, key=lambda x: x[5])[:20]:
    print(f"{t:<12} {ftd:<12} {start:<12} {exp:>8} {act:>8} {diff:>+6}")

print(f"\n... and largest positive diffs:")
for t, ftd, start, exp, act, diff in sorted(mismatches, key=lambda x: x[5], reverse=True)[:10]:
    print(f"{t:<12} {ftd:<12} {start:<12} {exp:>8} {act:>8} {diff:>+6}")
