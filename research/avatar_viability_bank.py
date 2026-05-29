import pickle
import json
import sqlite3
import time
import heapq
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from numba import njit


REPO = Path(r"C:/Users/Dan/Documents/ScanPerfect/swing-screener")
OHLCV_PATH = REPO / "local_runner/cache/universe_ohlcv_daily.pkl"
DB_PATH = REPO / "data/scanperfect.db"
OUT_DIR = REPO / "research" / "avatar_viability_bank"
OUT_DIR.mkdir(exist_ok=True)

SETUPS = [
    {"setup": "BF",   "klass": "breakout", "n_bars": 49},
    {"setup": "DTSS", "klass": "fade",     "n_bars": 86},
]

TOP_N = 30
TRADABLE_MIN_PRICE = 1.0
TRADABLE_MIN_DVOL = 4_000_000.0
TRADABLE_MIN_ADRP = 1.8
FADE_EXCLUDE_LAST = 10


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


def vectorized_max_pearson(avatars, windows):
    a = avatars - avatars.mean(axis=1, keepdims=True)
    a_norm = np.linalg.norm(a, axis=1)
    w = windows - windows.mean(axis=1, keepdims=True)
    w_norm = np.linalg.norm(w, axis=1)
    num = w @ a.T
    denom = np.outer(w_norm, a_norm)
    corr = np.where(denom > 0, num / denom, np.nan)
    if corr.size == 0:
        return np.full(len(windows), np.nan), np.zeros(len(windows), dtype=np.int64)
    nan_mask = np.isnan(corr)
    corr_for_max = np.where(nan_mask, -np.inf, corr)
    best_idx = corr_for_max.argmax(axis=1)
    max_corr = corr_for_max[np.arange(len(windows)), best_idx]
    max_corr = np.where(np.isfinite(max_corr), max_corr, np.nan)
    return max_corr, best_idx


def build_window_matrix(log_close, valid_end_idxs, n_bars):
    out = np.empty((len(valid_end_idxs), n_bars), dtype=np.float64)
    for k, end in enumerate(valid_end_idxs):
        out[k] = log_close[end - n_bars + 1 : end + 1]
    return out


def greedy_nonoverlap(end_idxs, scores, n_bars):
    order = np.argsort(-scores)
    used = np.zeros(len(end_idxs), dtype=bool)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append((int(end_idxs[i]), float(scores[i]), int(i)))
        used |= np.abs(end_idxs - end_idxs[i]) < n_bars
    return kept


def load_bank(setup, cache, n_bars):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY entry_date",
        (setup.lower(),)
    ).fetchall()
    conn.close()

    bank = []
    skipped = []
    for ticker, entry_date in rows:
        if ticker not in cache:
            skipped.append((ticker, entry_date, "no OHLCV"))
            continue
        df = cache[ticker]
        dates = df["date"].values
        entry_np = np.datetime64(entry_date[:10])
        idx = np.searchsorted(dates, entry_np)
        if idx >= len(dates) or str(dates[idx])[:10] != entry_date[:10]:
            skipped.append((ticker, entry_date, "entry date not in OHLCV"))
            continue
        asof_idx = idx - 1
        if asof_idx - n_bars + 1 < 0:
            skipped.append((ticker, entry_date, "insufficient history"))
            continue
        closes = df["close"].values[asof_idx - n_bars + 1 : asof_idx + 1]
        if (closes <= 0).any():
            skipped.append((ticker, entry_date, "non-positive close"))
            continue
        log_close = np.log(closes.astype(np.float64))
        if not np.isfinite(log_close).all():
            skipped.append((ticker, entry_date, "nan close"))
            continue
        bank.append({
            "ticker": ticker,
            "entry_date": entry_date[:10],
            "asof": str(dates[asof_idx])[:10],
            "log_close": log_close,
        })
    return bank, skipped


