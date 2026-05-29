import pickle
import json
import time
import heapq
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from numba import njit


REPO = Path(r"C:/Users/Dan/Documents/ScanPerfect/swing-screener")
OHLCV_PATH = REPO / "local_runner/cache/universe_ohlcv_daily.pkl"
OUT_DIR = REPO / "research" / "avatar_viability"
OUT_DIR.mkdir(exist_ok=True)

EXAMPLES = [
    {"setup": "DTSS", "klass": "fade",     "ticker": "PATH", "asof": "2025-12-05", "n_bars": 86},
    {"setup": "BF",   "klass": "breakout", "ticker": "ONDS", "asof": "2025-08-21", "n_bars": 49},
]

DTW_BAND = 5
TOP_N = 30
FADE_EXCLUDE_LAST = 10

TRADABLE_MIN_PRICE = 1.0
TRADABLE_MIN_DVOL = 4_000_000.0
TRADABLE_MIN_ADRP = 1.8


def compute_tradable(df):
    closes = df["close"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    dvols = df["dvol_20d"].values.astype(np.float64) if "dvol_20d" in df.columns else None

    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.where(lows > 0, highs / lows, np.nan)
    rolling = pd.Series(ratios).rolling(20, min_periods=1).mean().values
    adrp = (rolling - 1.0) * 100.0

    tradable = (closes >= TRADABLE_MIN_PRICE) & (~np.isnan(adrp)) & (adrp >= TRADABLE_MIN_ADRP)
    if dvols is not None:
        tradable &= (dvols >= TRADABLE_MIN_DVOL)
    return tradable


@njit(cache=True)
def breakout_filter_per_ticker(highs, lows, closes, volumes, end_idxs, n_bars):
    out = np.zeros(len(end_idxs), dtype=np.bool_)
    for k in range(len(end_idxs)):
        end = end_idxs[k]
        start = end - n_bars + 1
        max_high = -1e18
        anchor = start
        for i in range(start, end + 1):
            if highs[i] > max_high:
                max_high = highs[i]
                anchor = i
        num = 0.0
        den = 0.0
        for i in range(anchor, end + 1):
            typ = (highs[i] + lows[i] + closes[i]) / 3.0
            v = volumes[i]
            num += typ * v
            den += v
        if den <= 0.0:
            out[k] = False
        else:
            avwap = num / den
            out[k] = closes[end] <= avwap
    return out


@njit(cache=True)
def fade_filter_per_ticker(highs, closes, end_idxs, n_bars, exclude_last):
    out = np.zeros(len(end_idxs), dtype=np.bool_)
    for k in range(len(end_idxs)):
        end = end_idxs[k]
        start = end - n_bars + 1
        scope_end = end - exclude_last
        if scope_end < start:
            out[k] = False
            continue
        max_high = -1e18
        for i in range(start, scope_end + 1):
            if highs[i] > max_high:
                max_high = highs[i]
        out[k] = closes[end] <= max_high
    return out


@njit(cache=True)
def banded_dtw_cost(s1, s2, band):
    n = len(s1)
    INF = 1e18
    prev = np.full(n + 1, INF)
    curr = np.full(n + 1, INF)
    prev[0] = 0.0
    for i in range(1, n + 1):
        curr[:] = INF
        j_lo = max(1, i - band)
        j_hi = min(n, i + band)
        for j in range(j_lo, j_hi + 1):
            d = s1[i - 1] - s2[j - 1]
            d = d * d
            best = prev[j - 1]
            if prev[j] < best:
                best = prev[j]
            if curr[j - 1] < best:
                best = curr[j - 1]
            curr[j] = d + best
        prev, curr = curr, prev
    return prev[n]


@njit(cache=True)
def dtw_per_ticker(avatar, log_close, valid_end_idxs, n_bars, band):
    out = np.full(len(valid_end_idxs), 1e18)
    for k in range(len(valid_end_idxs)):
        end = valid_end_idxs[k]
        window = log_close[end - n_bars + 1 : end + 1]
        out[k] = banded_dtw_cost(avatar, window, band)
    return out


def vectorized_pearson(avatar, windows):
    a = avatar - avatar.mean()
    a_norm = np.linalg.norm(a)
    w = windows - windows.mean(axis=1, keepdims=True)
    w_norm = np.linalg.norm(w, axis=1)
    num = (w * a).sum(axis=1)
    denom = w_norm * a_norm
    out = np.full(len(windows), np.nan)
    ok = denom > 0
    out[ok] = num[ok] / denom[ok]
    return out


def build_window_matrix(log_close, valid_end_idxs, n_bars):
    out = np.empty((len(valid_end_idxs), n_bars), dtype=np.float64)
    for k, end in enumerate(valid_end_idxs):
        out[k] = log_close[end - n_bars + 1 : end + 1]
    return out


def greedy_nonoverlap(end_idxs, scores, n_bars, mode):
    if mode == "max":
        order = np.argsort(-scores)
    else:
        order = np.argsort(scores)
    used = np.zeros(len(end_idxs), dtype=bool)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append((int(end_idxs[i]), float(scores[i])))
        used |= np.abs(end_idxs - end_idxs[i]) < n_bars
    return kept


def scan(cache, avatar_log, n_bars, band, klass):
    pearson_heap = []
    dtw_heap = []
    n_tickers = 0
    n_candidates = 0
    n_filter_pass = 0
    n_dedup_pearson = 0
    n_dedup_dtw = 0

    t0 = time.time()
    for ticker, df in cache.items():
        if df is None or len(df) < n_bars + 20:
            continue
        n_tickers += 1
        if n_tickers % 1000 == 0:
            print(f"  {n_tickers} tickers, {n_candidates:,} cands, "
                  f"{n_filter_pass:,} pass-filter, {n_dedup_pearson:,} dedup-P, "
                  f"{n_dedup_dtw:,} dedup-D, {time.time()-t0:.0f}s")

        closes_raw = df["close"].values.astype(np.float64)
        highs_raw = df["high"].values.astype(np.float64)
        lows_raw = df["low"].values.astype(np.float64)
        volumes_raw = df["volume"].values.astype(np.float64)
        log_close = np.log(np.where(closes_raw > 0, closes_raw, np.nan))
        tradable = compute_tradable(df)

        n = len(log_close)
        end_idxs = np.arange(n_bars - 1, n)
        ok = tradable[end_idxs]
        if ok.sum() == 0:
            continue
        end_idxs = end_idxs[ok]

        windows = build_window_matrix(log_close, end_idxs, n_bars)
        finite_mask = np.isfinite(windows).all(axis=1)
        end_idxs = end_idxs[finite_mask]
        windows = windows[finite_mask]
        if len(end_idxs) == 0:
            continue

        n_candidates += len(end_idxs)

        end_idxs64 = end_idxs.astype(np.int64)
        if klass == "breakout":
            filt_pass = breakout_filter_per_ticker(highs_raw, lows_raw, closes_raw, volumes_raw,
                                                   end_idxs64, n_bars)
        elif klass == "fade":
            filt_pass = fade_filter_per_ticker(highs_raw, closes_raw, end_idxs64, n_bars, FADE_EXCLUDE_LAST)
        else:
            filt_pass = np.ones(len(end_idxs), dtype=np.bool_)

        if not filt_pass.any():
            continue
        end_idxs = end_idxs[filt_pass]
        windows = windows[filt_pass]
        n_filter_pass += len(end_idxs)

        end_idxs64 = end_idxs.astype(np.int64)
        rs = vectorized_pearson(avatar_log, windows)
        dtws = dtw_per_ticker(avatar_log, log_close, end_idxs64, n_bars, band)

        finite_r = np.isfinite(rs)
        if not finite_r.any():
            continue
        end_idxs_p = end_idxs[finite_r]
        rs_p = rs[finite_r]
        dtws_p = dtws[finite_r]

        pearson_keep = greedy_nonoverlap(end_idxs_p, rs_p, n_bars, mode="max")
        dtw_keep = greedy_nonoverlap(end_idxs_p, dtws_p, n_bars, mode="min")
        n_dedup_pearson += len(pearson_keep)
        n_dedup_dtw += len(dtw_keep)

        dates = df["date"].values
        for end_idx, r in pearson_keep:
            asof = dates[end_idx]
            asof_str = str(asof.astype("datetime64[D]")) if isinstance(asof, np.datetime64) else str(asof)[:10]
            if len(pearson_heap) < TOP_N:
                heapq.heappush(pearson_heap, (r, ticker, asof_str))
            elif r > pearson_heap[0][0]:
                heapq.heapreplace(pearson_heap, (r, ticker, asof_str))

        for end_idx, d in dtw_keep:
            asof = dates[end_idx]
            asof_str = str(asof.astype("datetime64[D]")) if isinstance(asof, np.datetime64) else str(asof)[:10]
            if len(dtw_heap) < TOP_N:
                heapq.heappush(dtw_heap, (-d, ticker, asof_str))
            elif -d > dtw_heap[0][0]:
                heapq.heapreplace(dtw_heap, (-d, ticker, asof_str))

    pearson_top = sorted(pearson_heap, key=lambda x: -x[0])
    dtw_top = sorted([(-mneg, t, a) for mneg, t, a in dtw_heap], key=lambda x: x[0])

    print(f"  scan complete: {n_tickers} tickers, {n_candidates:,} cands, "
          f"{n_filter_pass:,} pass-filter, {n_dedup_pearson:,} dedup-P, "
          f"{n_dedup_dtw:,} dedup-D, {time.time()-t0:.0f}s")
    return pearson_top, dtw_top


def render_grid(top_list, cache, n_bars, avatar_log, avatar_label, title, out_path, score_label):
    n = len(top_list)
    cols = 5
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.2, rows * 2.6))
    axes = axes.flatten() if rows > 1 else np.array([axes]).flatten()

    avatar_norm = avatar_log - avatar_log[-1]

    for i, item in enumerate(top_list):
        ax = axes[i]
        score = item[0]
        ticker = item[1]
        asof = item[2]
        df = cache[ticker]
        dates = df["date"].values
        idx = np.searchsorted(dates, np.datetime64(asof))
        window = np.log(df["close"].values[idx - n_bars + 1 : idx + 1])
        cand_norm = window - window[-1]
        x = np.arange(n_bars)
        ax.plot(x, avatar_norm, color="black", alpha=0.35, linewidth=1.2, label="avatar")
        ax.plot(x, cand_norm, color="blue", linewidth=1.5, label="candidate")
        ax.set_title(f"#{i+1}  {ticker}  {asof}\n{score_label}={score:.4f}", fontsize=8)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=7)
        if i == 0:
            ax.legend(fontsize=7, loc="best")

    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    plt.suptitle(title, fontsize=11, y=0.995)
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close()
    print(f"  saved {out_path}")


