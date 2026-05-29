import pandas as pd, numpy as np, pickle, sqlite3, time

t0 = time.time()
moves = pd.read_parquet('research/100moves.parquet')
for c in ['entry_date','signal_date','peak_date']:
    moves[c] = pd.to_datetime(moves[c])
moves = moves[moves['entry_date'] >= '2020-01-01'].reset_index(drop=True)

with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
    oc = pickle.load(f)

con = sqlite3.connect('data/scanperfect.db')
edf = pd.read_sql('SELECT ticker, earnings_date FROM earnings_dates', con); con.close()
edf['earnings_date'] = pd.to_datetime(edf['earnings_date'])
edf = edf.sort_values(['ticker','earnings_date'])
earn_by_ticker = {t: g['earnings_date'].values for t, g in edf.groupby('ticker')}

# Build per-entry trade lists for each of three rules.
# Each trade: (entry_date, exit_date, realized_return)

trades_180 = []
trades_100pct = []
trades_earn = []

for r in moves.itertuples():
    if r.ticker not in oc:
        continue
    df = oc[r.ticker]
    dates = df['date'].values
    di = np.searchsorted(dates, np.datetime64(r.entry_date))
    if di >= len(dates) or dates[di] != np.datetime64(r.entry_date):
        continue

    # Rule A: n_bar_exit_180
    end180 = di + 180
    if end180 < len(df):
        exit_close = float(df['close'].iloc[end180])
        exit_date = pd.Timestamp(dates[end180])
        trades_180.append((r.entry_date, exit_date, (exit_close / r.entry_price) - 1.0))

    # Rule B: fixed_gain_100pct (take profit at +100% from entry)
    target = r.entry_price * 2.0
    end_horizon = min(di + 1 + 180, len(df))
    h_fwd = df['high'].values[di+1:end_horizon]
    hits = np.where(h_fwd >= target)[0]
    if len(hits):
        exit_idx = di + 1 + hits[0]
        exit_date = pd.Timestamp(dates[exit_idx])
        trades_100pct.append((r.entry_date, exit_date, 1.0))   # exit at +100%

    # Rule C: skip if next earnings within 5 bars, else exit day-before next earnings
    earns = earn_by_ticker.get(r.ticker)
    if earns is not None:
        after = earns[earns > np.datetime64(r.entry_date)]
        if len(after):
            ei = np.searchsorted(dates, after[0])
            if ei < len(dates) and (ei - di) > 5:
                exit_bar = ei - 1
                if exit_bar > di:
                    exit_close = float(df['close'].iloc[exit_bar])
                    exit_date = pd.Timestamp(dates[exit_bar])
                    trades_earn.append((r.entry_date, exit_date, (exit_close / r.entry_price) - 1.0))

# Compounding sim: sort trades by entry, take non-overlapping (single capital pool, one trade at a time).
# When a candidate's entry < current_exit, skip it (overlap).

def compound(trades):
    trades_sorted = sorted(trades, key=lambda x: x[0])
    equity = 1.0
    next_free = pd.Timestamp('1900-01-01')
    n_taken = 0; n_skipped = 0
    curve = []
    for e, x, r in trades_sorted:
        if e < next_free:
            n_skipped += 1; continue
        equity *= (1.0 + r)
        n_taken += 1
        next_free = x
        curve.append((x, equity))
    return equity, n_taken, n_skipped, curve

# Also: fixed-size bet version (each trade risks a fixed % of capital, no overlap problem)
# We'll do 1% of capital per trade, hold many in parallel allowed by sizing.
def compound_fixed_size(trades, bet=0.01):
    equity = 1.0
    for e, x, r in trades:
        equity *= (1.0 + bet * r)
    return equity

print(f'=== TRADE COUNTS (2020+, with required data) ===')
print(f'  n_bar_exit_180:        {len(trades_180)}')
print(f'  fixed_gain_100pct:     {len(trades_100pct)}')
print(f'  exit_day_before_earn:  {len(trades_earn)}')
print()

window_start = pd.Timestamp('2020-01-01')
window_end = pd.Timestamp('2026-05-08')
years = (window_end - window_start).days / 365.25

print(f'=== SERIAL COMPOUNDING (one trade at a time, skip overlaps) ===')
print(f'  window: {window_start.date()} -> {window_end.date()}  ({years:.2f} yrs)')
print()
print(f'  {"rule":25s} {"trades_taken":>12s} {"skipped":>9s} {"final_$1":>11s} {"CAGR":>9s}')
for name, trades in [('n_bar_exit_180', trades_180), ('fixed_gain_100pct', trades_100pct), ('exit_day_before_earn', trades_earn)]:
    eq, ntook, nskip, _ = compound(trades)
    cagr = eq**(1/years) - 1
    print(f'  {name:25s} {ntook:>12d} {nskip:>9d} ${eq:>10,.2f} {cagr*100:>8.1f}%')

print()
print(f'=== FIXED-SIZE BETS (1% of capital per trade, all 1,9xx trades, no overlap concern) ===')
print(f'  {"rule":25s} {"trades":>8s} {"final_$1":>11s} {"CAGR":>9s}')
for name, trades in [('n_bar_exit_180', trades_180), ('fixed_gain_100pct', trades_100pct), ('exit_day_before_earn', trades_earn)]:
    eq = compound_fixed_size(trades, 0.01)
    cagr = eq**(1/years) - 1
    print(f'  {name:25s} {len(trades):>8d} ${eq:>10,.4f} {cagr*100:>8.1f}%')

print(f'\nwall: {time.time()-t0:.1f}s')