def scan(cache, avatars, n_bars, klass, bank_pairs):
    pearson_heap = []
    n_tickers = 0
    n_candidates = 0
    n_filter_pass = 0
    n_dedup = 0

    t0 = time.time()
    for ticker, df in cache.items():
        if df is None or len(df) < n_bars + 20:
            continue
        n_tickers += 1
        if n_tickers % 1000 == 0:
            print(f"  {n_tickers} tickers, {n_candidates:,} cands, "
                  f"{n_filter_pass:,} pass-filter, {n_dedup:,} dedup, {time.time()-t0:.0f}s")

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

        max_r, best_avatar = vectorized_max_pearson(avatars, windows)
        finite = np.isfinite(max_r)
        if not finite.any():
            continue
        end_idxs_f = end_idxs[finite]
        max_r_f = max_r[finite]
        best_avatar_f = best_avatar[finite]

        kept = greedy_nonoverlap(end_idxs_f, max_r_f, n_bars)
        n_dedup += len(kept)

        dates = df["date"].values
        for end_idx, r, src_idx in kept:
            asof = dates[end_idx]
            asof_str = str(asof.astype("datetime64[D]")) if isinstance(asof, np.datetime64) else str(asof)[:10]
            if (ticker, asof_str) in bank_pairs:
                continue
            best_av_idx = int(best_avatar_f[src_idx])
            if len(pearson_heap) < TOP_N:
                heapq.heappush(pearson_heap, (r, ticker, asof_str, best_av_idx))
            elif r > pearson_heap[0][0]:
                heapq.heapreplace(pearson_heap, (r, ticker, asof_str, best_av_idx))

    pearson_top = sorted(pearson_heap, key=lambda x: -x[0])
    print(f"  scan complete: {n_tickers} tickers, {n_candidates:,} cands, "
          f"{n_filter_pass:,} pass-filter, {n_dedup:,} dedup, {time.time()-t0:.0f}s")
    return pearson_top


def render_grid(top_list, cache, n_bars, bank, title, out_path):
    n = len(top_list)
    cols = 5
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.2, rows * 2.6))
    axes = axes.flatten() if rows > 1 else np.array([axes]).flatten()

    for i, (r, ticker, asof, best_av_idx) in enumerate(top_list):
        ax = axes[i]
        df = cache[ticker]
        dates = df["date"].values
        idx = np.searchsorted(dates, np.datetime64(asof))
        cand_log = np.log(df["close"].values[idx - n_bars + 1 : idx + 1])
        cand_norm = cand_log - cand_log[-1]

        bank_av_log = bank[best_av_idx]["log_close"]
        bank_av_norm = bank_av_log - bank_av_log[-1]
        bank_label = f"{bank[best_av_idx]['ticker']} {bank[best_av_idx]['asof']}"

        x = np.arange(n_bars)
        ax.plot(x, bank_av_norm, color="black", alpha=0.35, linewidth=1.2, label=f"avatar: {bank_label}")
        ax.plot(x, cand_norm, color="blue", linewidth=1.5, label="candidate")
        ax.set_title(f"#{i+1}  {ticker}  {asof}\nr={r:.4f}  vs  {bank_label}", fontsize=8)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=7)
        if i == 0:
            ax.legend(fontsize=6, loc="best")

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

    for setup_def in SETUPS:
        setup = setup_def["setup"]
        klass = setup_def["klass"]
        n_bars = setup_def["n_bars"]

        print(f"\n=== {setup} bank avatar (1-NN), N={n_bars}, klass={klass} ===")
        bank, skipped = load_bank(setup, cache, n_bars)
        print(f"  bank: {len(bank)} usable examples, {len(skipped)} skipped")
        for t, d, why in skipped:
            print(f"    skipped: {t} {d}: {why}")

        if not bank:
            print("  no bank, abort")
            continue

        avatars = np.stack([b["log_close"] for b in bank])
        print(f"  avatars matrix: {avatars.shape}")

        bank_pairs = {(b["ticker"], b["asof"]) for b in bank}
        pearson_top = scan(cache, avatars, n_bars, klass, bank_pairs)

        results = {
            "setup": setup,
            "klass": klass,
            "n_bars": n_bars,
            "bank_size": len(bank),
            "bank": [{"ticker": b["ticker"], "asof": b["asof"], "entry_date": b["entry_date"]} for b in bank],
            "top_pearson": [
                {"rank": i + 1, "ticker": t, "asof": a, "r": float(r),
                 "best_avatar": f"{bank[av_idx]['ticker']} {bank[av_idx]['asof']}"}
                for i, (r, t, a, av_idx) in enumerate(pearson_top)
            ],
        }
        json_path = OUT_DIR / f"{setup}_bank_results.json"
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  saved {json_path}")

        render_grid(
            pearson_top, cache, n_bars, bank,
            f"{setup} bank 1-NN — N={n_bars}, bank size={len(bank)}",
            OUT_DIR / f"{setup}_bank_pearson_top{TOP_N}.png",
        )


if __name__ == "__main__":
    main()
