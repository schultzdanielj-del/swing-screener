"""Weekly-scale MA-corridor heatmap diagnostic for all 5 setups.
Combines each setup's daily heatmap (from research/presignal_ma_heatmap/)
with the new weekly heatmap into a single combined image, overwriting the
daily-only PNG at research/presignal_ma_heatmap/{setup}_heatmap.png.

Weekly anchor: close[E-1] per Dan's weekly rule. Feature at (MA m, weekly
offset k) = log(MA_m(anchor_week - k) / close[E-1]). Offset 0 = anchor week.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
DAILY_HM_DIR = os.path.join(WORKTREE, "research", "presignal_ma_heatmap")
OUT_DIR_WEEKLY = os.path.join(WORKTREE, "research", "presignal_weekly_ma_heatmap")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]

# Daily MA basis (must match the daily heatmap script's config)
DAILY_MA_PERIODS = [5, 10, 20, 50, 100, 200]
DAILY_MA_TYPES = [f"SMA{p}" for p in DAILY_MA_PERIODS] + [f"EMA{p}" for p in DAILY_MA_PERIODS]

# Weekly MA basis (periods in weeks)
WEEKLY_MA_PERIODS = [5, 10, 20, 50, 100, 200]
WEEKLY_MA_TYPES = [f"SMA{p}w" for p in WEEKLY_MA_PERIODS] + [f"EMA{p}w" for p in WEEKLY_MA_PERIODS]

DATE_CUTOFF = "2020-01-02"
N_MARKET_SAMPLE = 10000
SEED = 42
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def compute_mas_series(series, periods):
    """series: (n,) 1-D close-like array. periods: list of periods.
    Returns (2*len(periods), n) SMA rows followed by EMA rows.
    """
    L = len(series)
    c = np.where(np.isfinite(series) & (series > 0), series, np.nan)
    out = np.full((2 * len(periods), L), np.nan, dtype=np.float64)
    cumsum_c = np.cumsum(np.where(np.isfinite(c), c, 0.0))
    cumsum_ok = np.cumsum(np.isfinite(c).astype(np.int64))
    for i, p in enumerate(periods):
        if L < p:
            continue
        num = cumsum_c[p - 1:] - np.concatenate([[0.0], cumsum_c[:L - p]])
        count = cumsum_ok[p - 1:] - np.concatenate([[0], cumsum_ok[:L - p]])
        sma = np.where(count == p, num / p, np.nan)
        out[i, p - 1:L] = sma
    for i, p in enumerate(periods):
        alpha = 2.0 / (p + 1.0)
        ema = np.full(L, np.nan)
        first = -1
        for t in range(L):
            if np.isfinite(c[t]):
                first = t
                break
        if first < 0:
            continue
        ema[first] = c[first]
        for t in range(first + 1, L):
            v = c[t]
            prev = ema[t - 1]
            if np.isfinite(v) and np.isfinite(prev):
                ema[t] = alpha * v + (1 - alpha) * prev
            elif np.isfinite(v):
                ema[t] = v
            else:
                ema[t] = prev
        out[len(periods) + i, :] = ema
    return out


def weekly_feature_vec(df, E_idx, W_N):
    """(n_MA, W_N) feature matrix on weekly basis. NaN-lenient."""
    wb = wagg.compute_weekly_back(df, E_idx, W_max_target=None)
    if wb is None or len(wb) < 2:
        return None
    anchor_close = wb[0]
    weekly_chrono = wb[::-1]  # oldest first, anchor last
    mas = compute_mas_series(weekly_chrono, WEEKLY_MA_PERIODS)
    anchor_idx_chrono = len(weekly_chrono) - 1
    log_anchor = np.log(anchor_close)
    feat = np.full((mas.shape[0], W_N), np.nan, dtype=np.float64)
    for k in range(W_N):
        chrono_idx = anchor_idx_chrono - k
        if chrono_idx < 0:
            continue
        vals = mas[:, chrono_idx]
        ok = np.isfinite(vals) & (vals > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            feat[ok, k] = np.log(vals[ok]) - log_anchor
    return feat


def collect_example_weekly_feats(setup, universe, W_N):
    examples = get_examples(setup)
    feats = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        f = weekly_feature_vec(df, E, W_N)
        if f is None:
            continue
        feats.append(f)
    return np.array(feats) if feats else None


def sample_market_weekly(universe, n_target, rng, W_N):
    tickers = list(universe.keys())
    feats = []
    attempts = 0
    max_attempts = n_target * 5
    while len(feats) < n_target and attempts < max_attempts:
        t = tickers[rng.integers(0, len(tickers))]
        df = universe[t]
        close = df["close"].values
        L = len(close)
        if L < 10:
            attempts += 1
            continue
        ds = vsc.dates_as_str(df)
        eligible_idx = np.where((np.arange(L) >= 1) & (ds >= DATE_CUTOFF))[0]
        if len(eligible_idx) == 0:
            attempts += 1
            continue
        E = int(eligible_idx[rng.integers(0, len(eligible_idx))])
        f = weekly_feature_vec(df, E, W_N)
        if f is None or not np.any(np.isfinite(f)):
            attempts += 1
            continue
        feats.append(f)
        attempts += 1
    return np.array(feats)


def compute_reject_heatmap(ex_feats, mkt_feats):
    with np.errstate(invalid='ignore'):
        lo = np.nanmin(ex_feats, axis=0)
        hi = np.nanmax(ex_feats, axis=0)
    n_MA = ex_feats.shape[1]
    W = ex_feats.shape[2]
    reject = np.full((n_MA, W), np.nan)
    for m in range(n_MA):
        for k in range(W):
            if not (np.isfinite(lo[m, k]) and np.isfinite(hi[m, k])):
                continue
            vals = mkt_feats[:, m, k]
            finite = np.isfinite(vals)
            if finite.sum() == 0:
                continue
            in_cor = (vals[finite] >= lo[m, k]) & (vals[finite] <= hi[m, k])
            reject[m, k] = 1.0 - in_cor.mean()
    return reject


def load_daily_heatmap(setup):
    """Return (n_MA, n_offsets) daily reject matrix from the earlier daily run."""
    path = os.path.join(DAILY_HM_DIR, "heatmap_results.json")
    if not os.path.exists(path):
        return None, None
    all_results = json.load(open(path))
    if setup not in all_results:
        return None, None
    r = all_results[setup]
    reject = np.array(r["reject_rate_grid"])
    max_offset = r["max_offset"]
    return reject, max_offset


def render_combined(setup, daily_reject, daily_max_offset, weekly_reject, W_N, out_path):
    """Two-panel image: daily top, weekly bottom. Save to out_path."""
    fig, axs = plt.subplots(2, 1, figsize=(14, 11),
                            gridspec_kw={"height_ratios": [1, 1]})
    # Daily panel
    ax = axs[0]
    if daily_reject is not None:
        im_d = ax.imshow(daily_reject, aspect="auto", cmap="viridis", vmin=0, vmax=1)
        ax.set_yticks(range(len(DAILY_MA_TYPES)))
        ax.set_yticklabels(DAILY_MA_TYPES, fontsize=8)
        d_step = max(1, daily_max_offset // 8)
        ax.set_xticks(range(0, daily_max_offset, d_step))
        ax.set_xticklabels([f"E-{k}" for k in range(0, daily_max_offset, d_step)], fontsize=8)
        cbar_d = plt.colorbar(im_d, ax=ax)
        cbar_d.set_label("reject rate (daily)", fontsize=8)
        # Mark top-5 meat
        flat = daily_reject.copy()
        flat[~np.isfinite(flat)] = -1
        top5 = np.argsort(flat.ravel())[::-1][:5]
        for fi in top5:
            r, c = np.unravel_index(fi, daily_reject.shape)
            if np.isfinite(daily_reject[r, c]):
                ax.plot(c, r, "r*", markersize=12, markeredgecolor="white")
        ax.set_title(f"{setup.upper()} — DAILY MA corridor reject rate (dark=meat, light=fat)",
                     fontsize=10)
        ax.set_xlabel("daily bar offset from entry")
        ax.set_ylabel("MA type (daily)")
    else:
        ax.text(0.5, 0.5, f"no daily heatmap available for {setup}",
                transform=ax.transAxes, ha="center", va="center")

    # Weekly panel
    ax = axs[1]
    im_w = ax.imshow(weekly_reject, aspect="auto", cmap="viridis", vmin=0, vmax=1)
    ax.set_yticks(range(len(WEEKLY_MA_TYPES)))
    ax.set_yticklabels(WEEKLY_MA_TYPES, fontsize=8)
    w_step = max(1, W_N // 8)
    ax.set_xticks(range(0, W_N, w_step))
    ax.set_xticklabels([f"W-{k}" for k in range(0, W_N, w_step)], fontsize=8)
    cbar_w = plt.colorbar(im_w, ax=ax)
    cbar_w.set_label("reject rate (weekly)", fontsize=8)
    flat = weekly_reject.copy()
    flat[~np.isfinite(flat)] = -1
    top5 = np.argsort(flat.ravel())[::-1][:5]
    for fi in top5:
        r, c = np.unravel_index(fi, weekly_reject.shape)
        if np.isfinite(weekly_reject[r, c]):
            ax.plot(c, r, "r*", markersize=12, markeredgecolor="white")
    ax.set_title(f"{setup.upper()} — WEEKLY MA corridor reject rate (W_N={W_N})  (dark=meat, light=fat)",
                 fontsize=10)
    ax.set_xlabel("weekly offset from entry (W-0 = anchor week)")
    ax.set_ylabel("MA type (weekly)")

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close(fig)


def main():
    os.makedirs(OUT_DIR_WEEKLY, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print(f"Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print("FAIL"); sys.exit(1)

    results = {}
    for setup in SETUPS:
        stage_a = json.load(open(os.path.join(STAGE_A_DIR, f"{setup}_stage_a.json")))
        W_N = stage_a["W_N"]
        print(f"\n=== {setup.upper()}  W_N={W_N}", flush=True)

        ex_feats = collect_example_weekly_feats(setup, universe, W_N)
        if ex_feats is None:
            print(f"  no examples; skip"); continue
        print(f"  examples: {len(ex_feats)}  shape {ex_feats.shape}", flush=True)
        t0 = time.time()
        mkt_feats = sample_market_weekly(universe, N_MARKET_SAMPLE, rng, W_N)
        print(f"  market vectors: {len(mkt_feats):,}  ({time.time()-t0:.1f}s)", flush=True)
        weekly_reject = compute_reject_heatmap(ex_feats, mkt_feats)
        valid = np.isfinite(weekly_reject).sum()
        if valid:
            print(f"  weekly reject: min={np.nanmin(weekly_reject):.3f}  "
                  f"median={np.nanmedian(weekly_reject):.3f}  max={np.nanmax(weekly_reject):.3f}", flush=True)
            flat = weekly_reject.copy()
            flat[~np.isfinite(flat)] = -1
            top5 = np.argsort(flat.ravel())[::-1][:5]
            print(f"  weekly top-5 meat:", flush=True)
            for fi in top5:
                r, c = np.unravel_index(fi, weekly_reject.shape)
                if np.isfinite(weekly_reject[r, c]):
                    print(f"    {WEEKLY_MA_TYPES[r]:>9}  W-{c:<3}  reject={weekly_reject[r, c]:.3f}", flush=True)

        # Merge with daily and render combined image
        daily_reject, daily_max_offset = load_daily_heatmap(setup)
        combined_path = os.path.join(DAILY_HM_DIR, f"{setup}_heatmap.png")
        render_combined(setup, daily_reject, daily_max_offset, weekly_reject, W_N, combined_path)
        print(f"  wrote combined daily+weekly image: {combined_path}", flush=True)

        results[setup] = {
            "W_N": W_N,
            "n_ex": int(len(ex_feats)),
            "n_mkt": int(len(mkt_feats)),
            "MA_types": WEEKLY_MA_TYPES,
            "reject_rate_grid": weekly_reject.tolist(),
        }

    with open(os.path.join(OUT_DIR_WEEKLY, "weekly_heatmap_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote weekly data to {OUT_DIR_WEEKLY}", flush=True)
    print(f"Combined images at {DAILY_HM_DIR}/{{setup}}_heatmap.png", flush=True)


if __name__ == "__main__":
    main()
