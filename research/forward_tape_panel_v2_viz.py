"""Single-image grid: pyramid ∩ presignal signal persistence.
Two modes:
  MODE = "examples"  → 3×3 grid, 3 setups × {short, median, long} examples.
  MODE = "wild"      → 3×3 grid, 3 random wild (is_example=0) clusters per setup.
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import random
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sqlite3
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle, Patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MAIN = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
CACHE = os.path.join(MAIN, "local_runner", "cache")
DB = os.path.join(MAIN, "data", "scanperfect.db")
POOL_DIR = os.path.join(WORKTREE, "research", "classifier_pool")
PANEL_DIR = os.path.join(WORKTREE, "research", "forward_tape_panel_v2")
PRESIG_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
N_DERIV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
OUT_DIR = os.path.join(PANEL_DIR, "viz")

MODE = "mixed"   # "examples" | "wild" | "mixed"
WILD_SEED = 7
WILD_PER_SETUP = 6
# Reproduce + exclude any wild picks already shown in earlier grids.
EXCLUDE_PRIOR_SEEDS = [(42, 3), (123, 6), (7, 6)]

MIXED_SEED = 11
MIXED_PER_SETUP = 3   # 3 ex + 3 wild per setup = 18 total
# Prior example picks to avoid repeating in mixed mode.
EXCLUDE_PRIOR_EXAMPLES = {
    ("htf", "ASTS", "2024-05-24"), ("htf", "AFG", "2021-02-03"), ("htf", "FORM", "2026-01-07"),
    ("bf", "IREN", "2025-06-20"),  ("bf", "ANF", "2023-11-01"),  ("bf", "JD", "2020-05-28"),
    ("base", "AAPU", "2024-11-22"),("base", "APP", "2024-09-10"),("base", "BAP", "2025-12-01"),
}

# Examples mode: 3 setups × (short, median, long). All is_example=1.
EXAMPLE_PICKS = [
    [("htf",  "ASTS", "2024-05-24"),  ("htf",  "AFG",  "2021-02-03"), ("htf",  "FORM", "2026-01-07")],
    [("bf",   "IREN", "2025-06-20"),  ("bf",   "ANF",  "2023-11-01"), ("bf",   "JD",   "2020-05-28")],
    [("base", "AAPU", "2024-11-22"),  ("base", "APP",  "2024-09-10"), ("base", "BAP",  "2025-12-01")],
]
EXAMPLE_COL_LABELS = ["short (outlier)", "median", "long (outlier)"]


def load_universe():
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def load_n_bars(setup):
    with open(os.path.join(N_DERIV_DIR, f"{setup}_summary.json")) as f:
        return int(json.load(f)["N_bars"])


AVWAP_MIN_BARS_BACK = 2   # anchor must be at least this many bars before sig_idx


def pick_best_avwap_anchor(df, sig_idx, N_bars):
    """Find anchor A in [sig_idx - N_bars + 1, sig_idx - AVWAP_MIN_BARS_BACK] that
    maximizes AVWAP(A..sig_idx) evaluated AT sig_idx. Returns (anchor_idx, avwap_value)."""
    lo = max(0, sig_idx - N_bars + 1)
    hi_A = sig_idx - AVWAP_MIN_BARS_BACK
    if hi_A < lo:
        return None, None
    h = df["high"].values[:sig_idx + 1].astype(float)
    l = df["low"].values[:sig_idx + 1].astype(float)
    c = df["close"].values[:sig_idx + 1].astype(float)
    v = df["volume"].values[:sig_idx + 1].astype(float)
    tp = (h + l + c) / 3.0
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    best_A = lo; best_val = -np.inf
    for A in range(lo, hi_A + 1):
        total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A]
        total_v = cum_v[sig_idx + 1] - cum_v[A]
        if total_v <= 0:
            continue
        val = total_tpv / total_v
        if val > best_val:
            best_val = val; best_A = A
    return best_A, best_val


def compute_avwap_series(df, anchor, end_idx):
    """AVWAP anchored at `anchor`, evaluated at each bar from anchor through end_idx.
    Returns np.array of length end_idx - anchor + 1."""
    h = df["high"].values[anchor:end_idx + 1].astype(float)
    l = df["low"].values[anchor:end_idx + 1].astype(float)
    c = df["close"].values[anchor:end_idx + 1].astype(float)
    v = df["volume"].values[anchor:end_idx + 1].astype(float)
    tp = (h + l + c) / 3.0
    tpv_cum = np.cumsum(tp * v)
    v_cum = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(v_cum > 0, tpv_cum / v_cum, np.nan)


def load_earnings_for(ticker):
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT earnings_date FROM earnings_dates WHERE ticker=? ORDER BY earnings_date",
            (ticker,),
        ).fetchall()
    return np.array([str(r[0])[:10] for r in rows], dtype="<U10")


def latest_pyramid_file(setup):
    pat = os.path.join(CACHE, f"pyramid_{setup}_mp_sig*_pk*_*.json")
    files = [f for f in glob.glob(pat) if "sig0_pk0" not in os.path.basename(f)]
    def ts(p):
        m = re.search(r"_(\d{8})_(\d{6})\.json$", p)
        return (m.group(1), m.group(2)) if m else ("", "")
    return max(files, key=ts) if files else None


def load_pyramid_dates_for_ticker(setup, ticker):
    p = latest_pyramid_file(setup)
    if p is None:
        return set()
    with open(p) as f:
        d = json.load(f)
    tr = d.get("tier_results", {})
    target = d.get("summary", {}).get("final_total")
    best = None
    for k, v in tr.items():
        if isinstance(v, dict) and "final_signals" in v:
            fs = v["final_signals"]
            if isinstance(fs, list) and (target is None or len(fs) == target):
                best = fs
                if target is not None and len(fs) == target:
                    break
    if best is None:
        return set()
    return {str(s["date"])[:10] for s in best if s.get("ticker") == ticker}


def load_presignal_bars_for_ticker(setup, ticker):
    with open(os.path.join(PRESIG_DIR, f"{setup}_passes.pkl"), "rb") as f:
        pre = pickle.load(f)["pre_dedup_by_ticker"]
    return set(pre.get(ticker, []))


def dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def draw_candle(ax, x, o, h, l, c, width=0.7):
    color = "#2ca02c" if c >= o else "#d62728"
    ax.plot([x, x], [l, h], color="black", linewidth=0.6, zorder=2)
    body_bot = min(o, c); body_top = max(o, c)
    rect = Rectangle((x - width / 2, body_bot), width, max(body_top - body_bot, 1e-6),
                     facecolor=color, edgecolor="black", linewidth=0.4, zorder=3)
    ax.add_patch(rect)


def render_into(ax, setup, ticker, signal_date, universe):
    pool = json.load(open(os.path.join(POOL_DIR, f"{setup}_pool.json")))
    cluster = next((c for c in pool["clusters"]
                    if c["ticker"] == ticker and c["rightmost"]["date"] == signal_date), None)
    if cluster is None:
        ax.text(0.5, 0.5, f"not in pool:\n{setup} {ticker} {signal_date}",
                ha="center", va="center", transform=ax.transAxes)
        return

    cid = cluster["cluster_id"]
    sig_idx = cluster["rightmost"]["bar_idx"]
    is_example = cluster["is_example"]

    s_df = pd.read_pickle(os.path.join(PANEL_DIR, f"{setup}_scalars.pkl"))
    e_df = pd.read_pickle(os.path.join(PANEL_DIR, f"{setup}_events.pkl"))
    scal = s_df[s_df.cluster_id == cid].iloc[0].to_dict()
    evs = e_df[e_df.cluster_id == cid].sort_values("offset").reset_index(drop=True)

    exit_fire = evs.reanchor_upper_exit | evs.reanchor_lower_exit
    first_exit_off = int(evs[exit_fire].offset.min()) if exit_fire.any() else None
    alive_last = (first_exit_off - 1) if first_exit_off else int(scal["earnings_cap_offset"])

    # Backward cluster run
    with open(os.path.join(PRESIG_DIR, f"{setup}_passes.pkl"), "rb") as f:
        pre = pickle.load(f)["pre_dedup_by_ticker"]
    pre_set = set(pre.get(ticker, []))
    back_run = [sig_idx]
    k = sig_idx - 1
    while k in pre_set:
        back_run.append(k); k -= 1
    back_run = sorted(back_run)

    df = universe[ticker]
    # Window must include anchor candidates from up to N_bars behind signal.
    try:
        _N = load_n_bars(setup)
    except Exception:
        _N = 40
    lo = max(0, sig_idx - max(_N + 5, 35))
    hi = min(len(df) - 1, sig_idx + min(35, int(scal["earnings_cap_offset"]) + 5))
    d_open = df["open"].values[lo:hi + 1]
    d_high = df["high"].values[lo:hi + 1]
    d_low = df["low"].values[lo:hi + 1]
    d_close = df["close"].values[lo:hi + 1]
    d_dates = dates_str(df)[lo:hi + 1]

    ern = load_earnings_for(ticker)
    ern_in_view = [e for e in ern if d_dates[0] <= e <= d_dates[-1]]

    pyr_dates = load_pyramid_dates_for_ticker(setup, ticker)
    presig_bars = load_presignal_bars_for_ticker(setup, ticker)

    xs = np.arange(len(d_open))
    for i in range(len(d_open)):
        draw_candle(ax, xs[i], d_open[i], d_high[i], d_low[i], d_close[i])

    price_range = d_high.max() - d_low.min()
    dot_off = price_range * 0.03

    # Only mark intersection bars — bars where pyramid fires AND presignal passes
    # (i.e. classifier pool signal bars for this ticker within the chart window).
    intersect_xs, intersect_ys = [], []
    for i, bar_idx in enumerate(range(lo, hi + 1)):
        date = d_dates[i]
        if date in pyr_dates and bar_idx in presig_bars:
            intersect_xs.append(i); intersect_ys.append(d_high[i] + dot_off)

    if intersect_xs:
        ax.scatter(intersect_xs, intersect_ys, marker="o", s=30, color="#9467bd",
                   edgecolor="black", linewidths=0.4, zorder=5)

    # Backward cluster run band
    if len(back_run) > 1:
        lo_run = back_run[0] - lo
        hi_run = back_run[-1] - lo
        ax.axvspan(lo_run - 0.5, hi_run + 0.5, facecolor="gold", alpha=0.25, zorder=1)

    # Signal bar
    sig_x = sig_idx - lo
    ax.axvspan(sig_x - 0.5, sig_x + 0.5, facecolor="#1f77b4", alpha=0.35, zorder=1)

    # Entry bar (examples)
    if is_example:
        e_x = sig_x + 1
        if 0 <= e_x < len(d_open):
            ax.axvspan(e_x - 0.5, e_x + 0.5, facecolor="#2ca02c", alpha=0.35, zorder=1)

    # Alive / dead stripes along bottom
    y_lo = d_low.min() - price_range * 0.03
    y_hi = d_low.min() - price_range * 0.015
    if alive_last >= 1:
        ax.fill_between([sig_x + 0.5, sig_x + alive_last + 0.5], [y_lo, y_lo], [y_hi, y_hi],
                        color="#2ca02c", alpha=0.7, zorder=1)
    if first_exit_off is not None:
        dead_from = sig_x + first_exit_off
        dead_to = sig_x + int(scal["earnings_cap_offset"])
        if dead_from <= dead_to and dead_to < len(d_open):
            ax.fill_between([dead_from - 0.5, dead_to + 0.5], [y_lo, y_lo], [y_hi, y_hi],
                            color="#d62728", alpha=0.7, zorder=1)

    # AVWAP — anchored on the N-window bar that produces the highest AVWAP at sig_idx.
    # Plot the line from anchor through the end of the chart window.
    try:
        N_bars = load_n_bars(setup)
    except Exception:
        N_bars = None
    if N_bars:
        a_idx, a_val = pick_best_avwap_anchor(df, sig_idx, N_bars)
        if a_idx is not None and a_idx <= hi:
            av_end = hi
            av_series = compute_avwap_series(df, a_idx, av_end)
            # Map anchor-space indices → chart-window x positions
            av_xs = np.arange(a_idx, av_end + 1) - lo
            mask = (av_xs >= 0) & (av_xs < len(d_open))
            ax.plot(av_xs[mask], av_series[mask], color="#8c2d8c", linewidth=1.4,
                    alpha=0.9, zorder=4)
            # Mark the anchor candle with a small triangle at the low
            if 0 <= a_idx - lo < len(d_open):
                ax.scatter([a_idx - lo], [d_low[a_idx - lo] - price_range * 0.015],
                           marker="^", s=30, color="#8c2d8c", edgecolor="black",
                           linewidths=0.4, zorder=5)

    # Earnings
    for e in ern_in_view:
        if e in d_dates:
            e_idx = int(np.where(d_dates == e)[0][0])
            ax.axvline(e_idx, color="red", linestyle="--", linewidth=0.8, alpha=0.6)

    # Tick labels — very sparse
    tick_every = max(1, len(d_open) // 5)
    tick_idx = list(range(0, len(d_open), tick_every))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([d_dates[i] for i in tick_idx], rotation=30, ha="right", fontsize=7)
    ax.tick_params(axis="y", labelsize=7)

    persist_str = f"persist={alive_last}" if first_exit_off else f"persist={alive_last} (cap)"
    ax.set_title(f"{ticker}  {signal_date}\n{persist_str}  back_run={len(back_run)}  cap={int(scal['earnings_cap_offset'])}",
                 fontsize=8)
    ax.set_xlim(-0.5, len(d_open) - 0.5)
    ax.set_ylim(y_lo - price_range * 0.01, d_high.max() + price_range * 0.10)
    ax.grid(True, alpha=0.25)


def prior_picks_set():
    """Reproduce every (setup, ticker, signal_date) pick from EXCLUDE_PRIOR_SEEDS
    so the new draw avoids anything the user has already seen."""
    out = set()
    for seed, per_setup in EXCLUDE_PRIOR_SEEDS:
        rng = random.Random(seed)
        for setup in ("htf", "bf", "base"):
            pool = json.load(open(os.path.join(POOL_DIR, f"{setup}_pool.json")))
            wild = [c for c in pool["clusters"] if c["is_example"] == 0]
            chosen = rng.sample(wild, min(per_setup, len(wild)))
            for c in chosen:
                out.add((setup, c["ticker"], c["rightmost"]["date"]))
    return out


def pick_wild(setup, n, rng, exclude=None):
    pool = json.load(open(os.path.join(POOL_DIR, f"{setup}_pool.json")))
    exclude = exclude or set()
    wild = [c for c in pool["clusters"]
            if c["is_example"] == 0
            and (setup, c["ticker"], c["rightmost"]["date"]) not in exclude]
    chosen = rng.sample(wild, min(n, len(wild)))
    return [(setup, c["ticker"], c["rightmost"]["date"]) for c in chosen]


def pick_example(setup, n, rng, exclude):
    pool = json.load(open(os.path.join(POOL_DIR, f"{setup}_pool.json")))
    ex = [c for c in pool["clusters"]
          if c["is_example"] == 1
          and (setup, c["ticker"], c["rightmost"]["date"]) not in exclude]
    chosen = rng.sample(ex, min(n, len(ex)))
    return [(setup, c["ticker"], c["rightmost"]["date"]) for c in chosen]


def build_picks():
    if MODE == "examples":
        return EXAMPLE_PICKS, EXAMPLE_COL_LABELS, "persistence_grid.png", (
            "Pyramid ∩ presignal — setup persistence across 113 breakout examples "
            "(3 setups × short/median/long outliers)"
        )
    if MODE == "wild":
        rng = random.Random(WILD_SEED)
        exclude = prior_picks_set()
        picks = []
        for setup in ("htf", "bf", "base"):
            flat = pick_wild(setup, WILD_PER_SETUP, rng, exclude=exclude)
            for r in range(0, WILD_PER_SETUP, 3):
                picks.append(flat[r:r + 3])
        print(f"  excluded {len(exclude)} prior picks; new 18 guaranteed disjoint")
        col_labels = [f"wild #{i+1}" for i in range(3)]
        total = WILD_PER_SETUP * 3
        return picks, col_labels, "wild_grid.png", (
            f"Pyramid ∩ presignal — {total} random wild (non-example) clusters, "
            f"{WILD_PER_SETUP} per setup (seed={WILD_SEED})"
        )
    # MODE == "mixed": 3 examples + 3 wild per setup, 18 total
    rng = random.Random(MIXED_SEED)
    wild_exclude = prior_picks_set()
    ex_exclude = set(EXCLUDE_PRIOR_EXAMPLES)
    picks = []
    for setup in ("htf", "bf", "base"):
        picks.append(pick_example(setup, MIXED_PER_SETUP, rng, ex_exclude))
        picks.append(pick_wild(setup, MIXED_PER_SETUP, rng, exclude=wild_exclude))
    col_labels = [f"pick #{i+1}" for i in range(3)]
    return picks, col_labels, "mixed_grid.png", (
        "Pyramid ∩ presignal + AVWAP anchored at highest-AVWAP-in-N-window bar "
        f"— 3 examples + 3 wild per setup (seed={MIXED_SEED})"
    )


def main():
    universe = load_universe()
    print(f"universe: {len(universe):,} tickers  mode={MODE}")

    picks, col_labels, out_name, suptitle = build_picks()
    n_rows = len(picks); n_cols = len(picks[0])
    # Height scales with row count; 3 in/row is comfortable for candles at this width.
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(18, max(11, n_rows * 3.5)))
    if n_rows == 1:
        axes = np.array([axes])

    for r, row_picks in enumerate(picks):
        for c, (setup, ticker, signal_date) in enumerate(row_picks):
            ax = axes[r, c]
            render_into(ax, setup, ticker, signal_date, universe)
            if r == 0:
                ax.text(0.5, 1.18, col_labels[c], transform=ax.transAxes,
                        ha="center", va="bottom", fontsize=10, fontweight="bold")
            if c == 0:
                ax.text(-0.15, 0.5, row_picks[0][0].upper(), transform=ax.transAxes,
                        ha="right", va="center", fontsize=12, fontweight="bold", rotation=90)

    legend_handles = [
        Patch(facecolor="#1f77b4", alpha=0.35, label="signal bar (S)"),
        Patch(facecolor="#2ca02c", alpha=0.35, label="entry bar (E = S+1) — examples only"),
        Patch(facecolor="gold", alpha=0.25, label="backward cluster run"),
        Patch(facecolor="#2ca02c", alpha=0.7, label="forward presignal-alive"),
        Patch(facecolor="#d62728", alpha=0.7, label="presignal dead"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#9467bd",
               markeredgecolor="black", markersize=8, label="pool signal (pyramid ∩ presignal)"),
        Line2D([0], [0], color="#8c2d8c", linewidth=1.6,
               label="AVWAP (anchored highest in N window)"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#8c2d8c",
               markeredgecolor="black", markersize=9, label="AVWAP anchor bar"),
        Line2D([0], [0], color="red", linestyle="--", label="earnings"),
    ]
    fig.legend(handles=legend_handles, loc="lower center", ncol=4, fontsize=9,
               bbox_to_anchor=(0.5, -0.015), frameon=True)

    fig.suptitle(suptitle, fontsize=12, y=0.995)
    plt.tight_layout(rect=[0.02, 0.04, 1, 0.97])

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, out_name)
    assert out.startswith(WORKTREE), f"refusing write outside worktree: {out}"
    plt.savefig(out, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
