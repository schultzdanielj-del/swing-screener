import pandas as pd, numpy as np, pickle, sqlite3, time
from collections import defaultdict

t0 = time.time()

moves = pd.read_parquet('research/100moves.parquet')
for c in ['entry_date','signal_date','peak_date']:
    moves[c] = pd.to_datetime(moves[c])
moves = moves[moves['entry_date'] >= '2020-01-01'].reset_index(drop=True)

with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
    oc = pickle.load(f)

# Load earnings dates per ticker
con = sqlite3.connect('data/scanperfect.db')
edf = pd.read_sql('SELECT ticker, earnings_date FROM earnings_dates', con)
con.close()
edf['earnings_date'] = pd.to_datetime(edf['earnings_date'])
edf = edf.sort_values(['ticker','earnings_date'])
earn_by_ticker = {t: g['earnings_date'].values for t, g in edf.groupby('ticker')}

EARN_BUFFER_BARS = 5  # don't take entries within 5 TRADING days of next earnings

# Walk entries
rows_out = []
skip_no_ohlcv = 0
skip_no_earnings = 0
skip_too_close_to_earnings = 0
skip_earnings_past_data = 0

for r in moves.itertuples():
    if r.ticker not in oc:
        skip_no_ohlcv += 1; continue
    df = oc[r.ticker]
    dates = df['date'].values
    di = np.searchsorted(dates, np.datetime64(r.entry_date))
    if di >= len(dates) or dates[di] != np.datetime64(r.entry_date):
        skip_no_ohlcv += 1; continue

    # Next earnings strictly AFTER entry_date
    earns = earn_by_ticker.get(r.ticker)
    if earns is None:
        skip_no_earnings += 1; continue
    after = earns[earns > np.datetime64(r.entry_date)]
    if len(after) == 0:
        skip_no_earnings += 1; continue
    next_earn = after[0]

    # Bar index of next earnings in OHLCV
    ei = np.searchsorted(dates, next_earn)
    if ei >= len(dates):
        # next earnings is beyond our OHLCV cache
        skip_earnings_past_data += 1; continue
    # If earnings_date is not a trading day, ei lands on the first trading day >= earnings_date
    # which is fine for "exit day BEFORE earnings": exit_bar = ei - 1

    trading_bars_to_earnings = ei - di   # bars from entry to earnings bar
    if trading_bars_to_earnings <= EARN_BUFFER_BARS:
        skip_too_close_to_earnings += 1; continue

    exit_bar = ei - 1  # last close before earnings (could be earnings_date itself if non-trading, or earnings_date-1 if trading)
    if exit_bar <= di:
        skip_too_close_to_earnings += 1; continue

    exit_close = float(df['close'].iloc[exit_bar])
    realized = (exit_close / float(r.entry_price)) - 1.0
    mfe = float(r.peak_return_pct) / 100.0
    capture = realized / mfe if mfe > 0 else np.nan

    rows_out.append({
        'ticker': r.ticker,
        'entry_date': r.entry_date,
        'entry_price': float(r.entry_price),
        'next_earnings': pd.Timestamp(next_earn),
        'exit_date': pd.Timestamp(dates[exit_bar]),
        'trading_bars_held': int(exit_bar - di),
        'exit_close': exit_close,
        'realized_pct': realized * 100,
        'mfe_pct': mfe * 100,
        'capture': capture,
        'peak_date': r.peak_date,
        'peak_return_pct': float(r.peak_return_pct),
    })

n_in = len(moves)
n_kept = len(rows_out)
print(f'=== ENTRY FILTERING ===')
print(f'  total 2020+ entries:           {n_in}')
print(f'  skip: ticker not in OHLCV:     {skip_no_ohlcv}')
print(f'  skip: no earnings data:        {skip_no_earnings}')
print(f'  skip: earnings beyond OHLCV:   {skip_earnings_past_data}')
print(f'  skip: earnings within {EARN_BUFFER_BARS} bars:   {skip_too_close_to_earnings}')
print(f'  kept (took the trade):         {n_kept}')
print()

if n_kept == 0:
    print('no trades survived filtering'); raise SystemExit

out = pd.DataFrame(rows_out)
out.to_csv('research/exit_at_earnings.csv', index=False)

caps = out['capture'].values
rels = out['realized_pct'].values
held = out['trading_bars_held'].values

print(f'=== RULE: filter 5-bar pre-earnings, exit day before next earnings  (n={n_kept}) ===')
print()
print('Trading bars held:')
print(f'  median: {np.median(held):.0f}   mean: {np.mean(held):.1f}   p05: {np.percentile(held,5):.0f}   p95: {np.percentile(held,95):.0f}')
print()
print('Realized return %:')
print(f'  median: {np.nanmedian(rels):6.2f}%   mean: {np.nanmean(rels):6.2f}%')
print(f'  p25:    {np.nanpercentile(rels,25):6.2f}%   p75:  {np.nanpercentile(rels,75):6.2f}%')
print(f'  p05:    {np.nanpercentile(rels,5):6.2f}%   p95:  {np.nanpercentile(rels,95):6.2f}%')
print(f'  losers (realized <0): {(rels<0).sum()} / {n_kept}  ({(rels<0).mean()*100:.1f}%)')
print()
print('Capture (realized / MFE):')
print(f'  median: {np.nanmedian(caps)*100:6.2f}%   mean: {np.nanmean(caps)*100:6.2f}%')
print(f'  p25:    {np.nanpercentile(caps,25)*100:6.2f}%   p75:  {np.nanpercentile(caps,75)*100:6.2f}%')
print()
print('=== COMPARISON to previous winners (h=180, 2020+, capture) ===')
print('  fixed_gain_100pct:     median capture 88.5%,  mean realized 100% (by construction)')
print('  n_bar_exit_180:        median capture 98.4%,  mean realized 153%')
print(f'  exit_day_before_earn:  median capture {np.nanmedian(caps)*100:.1f}%,  mean realized {np.nanmean(rels):.1f}%')
print()
print(f'wall: {time.time()-t0:.1f}s')
