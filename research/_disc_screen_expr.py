"""Stage 5: Full expression-cache discrimination screen.

For each (ticker, date) sample in the panel, look up its row of 16,216 expression
values from the per-ticker .npz cache. Run screen_features on the (202K x 16K)
matrix twice (broad / matched labels).

Architecture:
  - Pool workers process one ticker at a time (load .npz, extract sample rows)
  - Returns float16 rows tagged with global panel indices
  - Main process assembles (n_samples x 16216) float16 matrix
  - Runs screen_features at the end
"""
import os, sys, json, time
import numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date as dt_date, timedelta

sys.path.insert(0, os.path.abspath('.'))
from scripts.ev_grinder import screen_features

EXPR_DIR = 'local_runner/cache/expr_series'
MANIFEST = os.path.join(EXPR_DIR, '_manifest.json')


def _worker(args):
    ticker, samples = args  # samples: list of (global_idx, date_str)
    from local_runner.expr_cache_builder import _open_npz
    path = os.path.join(EXPR_DIR, f'{ticker}.npz')
    if not os.path.exists(path):
        return ticker, []
    try:
        loaded = _open_npz(path)
        data = loaded['data']
        dates = loaded['dates']
        # Date string lookup
        date_to_row = {str(d): i for i, d in enumerate(dates)}
        out = []
        for gidx, sd in samples:
            ri = date_to_row.get(sd)
            if ri is None:
                # asof: try prior trading days for holiday-adjacent samples
                for off in range(1, 6):
                    try:
                        pd_date = (dt_date.fromisoformat(sd) - timedelta(days=off)).isoformat()
                        ri = date_to_row.get(pd_date)
                        if ri is not None:
                            break
                    except ValueError:
                        break
            if ri is not None and ri < data.shape[0]:
                out.append((gidx, data[ri, :].astype(np.float16)))
        del data, loaded
        return ticker, out
    except Exception as e:
        return ticker, [('ERR', str(e))]


def main():
    t0 = time.time()
    print('loading panel + manifest...')
    panel = pd.read_parquet('research/disc_panel.parquet')
    panel['date'] = pd.to_datetime(panel['date'])
    panel = panel.reset_index(drop=True)
    n_samples = len(panel)
    print(f'  panel: {n_samples:,} samples, {panel["ticker"].nunique():,} unique tickers')

    with open(MANIFEST) as f:
        manifest = json.load(f)
    expr_names = manifest['expr_names']
    n_exprs = len(expr_names)
    print(f'  expr manifest: {n_exprs:,} expressions')

    # Pre-allocate the big matrix: 202K x 16216 x float16 = 6.5 GB
    X = np.full((n_samples, n_exprs), np.nan, dtype=np.float16)
    print(f'  matrix: {X.shape}  ({X.nbytes / 1e9:.2f} GB)')

    # Group panel by ticker → work items
    by_t = panel.groupby('ticker', sort=False)
    work = []
    for t, g in by_t:
        samples = [(int(i), d.strftime('%Y-%m-%d')) for i, d in zip(g.index, g['date'])]
        work.append((t, samples))
    print(f'  work items: {len(work):,} tickers')

    print(f'\nrunning per-ticker lookups in parallel (8 workers)...')
    t_lookup = time.time()
    completed = 0
    n_filled = 0
    n_missing_ticker = 0
    n_errors = 0
    with ProcessPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker, w) for w in work]
        for fut in as_completed(futures):
            ticker, out = fut.result()
            completed += 1
            if not out:
                n_missing_ticker += 1
            else:
                for gidx, row in out:
                    if gidx == 'ERR':
                        n_errors += 1
                        continue
                    X[gidx, :] = row
                    n_filled += 1
            if completed % 500 == 0 or completed == len(work):
                el = time.time() - t_lookup
                rate = completed / el if el > 0 else 1
                eta = (len(work) - completed) / rate if rate > 0 else 0
                print(f'  [{completed}/{len(work)}] {el:.0f}s, ~{eta:.0f}s left | filled: {n_filled:,} · missing tickers: {n_missing_ticker} · errors: {n_errors}')

    valid_rows = np.isfinite(X[:, 0])  # rows where first expr is finite
    print(f'  rows with valid first-expr value: {valid_rows.sum():,} / {n_samples:,}')

    # === SCREEN ===
    is_pos     = (panel['label'].values == 1)
    is_broad   = (panel['pool'].values == 'neg_broad')
    is_matched = (panel['pool'].values == 'neg_matched')
    moves = np.full(n_samples, np.nan)

    def run_screen(neg_mask, tag):
        keep = is_pos | neg_mask
        Xs = X[keep].astype(np.float32)
        is_win = is_pos[keep]
        print(f'\n[{tag}] screening {Xs.shape[0]:,} samples × {n_exprs:,} features...')
        ts = time.time()
        surv = screen_features(
            Xs, is_win, moves[keep], expr_names,
            min_per_bucket=20, wr_pp_threshold=2.0, mfe_adr_threshold=1e9
        )
        for s in surv:
            s.pop('values', None)
            s['family'] = 'expr_cache'
            s['label_set'] = tag
            s['pos_rate_d1']  = s['decile_wr'][0]
            s['pos_rate_d10'] = s['decile_wr'][9]
        surv.sort(key=lambda s: s['wr_spread'], reverse=True)
        print(f'  {len(surv)} survivors at {time.time()-ts:.1f}s')
        return surv

    surv_broad   = run_screen(is_broad,   'broad')
    surv_matched = run_screen(is_matched, 'matched')

    # Save
    with open('research/disc_expr_broad.json', 'w') as f:
        json.dump(surv_broad, f, indent=1, default=str)
    with open('research/disc_expr_matched.json', 'w') as f:
        json.dump(surv_matched, f, indent=1, default=str)
    print(f'\nsaved: research/disc_expr_broad.json ({len(surv_broad)}) + matched ({len(surv_matched)})')

    print(f'\n=== TOP 40 EXPRESSION FEATURES vs BROAD ===')
    for s in surv_broad[:40]:
        dr = ','.join(f'{w*100:.2f}' for w in s['decile_wr'])
        print(f"  {s['name']:40s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")

    print(f'\n=== TOP 40 EXPRESSION FEATURES vs MATCHED ===')
    for s in surv_matched[:40]:
        dr = ','.join(f'{w*100:.2f}' for w in s['decile_wr'])
        print(f"  {s['name']:40s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")

    print(f'\nwall total: {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
