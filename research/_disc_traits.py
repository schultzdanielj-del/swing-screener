"""Stage 2: Trait features per (ticker, date).

Features:
  market_cap_m at date, dvol_20d, close_price, log_price,
  days_since_first_bar (proxy for age), high52_pct_off, low52_pct_off,
  sector (one-hot 12), industry_size (rare), shares_outstanding, float_shares
"""
import pandas as pd, numpy as np, pickle, json, time, os
from collections import defaultdict

t0 = time.time()
panel = pd.read_parquet('research/disc_panel.parquet')
panel['date'] = pd.to_datetime(panel['date'])
print(f'panel: {len(panel)} rows')

with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
    oc = pickle.load(f)

with open('local_runner/cache/fundamentals_cache.json') as f:
    fc = json.load(f).get('tickers', {})

# Pre-compute per-ticker date indexing for fast lookup
ohlcv_by_ticker = {}
first_bar_by_ticker = {}
high52_by_ticker = {}   # rolling 252-day max close
low52_by_ticker = {}    # rolling 252-day min close
for t, df in oc.items():
    d = df.copy()
    d['date'] = pd.to_datetime(d['date'])
    d = d.sort_values('date').reset_index(drop=True)
    d['high52'] = d['close'].rolling(252, min_periods=20).max()
    d['low52']  = d['close'].rolling(252, min_periods=20).min()
    d['date_idx'] = np.arange(len(d))
    ohlcv_by_ticker[t] = d.set_index('date')
    first_bar_by_ticker[t] = d['date'].iloc[0]

print(f'precomputed {len(ohlcv_by_ticker)} ticker DFs in {time.time()-t0:.1f}s')

# Now extract per-sample traits via vectorized merge
# Group panel by ticker, merge against ticker df
out = []
missing_ohlcv = 0
panel_by_t = panel.groupby('ticker')
for t, group in panel_by_t:
    df = ohlcv_by_ticker.get(t)
    if df is None:
        missing_ohlcv += len(group)
        for r in group.itertuples():
            out.append({'ticker': r.ticker, 'date': r.date, 'label': r.label, 'pool': r.pool,
                        'close': np.nan, 'dvol_20d': np.nan, 'high52': np.nan, 'low52': np.nan,
                        'date_idx': -1})
        continue
    # asof-style: find closest prior date if not exact
    g = group.copy()
    # Use reindex with method='ffill'
    sub = df.reindex(g['date'], method='ffill')
    g['close']     = sub['close'].values
    g['dvol_20d']  = sub['dvol_20d'].values
    g['high52']    = sub['high52'].values
    g['low52']     = sub['low52'].values
    g['date_idx']  = sub['date_idx'].values
    for r in g.itertuples():
        out.append({'ticker': r.ticker, 'date': r.date, 'label': r.label, 'pool': r.pool,
                    'close': r.close, 'dvol_20d': r.dvol_20d,
                    'high52': r.high52, 'low52': r.low52, 'date_idx': r.date_idx})

traits = pd.DataFrame(out)
print(f'merged in {time.time()-t0:.1f}s, missing OHLCV rows: {missing_ohlcv}')

# Derived
traits['log_close']   = np.log10(traits['close'].clip(lower=0.01))
traits['log_dvol']    = np.log10(traits['dvol_20d'].clip(lower=1))
traits['high52_pct_off'] = (traits['close'] / traits['high52'] - 1.0) * 100.0
traits['low52_pct_off']  = (traits['close'] / traits['low52'] - 1.0) * 100.0

# Days since first bar (age proxy)
first_bar = pd.Series(first_bar_by_ticker)
traits = traits.merge(first_bar.rename('first_bar').reset_index().rename(columns={'index':'ticker'}),
                       on='ticker', how='left')
traits['days_since_first_bar'] = (traits['date'] - traits['first_bar']).dt.days

# Sector / shares / float from fundamentals
sec_lookup = {t: d.get('sector','Unknown') for t,d in fc.items()}
sh_lookup  = {t: d.get('shares_outstanding') for t,d in fc.items()}
fl_lookup  = {t: d.get('float_shares') for t,d in fc.items()}
traits['sector']   = traits['ticker'].map(sec_lookup).fillna('Unknown')
traits['shares_o'] = traits['ticker'].map(sh_lookup).astype('float64')
traits['float']    = traits['ticker'].map(fl_lookup).astype('float64')
traits['mkt_cap_at_date_m'] = (traits['close'] * traits['shares_o']) / 1e6
traits['float_pct'] = traits['float'] / traits['shares_o']  # float as % of shares
traits['log_mkt_cap'] = np.log10(traits['mkt_cap_at_date_m'].clip(lower=1))
traits['log_shares']  = np.log10(traits['shares_o'].clip(lower=1))
traits['log_float']   = np.log10(traits['float'].clip(lower=1))

traits = traits.drop(columns=['first_bar','high52','low52'])
traits.to_parquet('research/disc_traits.parquet', index=False)

print()
print(f'=== TRAIT FEATURES PER POOL ===')
print(f'rows: {len(traits)}')
print(f'cols: {[c for c in traits.columns if c not in ("ticker","date","label","pool","date_idx")]}')
# Quick distribution check
num_cols = ['close','log_close','dvol_20d','log_dvol','high52_pct_off','low52_pct_off',
            'days_since_first_bar','shares_o','float','mkt_cap_at_date_m','float_pct',
            'log_mkt_cap','log_shares','log_float']
print()
print('--- median per pool ---')
print(traits.groupby('pool')[num_cols].median().round(3).T.to_string())
print()
print('--- sector distribution per pool (top 8) ---')
print(traits.groupby('pool')['sector'].value_counts().groupby('pool').head(8).to_string())
print(f'\nwall: {time.time()-t0:.1f}s')
