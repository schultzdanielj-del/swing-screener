"""Custom TA features stage:
  D1/W1/M1 close vs SMA{8,10,20,21,50,100,200} (% distance + binary above)
  W1 + M1 MACD(6,20,9): macd, signal, hist, sign flags

Per-ticker vectorized in pandas, lookup at each sample (ticker, date).
"""
import os, sys, json, time
import numpy as np, pandas as pd, pickle
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath('.'))
from scripts.ev_grinder import screen_features

SMA_PERIODS = [8, 10, 20, 21, 50, 100, 200]
MACD_FAST, MACD_SLOW, MACD_SIG = 6, 20, 9

def compute_ticker_features(df):
    """df: ticker daily OHLCV (date, close at least). Returns DataFrame indexed by date with all features."""
    d = df.sort_values('date').set_index('date').copy()
    close = d['close']

    out = {}

    # D1 SMAs
    for n in SMA_PERIODS:
        sma = close.rolling(n, min_periods=max(5, n // 2)).mean()
        out[f'd1_close_vs_sma{n}_pct'] = (close / sma - 1.0) * 100.0
        out[f'd1_above_sma{n}']         = (close > sma).astype(np.float32)

    # W1 (weekly close, indexed by Friday)
    w_close = close.resample('W-FRI').last()
    for n in SMA_PERIODS:
        sma = w_close.rolling(n, min_periods=max(3, n // 2)).mean()
        out[f'w1_close_vs_sma{n}_pct'] = ((w_close / sma - 1.0) * 100.0).reindex(d.index, method='ffill')
        out[f'w1_above_sma{n}']         = (w_close > sma).astype(np.float32).reindex(d.index, method='ffill')

    # W1 MACD(6, 20, 9)
    w_macd_fast = w_close.ewm(span=MACD_FAST, adjust=False).mean()
    w_macd_slow = w_close.ewm(span=MACD_SLOW, adjust=False).mean()
    w_macd = w_macd_fast - w_macd_slow
    w_sig = w_macd.ewm(span=MACD_SIG, adjust=False).mean()
    w_hist = w_macd - w_sig
    w_macd_pct = (w_macd / w_close * 100.0)   # macd as % of price (scale-free)
    w_hist_pct = (w_hist / w_close * 100.0)
    out['w1_macd_pct']               = w_macd_pct.reindex(d.index, method='ffill')
    out['w1_macd_hist_pct']          = w_hist_pct.reindex(d.index, method='ffill')
    out['w1_macd_above_signal']      = (w_macd > w_sig).astype(np.float32).reindex(d.index, method='ffill')
    out['w1_macd_above_zero']        = (w_macd > 0).astype(np.float32).reindex(d.index, method='ffill')
    out['w1_macd_hist_positive']     = (w_hist > 0).astype(np.float32).reindex(d.index, method='ffill')

    # M1 (monthly close)
    m_close = close.resample('ME').last()
    for n in SMA_PERIODS:
        sma = m_close.rolling(n, min_periods=max(3, n // 3)).mean()
        out[f'm1_close_vs_sma{n}_pct'] = ((m_close / sma - 1.0) * 100.0).reindex(d.index, method='ffill')
        out[f'm1_above_sma{n}']         = (m_close > sma).astype(np.float32).reindex(d.index, method='ffill')

    # M1 MACD(6, 20, 9)
    m_macd_fast = m_close.ewm(span=MACD_FAST, adjust=False).mean()
    m_macd_slow = m_close.ewm(span=MACD_SLOW, adjust=False).mean()
    m_macd = m_macd_fast - m_macd_slow
    m_sig = m_macd.ewm(span=MACD_SIG, adjust=False).mean()
    m_hist = m_macd - m_sig
    out['m1_macd_pct']               = (m_macd / m_close * 100.0).reindex(d.index, method='ffill')
    out['m1_macd_hist_pct']          = (m_hist / m_close * 100.0).reindex(d.index, method='ffill')
    out['m1_macd_above_signal']      = (m_macd > m_sig).astype(np.float32).reindex(d.index, method='ffill')
    out['m1_macd_above_zero']        = (m_macd > 0).astype(np.float32).reindex(d.index, method='ffill')
    out['m1_macd_hist_positive']     = (m_hist > 0).astype(np.float32).reindex(d.index, method='ffill')

    return pd.DataFrame(out, index=d.index)


_OHLCV = None
def _worker_init(ohlcv):
    global _OHLCV
    _OHLCV = ohlcv

def _process_one_ticker(args):
    ticker, sample_dates = args  # sample_dates: list of pd.Timestamp
    df = _OHLCV.get(ticker)
    if df is None or len(df) < 30:
        return ticker, None, None
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    feats = compute_ticker_features(df)
    # Look up at each sample date (use ffill — sample_dates are valid trading days from panel)
    sample_dates = pd.DatetimeIndex(sample_dates)
    rows = feats.reindex(sample_dates, method='ffill')
    return ticker, sample_dates, rows.values.astype(np.float32)


if __name__ == '__main__':
    t0 = time.time()
    panel = pd.read_parquet('research/disc_panel.parquet')
    panel['date'] = pd.to_datetime(panel['date'])
    print(f'panel: {len(panel)} samples, {panel["ticker"].nunique()} unique tickers')

    print('loading OHLCV...')
    with open('local_runner/cache/universe_ohlcv_daily.pkl','rb') as f:
        ohlcv = pickle.load(f)
    print(f'  {len(ohlcv)} tickers')

    # Compute one ticker's features to get the feature name list
    feat_names = list(compute_ticker_features(ohlcv['SPY'].copy()).columns)
    n_feat = len(feat_names)
    print(f'{n_feat} TA features defined')

    # Group samples by ticker
    by_t = panel.groupby('ticker', sort=False)
    work = [(t, g['date'].tolist()) for t, g in by_t]
    print(f'work items (tickers): {len(work)}')

    # Output matrix aligned to panel order
    X = np.full((len(panel), n_feat), np.nan, dtype=np.float32)
    # Build index from (ticker, date) → row position
    panel_idx = {(r.ticker, r.date): i for i, r in enumerate(panel.itertuples(index=False))}

    print(f'computing per-ticker TA in parallel...')
    completed = 0
    n_workers = 8
    t_start = time.time()
    with ProcessPoolExecutor(max_workers=n_workers, initializer=_worker_init, initargs=(ohlcv,)) as pool:
        futures = [pool.submit(_process_one_ticker, w) for w in work]
        for fut in as_completed(futures):
            ticker, dates, mat = fut.result()
            completed += 1
            if dates is None: continue
            for d, row in zip(dates, mat):
                pos = panel_idx.get((ticker, pd.Timestamp(d)))
                if pos is not None:
                    X[pos, :] = row
            if completed % 1000 == 0 or completed == len(work):
                el = time.time() - t_start
                rate = completed / el if el > 0 else 1
                eta = (len(work) - completed) / rate if rate > 0 else 0
                print(f'  [{completed}/{len(work)}] {el:.0f}s, ~{eta:.0f}s left')

    np.save('research/disc_ta_features.npy', X)
    with open('research/disc_ta_feat_names.json','w') as f:
        json.dump(feat_names, f)
    print(f'feature matrix: {X.shape}  ({X.nbytes/1e6:.1f} MB)  saved.')

    # === SCREEN ===
    is_pos     = (panel['label'].values == 1)
    is_broad   = (panel['pool'].values == 'neg_broad')
    is_matched = (panel['pool'].values == 'neg_matched')
    moves = np.full(len(panel), np.nan)

    def screen(label_mask_neg, tag):
        keep = is_pos | label_mask_neg
        Xs = X[keep]
        is_win = is_pos[keep]
        surv = screen_features(Xs, is_win, moves[keep], feat_names,
                               min_per_bucket=20, wr_pp_threshold=0.5, mfe_adr_threshold=1e9)
        for s in surv:
            s.pop('values', None)
            s['family'] = 'custom_ta'
            s['label_set'] = tag
        surv.sort(key=lambda s: s['wr_spread'], reverse=True)
        return surv

    surv_broad   = screen(is_broad,   'broad')
    surv_matched = screen(is_matched, 'matched')

    with open('research/disc_ta_results.json','w') as f:
        json.dump({'broad': surv_broad, 'matched': surv_matched}, f, indent=1, default=str)

    print(f'\n=== TOP TA FEATURES vs BROAD (n={len(surv_broad)}) ===')
    for s in surv_broad[:30]:
        d = ','.join(f'{w*100:.2f}' for w in s['decile_wr'])
        print(f"  {s['name']:30s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}  [{d}]")

    print(f'\n=== TOP TA FEATURES vs MATCHED (n={len(surv_matched)}) ===')
    for s in surv_matched[:30]:
        d = ','.join(f'{w*100:.2f}' for w in s['decile_wr'])
        print(f"  {s['name']:30s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}  [{d}]")

    print(f'\nwall: {time.time()-t0:.1f}s')
