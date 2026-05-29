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

EARN_BUFFER_BARS = 5

# Build per-entry slices. Each entry's effective window is [entry+1 .. day_before_next_earnings].
# mfe_window = max forward high in that window relative to entry_price.

slices = []
skipped = {'no_ohlcv':0, 'no_earnings':0, 'earnings_past_data':0, 'within_buffer':0}

for r in moves.itertuples():
    if r.ticker not in oc:
        skipped['no_ohlcv'] += 1; continue
    df = oc[r.ticker]
    dates = df['date'].values
    di = np.searchsorted(dates, np.datetime64(r.entry_date))
    if di >= len(dates) or dates[di] != np.datetime64(r.entry_date):
        skipped['no_ohlcv'] += 1; continue

    earns = earn_by_ticker.get(r.ticker)
    if earns is None or len(earns[earns > np.datetime64(r.entry_date)]) == 0:
        skipped['no_earnings'] += 1; continue
    next_earn = earns[earns > np.datetime64(r.entry_date)][0]
    ei = np.searchsorted(dates, next_earn)
    if ei >= len(dates):
        skipped['earnings_past_data'] += 1; continue
    if (ei - di) <= EARN_BUFFER_BARS:
        skipped['within_buffer'] += 1; continue
    horizon = ei - 1   # last bar BEFORE earnings (absolute index in df)
    if horizon <= di:
        skipped['within_buffer'] += 1; continue

    # forward arrays from entry+1 to horizon (inclusive)
    fwd_h = df['high'].values[di+1:horizon+1].astype(np.float64)
    fwd_l = df['low'].values[di+1:horizon+1].astype(np.float64)
    fwd_c = df['close'].values[di+1:horizon+1].astype(np.float64)
    if len(fwd_c) < 2:
        skipped['within_buffer'] += 1; continue

    # ADR14 at entry
    lo = max(0, di - 14); pre = df.iloc[lo:di]
    adr_pct = ((pre['high']-pre['low'])/pre['close']).mean() if len(pre) >= 5 else np.nan

    # MFE in pre-earnings window
    fwd_max_high = float(fwd_h.max())
    mfe = (fwd_max_high / r.entry_price) - 1.0
    horizon_close = float(fwd_c[-1])
    fallback_realized = (horizon_close / r.entry_price) - 1.0

    slices.append({
        'ticker': r.ticker,
        'entry_date': r.entry_date,
        'entry_price': float(r.entry_price),
        'h': fwd_h, 'l': fwd_l, 'c': fwd_c,
        'adr_pct': adr_pct,
        'horizon_bars': len(fwd_c),
        'mfe_window': mfe,
        'horizon_close': horizon_close,
        'fallback_realized': fallback_realized,
        'peak_return_pct_full': float(r.peak_return_pct),
    })

n_kept = len(slices)
print(f'=== ENTRY FILTERING ===')
print(f'  total 2020+ entries:           {len(moves)}')
for k,v in skipped.items():
    print(f'  skip: {k:25s} {v}')
print(f'  kept:                          {n_kept}')

# mfe_window distribution
mfes = np.array([s['mfe_window'] for s in slices])
hbars = np.array([s['horizon_bars'] for s in slices])
print()
print(f'=== PRE-EARNINGS MFE DISTRIBUTION (n={n_kept}) ===')
print(f'  horizon bars: median={int(np.median(hbars))} mean={np.mean(hbars):.1f} p05={int(np.percentile(hbars,5))} p95={int(np.percentile(hbars,95))}')
print(f'  mfe_window %: median={np.median(mfes)*100:.2f}% mean={np.mean(mfes)*100:.2f}% p25={np.percentile(mfes,25)*100:.2f}% p75={np.percentile(mfes,75)*100:.2f}% p95={np.percentile(mfes,95)*100:.2f}%')
print(f'  entries where mfe_window > 0:  {(mfes>0).sum()} / {n_kept}  ({(mfes>0).mean()*100:.1f}%)')
print()

