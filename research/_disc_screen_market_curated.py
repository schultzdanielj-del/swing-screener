"""Curated market discrimination screen — ~30 high-value instruments only.

Reduces runtime from ~6 hours to ~15-20 min by limiting to the market features
that actually matter for regime analysis: broad indices, vol, breadth, sectors,
risk-on/off, style.
"""
import os, sys, json, time
import numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath('.'))

CURATED_INSTRUMENTS = [
    # Broad indices
    'SPY', 'QQQ', 'IWM', 'MDY', 'RSP', 'DIA',
    # Volatility
    'VIX.INDX', 'VVIX.INDX', 'VIX3M.INDX', 'VXX',
    # Breadth (advances/declines indices)
    'ADVN.INDX', 'ADVQ.INDX',
    # SPDR sectors (11)
    'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLB', 'XLC', 'XLRE', 'XLU',
    # Risk-on/off (credit, duration, gold)
    'HYG', 'TLT', 'IEF', 'GLD',
    # Style/size factors
    'IWD', 'IWF', 'IWN', 'IWO', 'MTUM', 'SPHB', 'SPLV',
]

_W = {}

def _worker_init(sig_dates, is_win, moves, n_exprs, expr_names):
    _W['sig_dates'] = sig_dates
    _W['is_win'] = is_win
    _W['moves'] = moves
    _W['n_exprs'] = n_exprs
    _W['expr_names'] = expr_names

def _screen_one_inst(args):
    inst_id, npz_path = args
    from datetime import date as dt_date, timedelta
    from local_runner.expr_cache_builder import _open_npz
    from scripts.ev_grinder import screen_features
    t0 = time.time()
    try:
        loaded = _open_npz(npz_path)
        data = loaded['data']
        dates = loaded['dates']
        date_to_row = {str(d): i for i, d in enumerate(dates)}

        sig_dates = _W['sig_dates']
        is_win = _W['is_win']
        moves = _W['moves']
        n_exprs = _W['n_exprs']
        expr_names = _W['expr_names']

        # float16 directly into the matrix
        vals = np.full((len(sig_dates), n_exprs), np.nan, dtype=np.float16)
        for si, sd in enumerate(sig_dates):
            ri = date_to_row.get(sd)
            if ri is None:
                for offset in range(1, 6):
                    try:
                        d = dt_date.fromisoformat(sd) - timedelta(days=offset)
                        ri = date_to_row.get(d.isoformat())
                        if ri is not None:
                            break
                    except (ValueError, TypeError):
                        break
            if ri is not None and ri < data.shape[0]:
                vals[si, :] = data[ri, :n_exprs]

        del data, loaded

        feat_names = [f'{inst_id}__{expr_names[j]}' for j in range(n_exprs)]
        vals_f32 = vals.astype(np.float32)
        del vals
        surv = screen_features(vals_f32, is_win, moves, feat_names,
                               min_per_bucket=20, wr_pp_threshold=2.0, mfe_adr_threshold=1e9)
        del vals_f32
        for s in surv:
            s.pop('values', None)
        surv.sort(key=lambda s: s['wr_spread'], reverse=True)
        surv = surv[:30]
        return inst_id, surv, time.time() - t0, None
    except Exception as e:
        import traceback
        return inst_id, [], time.time() - t0, traceback.format_exc()


def screen_market(sig_df, n_workers=2, label_tag=''):
    from scripts.ev_grinder import _instrument_filename
    MKT_DIR = 'local_runner/cache/market_series'
    MKT_MANIFEST = os.path.join(MKT_DIR, '_manifest.json')

    sig_dates = sig_df['date'].dt.strftime('%Y-%m-%d').tolist()
    is_win = (sig_df['label'].values == 1)
    moves = np.full(len(sig_df), np.nan)

    with open(MKT_MANIFEST) as f:
        manifest = json.load(f)
    expr_names = manifest['expr_names']
    n_exprs = len(expr_names)

    work = []
    missing = []
    for inst_id in CURATED_INSTRUMENTS:
        path = os.path.join(MKT_DIR, _instrument_filename(inst_id))
        if not os.path.exists(path):
            missing.append(inst_id); continue
        work.append((inst_id, path))

    print(f'[{label_tag}] {len(work)}/{len(CURATED_INSTRUMENTS)} curated instruments × {n_exprs} exprs · {len(sig_dates):,} samples · {n_workers} workers')
    if missing:
        print(f'  missing: {missing}')

    all_surv = []
    t0 = time.time()
    errors = []
    with ProcessPoolExecutor(max_workers=n_workers, initializer=_worker_init,
                              initargs=(sig_dates, is_win, moves, n_exprs, expr_names)) as pool:
        futures = [pool.submit(_screen_one_inst, w) for w in work]
        for i, fut in enumerate(as_completed(futures), 1):
            inst_id, surv, el, err = fut.result()
            if err:
                errors.append((inst_id, err))
            all_surv.extend(surv)
            wt = time.time() - t0
            rate = i / wt if wt > 0 else 1
            eta = (len(work) - i) / rate if rate > 0 else 0
            print(f'  [{i}/{len(work)}] {inst_id:14s} ({el:.0f}s)  elapsed: {wt:.0f}s  ETA: {eta:.0f}s  surv: {len(all_surv)}', flush=True)
    if errors:
        print(f'  {len(errors)} errors. first 3:')
        for inst, err in errors[:3]:
            print(f'   {inst}: {err.splitlines()[-1]}')
    all_surv.sort(key=lambda s: s['wr_spread'], reverse=True)
    return all_surv


if __name__ == '__main__':
    panel = pd.read_parquet('research/disc_panel.parquet')
    panel['date'] = pd.to_datetime(panel['date'])
    pos = panel[panel['label']==1].reset_index(drop=True)
    broad = panel[panel['pool']=='neg_broad'].reset_index(drop=True)
    matched = panel[panel['pool']=='neg_matched'].reset_index(drop=True)

    print('=== PHASE A: positives vs broad ===', flush=True)
    sig_broad = pd.concat([pos, broad], ignore_index=True)
    surv_broad = screen_market(sig_broad, n_workers=2, label_tag='broad')
    with open('research/disc_market_broad.json','w') as f:
        json.dump(surv_broad, f, indent=1, default=str)
    print(f'  saved {len(surv_broad)} survivors\n', flush=True)

    print('=== PHASE B: positives vs matched ===', flush=True)
    sig_matched = pd.concat([pos, matched], ignore_index=True)
    surv_matched = screen_market(sig_matched, n_workers=2, label_tag='matched')
    with open('research/disc_market_matched.json','w') as f:
        json.dump(surv_matched, f, indent=1, default=str)
    print(f'  saved {len(surv_matched)} survivors\n', flush=True)

    print('=== TOP 30 MARKET FEATURES (broad) ===', flush=True)
    for s in surv_broad[:30]:
        print(f"  {s['name']:50s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")
    print('\n=== TOP 30 MARKET FEATURES (matched) ===', flush=True)
    for s in surv_matched[:30]:
        print(f"  {s['name']:50s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")
