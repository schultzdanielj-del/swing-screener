import pandas as pd, numpy as np, pickle, time

t0 = time.time()
moves = pd.read_parquet('research/100moves.parquet')
for c in ['entry_date','signal_date','peak_date']:
    moves[c] = pd.to_datetime(moves[c])
moves = moves[moves['entry_date'] >= '2020-01-01'].reset_index(drop=True)

with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
    oc = pickle.load(f)

HORIZON = 180

slices = []
missing = 0
for r in moves.itertuples():
    if r.ticker not in oc:
        missing += 1; continue
    df = oc[r.ticker]
    dates = df['date'].values
    di = np.searchsorted(dates, np.datetime64(r.entry_date))
    if di >= len(dates) or dates[di] != np.datetime64(r.entry_date):
        missing += 1; continue
    end = min(di + 1 + HORIZON, len(df))
    fwd = df.iloc[di:end]
    if len(fwd) < 3:
        missing += 1; continue
    lo = max(0, di - 14); pre = df.iloc[lo:di]
    adr_pct = ((pre['high'] - pre['low']) / pre['close']).mean() if len(pre) >= 5 else np.nan
    slices.append({
        'ticker': r.ticker, 'entry_date': r.entry_date,
        'entry_price': float(r.entry_price),
        'mfe_pct_data': float(r.peak_return_pct) / 100.0,
        'opens':  fwd['open'].values.astype(np.float64),
        'highs':  fwd['high'].values.astype(np.float64),
        'lows':   fwd['low'].values.astype(np.float64),
        'closes': fwd['close'].values.astype(np.float64),
        'adr_pct': adr_pct,
    })
print(f'sliced {len(slices)} entries, missing {missing}, horizon={HORIZON} bars')

def rule_fixed_gain(s, x):
    target = s['entry_price'] * (1.0 + x)
    h = s['highs'][1:]
    hits = np.where(h >= target)[0]
    return (True, target) if len(hits) else (False, np.nan)

def rule_n_bar(s, N):
    return (True, s['closes'][N]) if N < len(s['closes']) else (False, np.nan)

def rule_trail_pct(s, x):
    h = s['highs']; c = s['closes']; rm = h[0]
    for k in range(1, len(c)):
        if h[k] > rm:
            rm = h[k]
        if c[k] <= rm * (1.0 - x):
            return True, c[k]
    return False, np.nan

def rule_trail_adr(s, N):
    if not np.isfinite(s['adr_pct']):
        return False, np.nan
    return rule_trail_pct(s, N * s['adr_pct'])

def rule_ma_cross(s, M):
    c = s['closes']
    if len(c) < M + 2:
        return False, np.nan
    for k in range(M, len(c)):
        if c[k] < c[k-M+1:k+1].mean():
            return True, c[k]
    return False, np.nan

def rule_lower_low(s, N):
    l = s['lows']; c = s['closes']
    if len(l) < N + 2:
        return False, np.nan
    for k in range(N, len(c)):
        if l[k] < l[k-N:k].min():
            return True, c[k]
    return False, np.nan

rules = []
for x in [0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 5.00, 10.00]:
    rules.append((f'fixed_gain_{int(x*100)}pct', rule_fixed_gain, x))
for N in [1,2,3,5,8,13,20,30,45,60,90,120,150,180]:
    rules.append((f'n_bar_exit_{N}', rule_n_bar, N))
for x in [0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
    rules.append((f'trail_pct_{int(x*100)}pct', rule_trail_pct, x))
for N in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]:
    rules.append((f'trail_adr_{N}x', rule_trail_adr, N))
for M in [5, 8, 10, 13, 20, 30, 50]:
    rules.append((f'ma_cross_{M}', rule_ma_cross, M))
for N in [3, 5, 8, 13, 20]:
    rules.append((f'lower_low_{N}', rule_lower_low, N))

results = []
for name, fn, param in rules:
    captures = []; realizeds = []; n_trig = 0
    for s in slices:
        trig, exit_px = fn(s, param)
        if trig:
            n_trig += 1
            realized = (exit_px / s['entry_price']) - 1.0
            mfe = s['mfe_pct_data']
            cap = realized / mfe if mfe > 0 else np.nan
            captures.append(cap); realizeds.append(realized)
    if n_trig == 0:
        continue
    caps = np.array(captures); rels = np.array(realizeds)
    results.append({
        'rule': name, 'param': param,
        'trigger_rate': n_trig / len(slices),
        'n_triggered': n_trig,
        'median_capture': np.nanmedian(caps),
        'mean_capture': np.nanmean(caps),
        'median_realized': np.nanmedian(rels),
        'mean_realized': np.nanmean(rels),
        'p25_capture': np.nanpercentile(caps, 25),
        'p75_capture': np.nanpercentile(caps, 75),
    })

res = pd.DataFrame(results)
strict = res[res['trigger_rate'] >= 0.999].sort_values('median_capture', ascending=False).reset_index(drop=True)
loose  = res.sort_values('median_capture', ascending=False).reset_index(drop=True)
res.to_csv('research/exit_rule_grind_h180_all.csv', index=False)
strict.to_csv('research/exit_rule_grind_h180_strict.csv', index=False)

print(f'\n=== STRICT 100%-trigger gate, horizon=180 ({len(strict)} surviving rules) ===')
print(strict.head(15).to_string(index=False))
print('\n=== TOP rules with trigger_rate >= 0.90, sorted by median_capture ===')
print(loose[loose['trigger_rate']>=0.90].head(15).to_string(index=False))
print(f'\nwall: {time.time()-t0:.1f}s')
