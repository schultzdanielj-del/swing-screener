"""Test whether MOC expressions carry signal for any downstream grinder.

Two tests per setup class (htf, bf, base):
  1. Signal discriminator: KS statistic between example-bar MOC values and
     universe-null MOC values for each of 42 expressions.
  2. Forward-outcome predictor: Spearman rank correlation between each of
     42 MOC expressions (at signal bar) and each of 6 forward outcomes
     (move_5d, move_10d, move_20d, mfe_10bar, mae_10bar, cap_eff_10bar),
     ADRP20-normalized.

Output: one PNG per setup class with two-panel summary — KS bar chart
(signal separation) + Spearman heatmap (outcome prediction).

No sample-size floor; NaN pairs dropped pairwise per test. Universe-null
bars are drawn randomly from the OHLCV cache over the same date range as
examples, with a loose tradable filter (close >= $1, ADRP20 >= 1.8%,
$vol20 >= $4M — matching SIGNAL_GRINDER tradable spec).
"""
import os
import pickle
import random
import sqlite3
import sys
import time
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp, spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE_OHLCV = os.path.join(ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
EXAMPLES_DB = os.path.join(ROOT, "data", "scanperfect.db")

SETUP_CLASSES = ["htf", "bf", "base"]

RVOL_WINDOW = 50
ATR_WINDOW = 14
TOLERANCE_FRAC = np.sqrt(1.0 / 78.0)
RVOL_BIRTH_MIN = 1.0
ADRP_WINDOW = 20

NULL_BARS_PER_TICKER = 20
NULL_N_TICKERS = 150  # ~3000 null bars target
MIN_TRADABLE_ADRP = 1.8
MIN_TRADABLE_DOLLAR_VOL = 4_000_000
MIN_CLOSE = 1.0

FORWARD_WINDOWS = {"move_5d": 5, "move_10d": 10, "move_20d": 20}
MFE_MAE_WINDOW = 10

RANDOM_SEED = 42

# 42 expression names in emission order
EXPR_SLOTS = [f"above_{i}" for i in (1, 2, 3)] + [f"below_{i}" for i in (1, 2, 3)]
EXPR_FEATURES = [
    "distance", "stack_weight", "stack_count",
    "max_contributor_rvol", "cross_count",
    "bars_since_birth", "bars_since_last_contribution",
]
EXPR_NAMES = [f"{slot}_{feat}" for slot in EXPR_SLOTS for feat in EXPR_FEATURES]
assert len(EXPR_NAMES) == 42


def compute_rvol(volume):
    sma = pd.Series(volume).rolling(RVOL_WINDOW, min_periods=1).mean().values
    out = volume / sma
    out[~np.isfinite(out)] = 0.0
    return out


def compute_atr14(high, low, close):
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    return pd.Series(tr).rolling(ATR_WINDOW, min_periods=1).mean().values


def compute_adrp20(high, low):
    ratio = high / np.where(low > 0, low, np.nan)
    return (pd.Series(ratio).rolling(ADRP_WINDOW, min_periods=1).mean().values - 1) * 100


def compute_dollar_vol20(close, volume):
    dv = close * volume
    return pd.Series(dv).rolling(ADRP_WINDOW, min_periods=1).mean().values


def build_moc_and_snapshots(high, low, close, volume, target_bars):
    """Walk D1 bars building MOC levels. At each bar in target_bars, emit the
    42-expression vector for that bar. Returns {bar_idx: np.ndarray(42)}.
    """
    n = len(close)
    target_set = set(int(b) for b in target_bars if 0 <= b < n)
    if not target_set:
        return {}

    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * TOLERANCE_FRAC

    levels = []
    snapshots = {}

    for t in range(n):
        rv = rvol[t]
        tol_t = tol[t]
        ct = close[t]

        if rv > RVOL_BIRTH_MIN:
            w = max(0.0, rv - 1.0)
            for price in (high[t], low[t]):
                matched_lvl = None
                for lvl in levels:
                    if abs(lvl["price"] - price) <= tol_t:
                        matched_lvl = lvl
                        break
                if matched_lvl is None:
                    levels.append({
                        "price": price, "birth_bar": t,
                        "stack_weight": w, "stack_count": 1,
                        "max_contributor_rvol": rv, "cross_count": 0,
                        "last_contribution_bar": t, "last_close_side": None,
                    })
                else:
                    matched_lvl["stack_weight"] += w
                    matched_lvl["stack_count"] += 1
                    if rv > matched_lvl["max_contributor_rvol"]:
                        matched_lvl["max_contributor_rvol"] = rv
                    matched_lvl["last_contribution_bar"] = t

        for lvl in levels:
            if t <= lvl["birth_bar"]:
                continue
            dist = ct - lvl["price"]
            if dist > tol_t:
                curr_side = 1
            elif dist < -tol_t:
                curr_side = -1
            else:
                curr_side = 0
            if lvl["last_close_side"] is not None and curr_side != 0:
                if curr_side != lvl["last_close_side"] and lvl["last_close_side"] != 0:
                    lvl["cross_count"] += 1
            if curr_side != 0:
                lvl["last_close_side"] = curr_side

        if t in target_set:
            snapshots[t] = snapshot_42(levels, ct, atr[t], t)

    return snapshots


def snapshot_42(levels, close_t, atr_t, bar_idx):
    """Top-3-above + top-3-below current close by stack_weight. Emit 42-vec."""
    above = [l for l in levels if l["price"] > close_t]
    below = [l for l in levels if l["price"] < close_t]
    above.sort(key=lambda l: -l["stack_weight"])
    below.sort(key=lambda l: -l["stack_weight"])
    top = (above[:3] + [None] * 3)[:3] + (below[:3] + [None] * 3)[:3]
    vec = np.full(42, np.nan, dtype=np.float64)
    for i, lvl in enumerate(top):
        if lvl is None:
            continue
        base = i * 7
        atr_safe = atr_t if atr_t and atr_t > 0 else np.nan
        vec[base + 0] = (lvl["price"] - close_t) / atr_safe
        vec[base + 1] = lvl["stack_weight"]
        vec[base + 2] = lvl["stack_count"]
        vec[base + 3] = lvl["max_contributor_rvol"]
        vec[base + 4] = lvl["cross_count"]
        vec[base + 5] = bar_idx - lvl["birth_bar"]
        vec[base + 6] = bar_idx - lvl["last_contribution_bar"]
    return vec


def load_examples():
    conn = sqlite3.connect(EXAMPLES_DB)
    c = conn.cursor()
    c.execute("SELECT setup_type, ticker, entry_date FROM examples")
    rows = [(s, t, pd.to_datetime(d)) for s, t, d in c.fetchall()]
    conn.close()
    return rows


def resolve_signal_bar(df, entry_date):
    """Signal bar = last bar strictly before entry_date. Returns bar index or None."""
    if isinstance(df.index, pd.DatetimeIndex):
        dates = df.index
    elif "date" in df.columns:
        dates = pd.to_datetime(df["date"])
    else:
        return None
    mask = dates < entry_date
    if not mask.any():
        return None
    return int(np.where(mask)[0][-1])


def get_dates(df):
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index
    if "date" in df.columns:
        return pd.to_datetime(df["date"])
    return None


def compute_forward_outcomes(high, low, close, signal_bar, adrp20):
    """Return dict of outcome name -> ADRP20-normalized value. NaN if out of range."""
    n = len(close)
    c0 = close[signal_bar]
    adr = adrp20[signal_bar]
    if not (adr and adr > 0 and np.isfinite(adr)):
        return {k: np.nan for k in list(FORWARD_WINDOWS) +
                ["mfe_10bar", "mae_10bar", "cap_eff_10bar"]}
    out = {}
    for name, w in FORWARD_WINDOWS.items():
        end = signal_bar + w
        if end >= n:
            out[name] = np.nan
        else:
            pct = (close[end] / c0 - 1) * 100
            out[name] = pct / adr
    # MFE/MAE over 10 bars AFTER signal (bars signal+1 .. signal+10)
    end10 = signal_bar + MFE_MAE_WINDOW
    if end10 >= n:
        out["mfe_10bar"] = np.nan
        out["mae_10bar"] = np.nan
        out["cap_eff_10bar"] = np.nan
    else:
        window_hi = high[signal_bar + 1: end10 + 1]
        window_lo = low[signal_bar + 1: end10 + 1]
        mfe_pct = (window_hi.max() / c0 - 1) * 100
        mae_pct = (window_lo.min() / c0 - 1) * 100
        move10_pct = (close[end10] / c0 - 1) * 100
        out["mfe_10bar"] = mfe_pct / adr
        out["mae_10bar"] = mae_pct / adr
        out["cap_eff_10bar"] = (move10_pct / mfe_pct) if mfe_pct > 0 else np.nan
    return out


def sample_universe_nulls(daily_cache, example_tickers, example_date_range, rng):
    """Pick NULL_N_TICKERS tickers (excluding example-pile tickers) and
    NULL_BARS_PER_TICKER bars per ticker meeting the tradable filter.
    Returns list of (ticker, bar_idx) tuples.
    """
    candidate_tickers = [t for t in daily_cache.keys() if t not in example_tickers]
    rng.shuffle(candidate_tickers)

    earliest_dt, latest_dt = example_date_range
    null_pairs = []

    for ticker in candidate_tickers:
        if len(null_pairs) >= NULL_N_TICKERS * NULL_BARS_PER_TICKER:
            break
        df = daily_cache[ticker]
        dates = get_dates(df)
        if dates is None or len(df) < ADRP_WINDOW + 5:
            continue
        close = df["close"].values.astype(float)
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        vol = df["volume"].values.astype(float)
        adrp = compute_adrp20(high, low)
        dv = compute_dollar_vol20(close, vol)
        tradable = (close >= MIN_CLOSE) & \
                   (adrp >= MIN_TRADABLE_ADRP) & \
                   (dv >= MIN_TRADABLE_DOLLAR_VOL)
        date_mask = (dates >= earliest_dt) & (dates <= latest_dt)
        eligible = np.where(tradable & date_mask.values)[0]
        if len(eligible) < 2:
            continue
        k = min(NULL_BARS_PER_TICKER, len(eligible))
        picks = rng.sample(list(eligible), k)
        for p in picks:
            null_pairs.append((ticker, int(p)))
    return null_pairs


def build_features_for_bars(daily_cache, ticker_to_bars):
    """For each ticker -> list of target bars, run MOC builder once and emit
    42-vec per bar. Returns dict[(ticker, bar)] -> np.ndarray(42).
    """
    out = {}
    total = sum(len(b) for b in ticker_to_bars.values())
    done = 0
    t_start = time.time()
    for ticker, bars in ticker_to_bars.items():
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        snapshots = build_moc_and_snapshots(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
            bars,
        )
        for b, vec in snapshots.items():
            out[(ticker, b)] = vec
        done += len(bars)
    print(f"    built MOC features for {done}/{total} bars across "
          f"{len(ticker_to_bars)} tickers in {time.time()-t_start:.1f}s")
    return out


def render_summary(setup, ks_stats, spearman_matrix, outcome_names,
                   n_examples, n_nulls, out_path):
    fig, axes = plt.subplots(1, 2, figsize=(18, 14),
                             gridspec_kw={"width_ratios": [1, 1.4]})

    # Signal-separation KS bar chart
    ax = axes[0]
    y = np.arange(len(EXPR_NAMES))
    ks_vals = ks_stats
    order = np.argsort(-np.nan_to_num(ks_vals, nan=-1))
    colors = ["crimson" if v > 0.15 else "steelblue" for v in ks_vals[order]]
    ax.barh(y, ks_vals[order], color=colors, edgecolor="black", linewidth=0.3)
    ax.set_yticks(y)
    ax.set_yticklabels([EXPR_NAMES[i] for i in order], fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("KS statistic (examples vs universe null)", fontsize=9)
    ax.set_title(f"Signal separation — {setup}\n"
                 f"n_examples={n_examples}, n_null={n_nulls} | "
                 f"red bar = KS > 0.15 (meaningful)",
                 fontsize=10)
    ax.axvline(0.15, color="red", linestyle="--", linewidth=0.8, alpha=0.7)
    ax.grid(True, axis="x", alpha=0.25)

    # Spearman heatmap (reorder rows to match KS order for visual alignment)
    ax = axes[1]
    mat = spearman_matrix[order, :]
    im = ax.imshow(mat, cmap="RdBu_r", vmin=-0.4, vmax=0.4, aspect="auto")
    ax.set_xticks(np.arange(len(outcome_names)))
    ax.set_xticklabels(outcome_names, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels([EXPR_NAMES[i] for i in order], fontsize=7)
    ax.set_title(f"Forward-outcome Spearman ρ — {setup}\n"
                 f"n={n_examples} | positive ρ (red) = higher MOC value → higher outcome",
                 fontsize=10)
    # annotate each cell
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:+.2f}", ha="center", va="center",
                        fontsize=6,
                        color="white" if abs(v) > 0.22 else "black")
    fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Spearman ρ")

    fig.suptitle(f"MOC usefulness test — setup: {setup.upper()}",
                 fontsize=13, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def main():
    rng = random.Random(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    print(f"Loading {CACHE_OHLCV}")
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)
    print(f"  {len(daily_cache)} tickers in cache")

    print("\nLoading examples from SQLite")
    all_examples = load_examples()
    breakouts = [(s, t, d) for s, t, d in all_examples if s in SETUP_CLASSES]
    print(f"  total examples: {len(all_examples)} | "
          f"breakouts: {len(breakouts)}")
    for cls in SETUP_CLASSES:
        n = sum(1 for s, _, _ in breakouts if s == cls)
        print(f"    {cls}: {n}")

    # Resolve signal bars per example
    print("\nResolving signal bars + forward outcomes per example")
    resolved = []
    dropped = 0
    for setup, ticker, entry_date in breakouts:
        if ticker not in daily_cache:
            dropped += 1
            continue
        df = daily_cache[ticker]
        sb = resolve_signal_bar(df, entry_date)
        if sb is None:
            dropped += 1
            continue
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        adrp = compute_adrp20(high, low)
        outcomes = compute_forward_outcomes(high, low, close, sb, adrp)
        resolved.append({
            "setup": setup, "ticker": ticker, "entry_date": entry_date,
            "signal_bar": sb, "outcomes": outcomes,
        })
    print(f"  resolved: {len(resolved)} | dropped: {dropped}")

    # Compute example date range for null sampling
    signal_dates = []
    for r in resolved:
        df = daily_cache[r["ticker"]]
        dates = get_dates(df)
        if dates is not None:
            signal_dates.append(dates[r["signal_bar"]])
    date_range = (min(signal_dates), max(signal_dates))
    print(f"  example signal-date range: {date_range[0].date()} .. {date_range[1].date()}")

    # Build universe-null sample
    print("\nSampling universe-null bars (tradable filter)")
    example_tickers = {r["ticker"] for r in resolved}
    null_pairs = sample_universe_nulls(daily_cache, example_tickers,
                                       date_range, rng)
    print(f"  null bars collected: {len(null_pairs)} "
          f"across {len({t for t, _ in null_pairs})} tickers")

    # Build MOC feature matrix for examples + nulls
    print("\nBuilding MOC features for examples")
    ex_ticker_to_bars = defaultdict(list)
    for r in resolved:
        ex_ticker_to_bars[r["ticker"]].append(r["signal_bar"])
    ex_features = build_features_for_bars(daily_cache, ex_ticker_to_bars)

    print("Building MOC features for null bars")
    null_ticker_to_bars = defaultdict(list)
    for t, b in null_pairs:
        null_ticker_to_bars[t].append(b)
    null_features = build_features_for_bars(daily_cache, null_ticker_to_bars)

    # Stack into arrays
    outcome_names = list(FORWARD_WINDOWS) + ["mfe_10bar", "mae_10bar", "cap_eff_10bar"]

    for setup in SETUP_CLASSES + ["breakout_pooled"]:
        if setup == "breakout_pooled":
            cls_examples = [r for r in resolved if r["setup"] in SETUP_CLASSES]
        else:
            cls_examples = [r for r in resolved if r["setup"] == setup]
        if not cls_examples:
            print(f"\n{setup}: no examples, skipping")
            continue

        X_ex = []
        Y_out = {k: [] for k in outcome_names}
        for r in cls_examples:
            key = (r["ticker"], r["signal_bar"])
            vec = ex_features.get(key)
            if vec is None:
                continue
            X_ex.append(vec)
            for k in outcome_names:
                Y_out[k].append(r["outcomes"][k])
        if not X_ex:
            print(f"\n{setup}: no feature vectors, skipping")
            continue
        X_ex = np.array(X_ex)
        for k in outcome_names:
            Y_out[k] = np.array(Y_out[k], dtype=float)

        X_null = np.array([null_features[k] for k in null_features])
        n_ex = X_ex.shape[0]
        n_null = X_null.shape[0]

        print(f"\n{setup}: n_examples={n_ex}, n_null={n_null}")

        # KS per expression
        ks_stats = np.full(42, np.nan)
        for j in range(42):
            ex_vals = X_ex[:, j]
            null_vals = X_null[:, j]
            ex_vals = ex_vals[np.isfinite(ex_vals)]
            null_vals = null_vals[np.isfinite(null_vals)]
            if len(ex_vals) >= 10 and len(null_vals) >= 50:
                ks_stats[j] = ks_2samp(ex_vals, null_vals).statistic

        # Spearman per (expression, outcome)
        spearman_matrix = np.full((42, len(outcome_names)), np.nan)
        for j in range(42):
            ex_vals = X_ex[:, j]
            for k_idx, k in enumerate(outcome_names):
                y = Y_out[k]
                m = np.isfinite(ex_vals) & np.isfinite(y)
                if m.sum() < 10:
                    continue
                rho, _ = spearmanr(ex_vals[m], y[m])
                if np.isfinite(rho):
                    spearman_matrix[j, k_idx] = rho

        # Best signal separators
        top_ks = np.argsort(-np.nan_to_num(ks_stats, nan=-1))[:5]
        print("  top 5 signal separators (KS):")
        for idx in top_ks:
            print(f"    {EXPR_NAMES[idx]:<48s}  KS={ks_stats[idx]:.3f}")
        # Best outcome predictors
        abs_rho = np.abs(spearman_matrix)
        if np.isfinite(abs_rho).any():
            flat_idx = np.unravel_index(
                np.argsort(-np.nan_to_num(abs_rho, nan=-1), axis=None)[:5],
                abs_rho.shape,
            )
            print("  top 5 forward-outcome predictors (|Spearman rho|):")
            for i, j in zip(*flat_idx):
                print(f"    {EXPR_NAMES[i]:<48s}  "
                      f"vs {outcome_names[j]:<15s}  rho={spearman_matrix[i, j]:+.3f}")

        out_path = os.path.join(HERE, f"moc_usefulness_{setup}.png")
        render_summary(setup, ks_stats, spearman_matrix, outcome_names,
                       n_ex, n_null, out_path)
        print(f"  saved: {out_path}")


if __name__ == "__main__":
    main()
