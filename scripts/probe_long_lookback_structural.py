"""
Long-lookback structural probe — read-only experiment.

Question: do longer-lookback (252, 504) versions of raw-price structural ops
add signal beyond what existing expressions already capture?

Candidates (per op × 2 lookbacks = 8 total):
  range_position, pullback_adr14, retracement_level, range_width_adr14
  at lookbacks 252 and 504.

Pool (existing expressions):
  short-lookback family, LSP top-3 distances, algo shallowest distances,
  ext_ceil_*_lb252, pctrank_close_252.

Reports per-ticker correlation distribution + pooled decile forward returns.
No thresholds. Numbers only.
"""
import io, json, os, pickle, time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import zstandard as zstd

CACHE_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
EXPR_DIR = os.path.join(CACHE_DIR, "expr_series")
MANIFEST = os.path.join(EXPR_DIR, "_manifest.json")
OHLCV_PKL = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "research", "long_lookback_probe_results.md")

MIN_BARS = 600
MIN_FWD = 60  # ensure 60-bar forward returns exist


def open_npz(path):
    with open(path, "rb") as f:
        head = f.read(2)
    with open(path, "rb") as f:
        blob = f.read()
    if head == b"PK":
        return np.load(io.BytesIO(blob), allow_pickle=True)
    dctx = zstd.ZstdDecompressor()
    return np.load(io.BytesIO(dctx.decompress(blob)), allow_pickle=True)


def rolling_max(a, w):
    """Rolling max over window w; NaN for bars with fewer than w prior bars."""
    n = len(a)
    out = np.full(n, np.nan)
    if n < w:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    sw = sliding_window_view(a, w)
    out[w - 1:] = sw.max(axis=1)
    return out


def rolling_min(a, w):
    n = len(a)
    out = np.full(n, np.nan)
    if n < w:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    sw = sliding_window_view(a, w)
    out[w - 1:] = sw.min(axis=1)
    return out


def compute_adr14(h, l, c):
    """ADR14 = 14-bar SMA of daily range (high - low)."""
    rng = h - l
    n = len(rng)
    out = np.full(n, np.nan)
    if n < 14:
        return out
    csum = np.cumsum(rng)
    out[13] = csum[13] / 14
    out[14:] = (csum[14:] - csum[:-14]) / 14
    return out


def compute_candidate(op, lb, o, h, l, c, adr14):
    if op == "range_position" or op == "retracement_level":
        maxh = rolling_max(h, lb)
        minl = rolling_min(l, lb)
        rng = maxh - minl
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (c - minl) / rng
        return v
    elif op == "pullback":
        maxh = rolling_max(h, lb)
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (maxh - c) / adr14
        return v
    elif op == "range_width":
        maxh = rolling_max(h, lb)
        minl = rolling_min(l, lb)
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (maxh - minl) / adr14
        return v
    raise ValueError(op)


CANDIDATES = []  # (name, op, lb)
for op in ["range_position", "pullback", "retracement_level", "range_width"]:
    for lb in [252, 504]:
        CANDIDATES.append((f"{op}_{lb}", op, lb))


# per-candidate baseline 120 name (for forward-return comparison)
BASELINE_120 = {
    "range_position_252": "range_pos_120",
    "range_position_504": "range_pos_120",
    "pullback_252": "pullback_120_adr14",
    "pullback_504": "pullback_120_adr14",
    "retracement_level_252": "retrace_120",
    "retracement_level_504": "retrace_120",
    "range_width_252": "range_width_120",
    "range_width_504": "range_width_120",
}


# per-candidate short-lookback family names
def short_family(cand_name):
    if cand_name.startswith("range_position"):
        return ["range_pos_50", "range_pos_65", "range_pos_90", "range_pos_120"]
    if cand_name.startswith("retracement_level"):
        return ["retrace_50", "retrace_65", "retrace_90", "retrace_120"]
    if cand_name.startswith("pullback"):
        return ["pullback_50_adr14", "pullback_65_adr14", "pullback_120_adr14"]
    if cand_name.startswith("range_width"):
        return ["range_width_50", "range_width_65", "range_width_120"]
    return []


COMMON_POOL = [
    "level_above1_distance", "level_above2_distance", "level_above3_distance",
    "level_below1_distance", "level_below2_distance", "level_below3_distance",
    "algo_hminus_shallowest_distance", "algo_lplus_shallowest_distance",
    "ext_ceil_avgc50_lb252_adr14", "ext_ceil_avgc200_lb252_adr14",
    "pctrank_close_252",
]


