"""Stage 5 (chunked, RAM-safe): full expression-cache discrimination screen.

Chunks features in groups of CHUNK_SIZE columns. Per chunk:
  - Allocate (n_samples x CHUNK_SIZE) float16 ≈ 600 MB
  - Per-ticker parallel reader fills it
  - Screen those features → survivors
  - Drop chunk matrix
  - Repeat

Total peak RAM ≈ chunk_matrix (0.6 GB) + workers (0.3 GB × 4 = 1.2 GB) + survivors (small).
Fits well under 32 GB even with other processes running.
"""
import os, sys, json, time, gc
import numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date as dt_date, timedelta

sys.path.insert(0, os.path.abspath('.'))
from scripts.ev_grinder import screen_features

EXPR_DIR = 'local_runner/cache/expr_series'
MANIFEST = os.path.join(EXPR_DIR, '_manifest.json')
CHUNK_SIZE = 2000      # expression columns per chunk
N_WORKERS  = 4         # conservative


# Per-worker globals via initializer
_W = {}

def _worker_init(samples_per_ticker, panel_idx_arr, n_samples):
    _W['samples_per_ticker'] = samples_per_ticker   # dict: ticker -> list of (global_idx, date_str)
    _W['n_samples'] = n_samples


def _worker(args):
    ticker, col_start, col_end = args
    from local_runner.expr_cache_builder import _open_npz
    path = os.path.join(EXPR_DIR, f'{ticker}.npz')
    if not os.path.exists(path):
        return []
    try:
        loaded = _open_npz(path)
        data = loaded['data']      # (n_dates, 16216) float16
        dates = loaded['dates']
        date_to_row = {str(d): i for i, d in enumerate(dates)}

        out = []
        for gidx, sd in _W['samples_per_ticker'][ticker]:
            ri = date_to_row.get(sd)
            if ri is None:
                for off in range(1, 6):
                    try:
                        pd_date = (dt_date.fromisoformat(sd) - timedelta(days=off)).isoformat()
                        ri = date_to_row.get(pd_date)
                        if ri is not None: break
                    except ValueError: break
            if ri is not None and ri < data.shape[0]:
                # Return only the chunk slice
                out.append((gidx, data[ri, col_start:col_end].astype(np.float16)))
        del data, loaded
        return out
    except Exception as e:
        return [('ERR', str(e))]