def main():
    print("Loading OHLCV pickle ...")
    t0 = time.time()
    with open(OHLCV_PATH, "rb") as f:
        cache = pickle.load(f)
    print(f"  {len(cache)} tickers, {time.time()-t0:.1f}s")

    print("\nWarming numba ...")
    t0 = time.time()
    a = np.linspace(0, 1, 49)
    b = np.linspace(0, 1, 49) + 0.01
    _ = banded_dtw_cost(a, b, 5)
    _ = dtw_per_ticker(a, np.concatenate([np.zeros(50), b]), np.array([98], dtype=np.int64), 49, 5)
    print(f"  warm: {time.time()-t0:.1f}s")

    for ex in EXAMPLES:
        setup = ex["setup"]
        ticker = ex["ticker"]
        asof = ex["asof"]
        n_bars = ex["n_bars"]
        print(f"\n=== {setup} avatar: {ticker} asof {asof}, N={n_bars} ===")

        df = cache[ticker]
        dates = df["date"].values
        idx = np.searchsorted(dates, np.datetime64(asof))
        if str(dates[idx])[:10] != asof:
            print(f"  ERROR: asof {asof} not exact in {ticker} (got {dates[idx]})")
            continue
        avatar_log = np.log(df["close"].values[idx - n_bars + 1 : idx + 1])

        pearson_top, dtw_top = scan(cache, avatar_log, n_bars, DTW_BAND, ex["klass"])

        results = {
            "setup": setup,
            "avatar_ticker": ticker,
            "avatar_asof": asof,
            "n_bars": n_bars,
            "dtw_band": DTW_BAND,
            "top_pearson": [{"rank": i + 1, "ticker": t, "asof": a, "r": float(r)}
                            for i, (r, t, a) in enumerate(pearson_top)],
            "top_dtw": [{"rank": i + 1, "ticker": t, "asof": a, "cost": float(c)}
                        for i, (c, t, a) in enumerate(dtw_top)],
        }
        json_path = OUT_DIR / f"{setup}_{ticker}_results.json"
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  saved {json_path}")

        avatar_label = f"{ticker} {asof}"
        render_grid(
            pearson_top, cache, n_bars, avatar_log, avatar_label,
            f"{setup} viability gate — Pearson — avatar: {ticker} {asof} (N={n_bars})",
            OUT_DIR / f"{setup}_{ticker}_pearson_top{TOP_N}.png",
            "r",
        )
        render_grid(
            dtw_top, cache, n_bars, avatar_log, avatar_label,
            f"{setup} viability gate — DTW (band ±{DTW_BAND}) — avatar: {ticker} {asof} (N={n_bars})",
            OUT_DIR / f"{setup}_{ticker}_dtw_top{TOP_N}.png",
            "cost",
        )


if __name__ == "__main__":
    main()