def process_ticker(args):
    ticker, ohlcv_dict = args
    df = ohlcv_dict
    n = len(df)
    if n < MIN_BARS + MIN_FWD:
        return {"ticker": ticker, "skipped": "short_history"}

    path = os.path.join(EXPR_DIR, f"{ticker}.npz")
    if not os.path.exists(path):
        return {"ticker": ticker, "skipped": "no_cache"}

    try:
        d = open_npz(path)
        cache_data = d["data"]
        cache_dates = d["dates"]
    except Exception as e:
        return {"ticker": ticker, "skipped": f"cache_load_err:{e}"}

    # Align OHLCV dates to cache dates (cache starts 2020-01-02)
    df_dates = pd.to_datetime(df["date"]).to_numpy()
    c_dates_dt = pd.to_datetime(cache_dates).to_numpy()
    # find overlapping window: cache is subset of ohlcv typically
    # simplest: take the tail of ohlcv that matches cache len; assume same trailing bars
    if len(cache_data) > n:
        return {"ticker": ticker, "skipped": "cache_longer_than_ohlcv"}

    # Slice OHLCV to the range matching cache dates (trust date alignment)
    # Use ohlcv last len(cache_data) bars assuming both end same day
    ohlcv_slice = df.iloc[-len(cache_data):]
    o = ohlcv_slice["open"].to_numpy(dtype=np.float64)
    h = ohlcv_slice["high"].to_numpy(dtype=np.float64)
    l = ohlcv_slice["low"].to_numpy(dtype=np.float64)
    c = ohlcv_slice["close"].to_numpy(dtype=np.float64)
    if len(c) < MIN_BARS + MIN_FWD:
        return {"ticker": ticker, "skipped": "short_after_align"}

    adr14 = compute_adr14(h, l, c)

    # Compute candidates
    cand_values = {}
    for name, op, lb in CANDIDATES:
        cand_values[name] = compute_candidate(op, lb, o, h, l, c, adr14)

    # Correlations
    per_cand_corrs = {}
    for name, _, _ in CANDIDATES:
        v = cand_values[name]
        corrs = []
        for pool_name, pool_idx in WORKER_POOL_IDX.items():
            if pool_idx is None:
                continue
            pv = cache_data[:, pool_idx].astype(np.float32)
            # pairwise non-nan
            mask = np.isfinite(v) & np.isfinite(pv)
            if mask.sum() < 100:
                continue
            a = v[mask]
            b = pv[mask]
            if a.std() == 0 or b.std() == 0:
                continue
            r = float(np.corrcoef(a, b)[0, 1])
            corrs.append((pool_name, r))
        if not corrs:
            per_cand_corrs[name] = None
        else:
            # max |corr|
            best = max(corrs, key=lambda x: abs(x[1]))
            per_cand_corrs[name] = {"max_abs_corr": abs(best[1]),
                                    "top1": best[0],
                                    "all": corrs}

    # Forward returns
    fwd_returns = {}  # horizon -> (candidate_values[:-h], forward_ret[:-h]) accumulators returned as raw
    fwd_data = {}
    for horizon in [5, 20, 60]:
        fr = np.full(len(c), np.nan)
        fr[:-horizon] = (c[horizon:] - c[:-horizon]) / c[:-horizon] * 100
        fwd_data[horizon] = fr

    # Per-candidate decile-binned forward returns (per-ticker deciles from ticker's own distribution).
    # Returns dict[cand_name][horizon] = list of 10 lists, each containing forward returns in that decile.
    def bin_by_ticker_decile(values, fr):
        mask = np.isfinite(values) & np.isfinite(fr)
        if mask.sum() < 100:
            return [[] for _ in range(10)]
        v = values[mask]
        r = fr[mask]
        # per-ticker decile edges from this ticker's own values
        edges = np.percentile(v, np.linspace(0, 100, 11))
        # monotonic
        for i in range(1, len(edges)):
            if edges[i] <= edges[i-1]:
                edges[i] = edges[i-1] + 1e-9
        bins = np.digitize(v, edges[1:-1])
        out = []
        for d in range(10):
            sel = bins == d
            out.append(r[sel].astype(np.float32).tolist() if sel.any() else [])
        return out

    fwd_deciles = {}
    baseline_deciles = {}
    for name, _, _ in CANDIDATES:
        v = cand_values[name]
        fwd_deciles[name] = {}
        for horizon in [5, 20, 60]:
            fr = fwd_data[horizon]
            fwd_deciles[name][horizon] = bin_by_ticker_decile(v, fr)
        # baseline (120-version) from cache
        b120_name = BASELINE_120[name]
        idx = WORKER_POOL_IDX.get(b120_name)
        if idx is None:
            baseline_deciles[name] = None
            continue
        bv = cache_data[:, idx].astype(np.float32)
        baseline_deciles[name] = {}
        for horizon in [5, 20, 60]:
            fr = fwd_data[horizon]
            baseline_deciles[name][horizon] = bin_by_ticker_decile(bv, fr)

    return {
        "ticker": ticker,
        "corrs": per_cand_corrs,
        "fwd_deciles": fwd_deciles,
        "baseline_deciles": baseline_deciles,
    }