def main():
    t0 = time.time()
    panel = pd.read_parquet('research/disc_panel.parquet')
    panel['date'] = pd.to_datetime(panel['date'])
    panel = panel.reset_index(drop=True)
    n_samples = len(panel)
    print(f'panel: {n_samples:,} samples, {panel["ticker"].nunique():,} unique tickers')

    with open(MANIFEST) as f:
        manifest = json.load(f)
    expr_names = manifest['expr_names']
    n_exprs = len(expr_names)
    print(f'manifest: {n_exprs:,} expressions, chunk={CHUNK_SIZE}, workers={N_WORKERS}')

    # samples_per_ticker
    samples_per_ticker = {}
    for r in panel.itertuples():
        samples_per_ticker.setdefault(r.ticker, []).append((int(r.Index), r.date.strftime('%Y-%m-%d')))
    tickers_in_panel = list(samples_per_ticker.keys())
    print(f'tickers in panel: {len(tickers_in_panel):,}')

    is_pos     = (panel['label'].values == 1)
    is_broad   = (panel['pool'].values == 'neg_broad')
    is_matched = (panel['pool'].values == 'neg_matched')
    moves = np.full(n_samples, np.nan)

    surv_broad_all = []
    surv_matched_all = []

    n_chunks = (n_exprs + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f'\nrunning {n_chunks} chunks of {CHUNK_SIZE} features each\n')

    for ck in range(n_chunks):
        c0 = ck * CHUNK_SIZE
        c1 = min(c0 + CHUNK_SIZE, n_exprs)
        chunk_cols = c1 - c0
        chunk_names = expr_names[c0:c1]
        print(f'[chunk {ck+1}/{n_chunks}] cols {c0}:{c1} ({chunk_cols} features)')

        # Allocate chunk matrix
        X = np.full((n_samples, chunk_cols), np.nan, dtype=np.float16)
        chunk_mb = X.nbytes / 1e6
        print(f'  matrix allocated: {chunk_mb:.0f} MB')

        # Parallel per-ticker fill
        ts = time.time()
        with ProcessPoolExecutor(max_workers=N_WORKERS, initializer=_worker_init,
                                  initargs=(samples_per_ticker, None, n_samples)) as pool:
            futures = [pool.submit(_worker, (t, c0, c1)) for t in tickers_in_panel]
            done = 0
            errors = 0
            for fut in as_completed(futures):
                out = fut.result()
                done += 1
                for gidx, row in out:
                    if gidx == 'ERR':
                        errors += 1
                        continue
                    X[gidx, :] = row
                if done % 2000 == 0:
                    print(f'    [{done}/{len(tickers_in_panel)}] {time.time()-ts:.0f}s elapsed')
        print(f'  fill done: {time.time()-ts:.0f}s · errors: {errors}')

        # Screen this chunk (float32 cast for screen_features)
        ts_s = time.time()
        X32 = X.astype(np.float32)
        del X
        gc.collect()

        # Broad screen
        keep = is_pos | is_broad
        surv = screen_features(
            X32[keep], is_pos[keep], moves[keep], chunk_names,
            min_per_bucket=20, wr_pp_threshold=2.0, mfe_adr_threshold=1e9
        )
        for s in surv:
            s.pop('values', None)
            s['family'] = 'expr_cache'
            s['label_set'] = 'broad'
            s['pos_rate_d1']  = s['decile_wr'][0]
            s['pos_rate_d10'] = s['decile_wr'][9]
        surv_broad_all.extend(surv)
        n_b = len(surv)

        # Matched screen (reuse X32)
        keep = is_pos | is_matched
        surv = screen_features(
            X32[keep], is_pos[keep], moves[keep], chunk_names,
            min_per_bucket=20, wr_pp_threshold=2.0, mfe_adr_threshold=1e9
        )
        for s in surv:
            s.pop('values', None)
            s['family'] = 'expr_cache'
            s['label_set'] = 'matched'
            s['pos_rate_d1']  = s['decile_wr'][0]
            s['pos_rate_d10'] = s['decile_wr'][9]
        surv_matched_all.extend(surv)
        n_m = len(surv)

        del X32
        gc.collect()
        print(f'  screen done: {time.time()-ts_s:.0f}s · broad survivors: {n_b}, matched: {n_m}')
        print(f'  cumulative: broad={len(surv_broad_all)}, matched={len(surv_matched_all)}\n')

    surv_broad_all.sort(key=lambda s: s['wr_spread'], reverse=True)
    surv_matched_all.sort(key=lambda s: s['wr_spread'], reverse=True)

    with open('research/disc_expr_broad.json','w') as f:
        json.dump(surv_broad_all, f, indent=1, default=str)
    with open('research/disc_expr_matched.json','w') as f:
        json.dump(surv_matched_all, f, indent=1, default=str)

    print(f'\n=== TOP 50 EXPRESSION FEATURES vs BROAD ===')
    for s in surv_broad_all[:50]:
        print(f"  {s['name']:40s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")
    print(f'\n=== TOP 50 EXPRESSION FEATURES vs MATCHED ===')
    for s in surv_matched_all[:50]:
        print(f"  {s['name']:40s} sp={s['wr_spread']*100:5.2f}pp  D1={s['decile_wr'][0]*100:5.2f}%  D10={s['decile_wr'][9]*100:5.2f}%  {s['direction']:11s}")
    print(f'\nwall total: {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