# Rule definitions: apply within window [1..horizon_bars-1] (zero-indexed into fwd_c arrays)
# Each rule returns (triggered, exit_close). If not triggered, caller falls back to horizon_close.

def fixed_gain(s, x):
    target = s['entry_price'] * (1.0 + x)
    hits = np.where(s['h'] >= target)[0]
    return (True, target) if len(hits) else (False, None)

def n_bar(s, N):
    if N-1 < len(s['c']):
        return True, s['c'][N-1]
    return False, None  # N bigger than window — fall back

def trail_pct(s, x):
    rm = s['h'][0] if len(s['h'])>0 else s['entry_price']
    rm = max(rm, s['entry_price'])
    for k in range(len(s['c'])):
        if s['h'][k] > rm: rm = s['h'][k]
        if s['c'][k] <= rm * (1.0 - x):
            return True, s['c'][k]
    return False, None

def trail_adr(s, N):
    if not np.isfinite(s['adr_pct']): return False, None
    return trail_pct(s, N * s['adr_pct'])

def ma_cross(s, M):
    c = s['c']
    if len(c) < M+1: return False, None
    for k in range(M-1, len(c)):
        if c[k] < c[k-M+1:k+1].mean():
            return True, c[k]
    return False, None

def lower_low(s, N):
    l = s['l']; c = s['c']
    if len(l) < N+1: return False, None
    for k in range(N, len(c)):
        if l[k] < l[k-N:k].min():
            return True, c[k]
    return False, None

rules = []
for x in [0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00, 1.50, 2.00]:
    rules.append((f'fixed_gain_{int(x*100)}pct', fixed_gain, x))
for N in [3, 5, 8, 13, 20, 30, 45]:
    rules.append((f'n_bar_then_earn_{N}', n_bar, N))
for x in [0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]:
    rules.append((f'trail_pct_{int(x*100)}pct', trail_pct, x))
for N in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
    rules.append((f'trail_adr_{N}x', trail_adr, N))
for M in [3, 5, 8, 10, 13, 20]:
    rules.append((f'ma_cross_{M}', ma_cross, M))
for N in [3, 5, 8, 13]:
    rules.append((f'lower_low_{N}', lower_low, N))

# Also: baseline of "always fall back to earnings" (no rule)
def no_rule(s, _):
    return False, None
rules.insert(0, ('JUST_EXIT_BEFORE_EARN (baseline)', no_rule, None))

results = []
for name, fn, param in rules:
    captures = []; realizeds = []
    n_rule_trig = 0
    for s in slices:
        trig, exit_px = fn(s, param)
        if trig:
            n_rule_trig += 1
            realized = (exit_px / s['entry_price']) - 1.0
        else:
            realized = s['fallback_realized']
        mfe = s['mfe_window']
        cap = realized / mfe if mfe > 0 else np.nan
        captures.append(cap)
        realizeds.append(realized)
    caps = np.array(captures); rels = np.array(realizeds)
    results.append({
        'rule': name, 'param': param,
        'rule_trigger_rate': n_rule_trig / n_kept,
        'median_capture': np.nanmedian(caps),
        'mean_capture':   np.nanmean(caps),
        'median_realized':np.nanmedian(rels),
        'mean_realized':  np.nanmean(rels),
        'p25_realized':   np.nanpercentile(rels, 25),
        'p75_realized':   np.nanpercentile(rels, 75),
        'losers':         int((rels < 0).sum()),
    })

res = pd.DataFrame(results).sort_values('median_capture', ascending=False).reset_index(drop=True)
res.to_csv('research/exit_earnings_grind_results.csv', index=False)

print(f'=== RULES RANKED BY MEDIAN CAPTURE OF PRE-EARNINGS MFE (n={n_kept}) ===')
print(res.head(20).to_string(index=False))
print()
print('=== BASELINE (no rule, exit day before earnings only) ===')
bl = res[res['rule'].str.startswith('JUST_EXIT')].iloc[0]
print(bl.to_string())
print()
print(f'wall: {time.time()-t0:.1f}s')