# Globals set in worker init
WORKER_POOL_IDX = None
WORKER_OHLCV = None


def _worker_init(pool_idx, ohlcv_path):
    global WORKER_POOL_IDX, WORKER_OHLCV
    WORKER_POOL_IDX = pool_idx
    with open(ohlcv_path, "rb") as f:
        WORKER_OHLCV = pickle.load(f)


def _worker_run(ticker):
    ohlcv = WORKER_OHLCV.get(ticker)
    if ohlcv is None:
        return {"ticker": ticker, "skipped": "no_ohlcv"}
    return process_ticker((ticker, ohlcv))


def main():
    t_start = time.time()
    print("Loading manifest...")
    with open(MANIFEST) as f:
        manifest = json.load(f)
    expr_names = manifest["expr_names"]
    name_to_idx = {n: i for i, n in enumerate(expr_names)}

    # Resolve pool
    pool_names = set()
    for name, _, _ in CANDIDATES:
        pool_names.update(short_family(name))
        pool_names.add(BASELINE_120[name])
    pool_names.update(COMMON_POOL)

    pool_idx = {}
    missing = []
    found = []
    for name in sorted(pool_names):
        i = name_to_idx.get(name)
        pool_idx[name] = i
        (found if i is not None else missing).append(name)
    print(f"Pool: {len(found)} found, {len(missing)} missing")
    print(f"  Found: {found}")
    print(f"  Missing: {missing}")

    print("Loading OHLCV pickle (main process peek for ticker list)...")
    with open(OHLCV_PKL, "rb") as f:
        ohlcv_peek = pickle.load(f)
    tickers = sorted(ohlcv_peek.keys())
    del ohlcv_peek
    print(f"Tickers in OHLCV: {len(tickers)}")

    # Run
    n_workers = 14
    results = []
    t_loop = time.time()
    print(f"Processing {len(tickers)} tickers with {n_workers} workers...")
    with ProcessPoolExecutor(max_workers=n_workers, initializer=_worker_init,
                              initargs=(pool_idx, OHLCV_PKL)) as ex:
        futures = {ex.submit(_worker_run, t): t for t in tickers}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            done += 1
            if done % 500 == 0:
                elapsed = time.time() - t_loop
                rate = done / elapsed
                eta = (len(tickers) - done) / rate
                print(f"  {done}/{len(tickers)}  rate={rate:.1f}/s  eta={eta:.0f}s")

    total_time = time.time() - t_start
    print(f"Done in {total_time:.1f}s")

    # Aggregate
    skipped = defaultdict(int)
    keep = []
    for r in results:
        if "skipped" in r:
            skipped[r["skipped"]] += 1
        else:
            keep.append(r)
    print(f"Kept: {len(keep)}, Skipped: {dict(skipped)}")

    # Correlation distribution per candidate
    corr_report = {}
    for name, _, _ in CANDIDATES:
        max_abs = []
        top1_counter = Counter()
        for r in keep:
            c = r["corrs"].get(name)
            if c is None:
                continue
            max_abs.append(c["max_abs_corr"])
            top1_counter[c["top1"]] += 1
        if not max_abs:
            corr_report[name] = None
            continue
        arr = np.array(max_abs)
        corr_report[name] = {
            "n": len(arr),
            "median": float(np.median(arr)),
            "p25": float(np.percentile(arr, 25)),
            "p75": float(np.percentile(arr, 75)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "top1_counts": top1_counter.most_common(5),
        }

    # Forward return deciles — per-ticker deciles, pooled returns per decile.
    # Report median + IQR (p25/p75) + p10/p90 per decile. Median is robust to outliers.
    def decile_stats(pooled):
        """pooled = list of 10 lists of returns. Returns list of (decile, N, median, p25, p75, p10, p90)."""
        out = []
        for d in range(10):
            arr = np.array(pooled[d], dtype=np.float32)
            if len(arr) == 0:
                out.append((d+1, 0, np.nan, np.nan, np.nan, np.nan, np.nan))
            else:
                out.append((d+1, len(arr),
                            float(np.median(arr)),
                            float(np.percentile(arr, 25)),
                            float(np.percentile(arr, 75)),
                            float(np.percentile(arr, 10)),
                            float(np.percentile(arr, 90))))
        return out

    fwd_report = {}
    for name, _, _ in CANDIDATES:
        fwd_report[name] = {}
        for horizon in [5, 20, 60]:
            cand_pooled = [[] for _ in range(10)]
            bl_pooled = [[] for _ in range(10)]
            for r in keep:
                fd = r.get("fwd_deciles", {}).get(name, {}).get(horizon)
                if fd is not None:
                    for d in range(10):
                        cand_pooled[d].extend(fd[d])
                bd = r.get("baseline_deciles", {})
                if bd is not None and bd.get(name) is not None:
                    bh = bd[name].get(horizon)
                    if bh is not None:
                        for d in range(10):
                            bl_pooled[d].extend(bh[d])
            fwd_report[name][horizon] = {
                "cand": decile_stats(cand_pooled),
                "baseline": decile_stats(bl_pooled),
            }

    # Write output
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    lines = []
    lines.append("# Long-lookback structural probe — results\n")
    lines.append(f"## Run parameters\n")
    lines.append(f"- Wall time: {total_time/60:.1f} min")
    lines.append(f"- Tickers kept: {len(keep)}")
    lines.append(f"- Tickers skipped: {dict(skipped)}")
    lines.append(f"- Min bars threshold: {MIN_BARS} + {MIN_FWD} forward = {MIN_BARS+MIN_FWD}")
    lines.append(f"- Candidates: {[c[0] for c in CANDIDATES]}\n")

    lines.append(f"## Pool expression resolution\n")
    lines.append(f"**Found in manifest ({len(found)}):** {found}\n")
    lines.append(f"**Missing from manifest ({len(missing)}):** {missing}\n")
    lines.append("Note: `retracement_level` computes identically to `range_position` in "
                 "this codebase (both are (close - minl_p)/(maxh_p - minl_p)).\n")

    lines.append(f"## Test A — Per-ticker max |correlation| with pool\n")
    lines.append("| Candidate | N | median | p25 | p75 | p95 | p99 | Top-5 most-frequent top-1 correlates |")
    lines.append("|-----------|---|--------|-----|-----|-----|-----|--------------------------------------|")
    for name, _, _ in CANDIDATES:
        rep = corr_report.get(name)
        if rep is None:
            lines.append(f"| {name} | 0 | — | — | — | — | — | — |")
            continue
        top_str = "; ".join(f"{nm}({cnt})" for nm, cnt in rep["top1_counts"])
        lines.append(f"| {name} | {rep['n']} | {rep['median']:.3f} | {rep['p25']:.3f} | "
                     f"{rep['p75']:.3f} | {rep['p95']:.3f} | {rep['p99']:.3f} | {top_str} |")
    lines.append("")

    lines.append(f"## Test B — Decile forward-return distributions (pooled)\n")
    for name, _, _ in CANDIDATES:
        lines.append(f"### {name}\n")
        for horizon in [5, 20, 60]:
            d = fwd_report.get(name, {}).get(horizon)
            if d is None:
                lines.append(f"**Horizon {horizon}bar:** no data\n")
                continue
            lines.append(f"**Horizon {horizon}bar:**\n")
            lines.append("Per-ticker deciles (each ticker binned in its own distribution); pooled returns; median + IQR.\n")
            lines.append("| Decile | Cand N | Cand median% | Cand p25% | Cand p75% | 120 median% | 120 p25% | 120 p75% |")
            lines.append("|--------|--------|--------------|-----------|-----------|-------------|----------|----------|")
            for i in range(10):
                cd = d["cand"][i]
                bl = d["baseline"][i] if d["baseline"] else (i+1, 0, np.nan, np.nan, np.nan, np.nan, np.nan)
                lines.append(f"| {cd[0]} | {cd[1]} | {cd[2]:.3f} | {cd[3]:.3f} | {cd[4]:.3f} | "
                             f"{bl[2]:.3f} | {bl[3]:.3f} | {bl[4]:.3f} |")
            # Decile-1 vs Decile-10 spread summary
            sp_cand = d["cand"][9][2] - d["cand"][0][2]
            sp_bl = (d["baseline"][9][2] - d["baseline"][0][2]) if d["baseline"] else np.nan
            lines.append(f"\n**Decile 10 minus Decile 1 median spread:** candidate={sp_cand:.3f}%  |  120-baseline={sp_bl:.3f}%\n")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
