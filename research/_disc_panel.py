"""Stage 1: Build positive + 2 negative-pool sample panels.

Positives  : 100Moves entries 2020+
Neg broad  : 100K random (ticker, date) from tradable universe, 2020-2026, excluding positives
Neg matched: 100K (ticker, date) where date is a positive's entry_date, ticker not a positive on that date, tradable

Saved to research/disc_panel.parquet (single file with label + pool column).
"""
import pandas as pd, numpy as np, pickle, time, os
np.random.seed(42)

t0 = time.time()
START = pd.Timestamp('2020-01-01')
END = pd.Timestamp('2026-05-08')
N_NEG = 100_000

# === Positives ===
moves = pd.read_parquet('research/100moves.parquet')
moves['entry_date'] = pd.to_datetime(moves['entry_date'])
pos_df = moves[moves['entry_date'] >= START][['ticker','entry_date']].copy()
pos_df = pos_df.rename(columns={'entry_date':'date'})
pos_df['label'] = 1
pos_df['pool'] = 'positive'
print(f'positives: {len(pos_df)}')

# === Tradable universe per ticker ===
print('loading OHLCV...')
with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
    oc = pickle.load(f)
print(f'  {len(oc)} tickers in OHLCV')

print('building tradable (ticker, date) pool (price>$1 AND $vol>$1M)...')
# Build two arrays: per-ticker date arrays, then concatenate.
ticker_list = []
date_list = []
for ticker, df in oc.items():
    if 'dvol_20d' not in df.columns:  # skip if missing
        continue
    m = (df['date'] >= START) & (df['date'] <= END) & (df['close'] > 1.0) & (df['dvol_20d'] > 1e6)
    if m.sum() == 0:
        continue
    sub = df.loc[m, 'date'].values
    ticker_list.append(np.full(len(sub), ticker, dtype=object))
    date_list.append(sub.astype('datetime64[ns]'))

all_tickers = np.concatenate(ticker_list)
all_dates = np.concatenate(date_list)
print(f'  total tradable (ticker, date) pairs: {len(all_tickers):,}')

# Build positives set for exclusion
pos_keys = set(zip(pos_df['ticker'].values, pd.to_datetime(pos_df['date']).values.astype('datetime64[ns]')))

# === Broad negatives ===
print(f'sampling {N_NEG:,} broad negatives...')
# Strategy: oversample then filter exclusions
oversample = int(N_NEG * 1.05)
keep_broad = []
attempts = 0
while len(keep_broad) < N_NEG and attempts < 5:
    attempts += 1
    idx = np.random.choice(len(all_tickers), oversample, replace=False)
    for i in idx:
        key = (all_tickers[i], all_dates[i])
        if key in pos_keys: continue
        keep_broad.append(key)
        if len(keep_broad) >= N_NEG: break
    print(f'  attempt {attempts}: have {len(keep_broad)}')
    oversample = (N_NEG - len(keep_broad)) * 2 + 1000

neg_broad = pd.DataFrame(keep_broad[:N_NEG], columns=['ticker','date'])
neg_broad['label'] = 0
neg_broad['pool'] = 'neg_broad'
print(f'  broad: {len(neg_broad)}')

# === Matched negatives (date ∈ positive dates) ===
print(f'sampling {N_NEG:,} date-matched negatives...')
pos_dates_set = set(pd.to_datetime(pos_df['date']).values.astype('datetime64[ns]'))
# Mask tradable pairs to only those whose date is in positive set
date_mask = np.isin(all_dates, np.array(list(pos_dates_set), dtype='datetime64[ns]'))
print(f'  tradable pairs on positive dates: {date_mask.sum():,}')
matched_tickers = all_tickers[date_mask]
matched_dates = all_dates[date_mask]

keep_matched = []
attempts = 0
while len(keep_matched) < N_NEG and attempts < 5:
    attempts += 1
    oversample = int((N_NEG - len(keep_matched)) * 1.05 + 1000)
    idx = np.random.choice(len(matched_tickers), oversample, replace=False)
    for i in idx:
        key = (matched_tickers[i], matched_dates[i])
        if key in pos_keys: continue
        keep_matched.append(key)
        if len(keep_matched) >= N_NEG: break
    print(f'  attempt {attempts}: have {len(keep_matched)}')

neg_matched = pd.DataFrame(keep_matched[:N_NEG], columns=['ticker','date'])
neg_matched['label'] = 0
neg_matched['pool'] = 'neg_matched'
print(f'  matched: {len(neg_matched)}')

# === Save ===
panel = pd.concat([pos_df, neg_broad, neg_matched], ignore_index=True)
panel['date'] = pd.to_datetime(panel['date'])
panel.to_parquet('research/disc_panel.parquet', index=False)

print()
print('=== PANEL SUMMARY ===')
print(panel['pool'].value_counts().to_string())
print(f'unique tickers across panel: {panel["ticker"].nunique():,}')
print(f'date coverage: {panel["date"].min().date()} -> {panel["date"].max().date()}')
print(f'saved to research/disc_panel.parquet  ({os.path.getsize("research/disc_panel.parquet")/1e6:.1f} MB)')
print(f'wall: {time.time()-t0:.1f}s')
