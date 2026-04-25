"""Render Extension Chart Trendlines PNG truth set for Phase 4 validation gate.

Walks the production primitive at asof 2026-04-10 for AAPL/CAR/SPY/MSFT/TSLA
on freshly-recomputed ext50 (post 2026-04-23 OHLCV distribution-fix). Renders
PNGs to research/ext50_trendlines_truth_2026-04-24/.

After Dan eyeball-locks, these PNGs are the cache-build pixel-identity gate.
"""
import os
import sys
import io
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "local_runner"))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from local_runner.expr_cache_builder import (
    _load_daily_cache, _load_htf_cache, _load_expressions, _init_worker,
    _compute_ticker_full, _df_to_dict, _truncate_to_cache_window,
)
from scripts.reversal_profile import compute_all_reversal_profile_series
from scripts.ext50_trendlines import (
    cascade_at, line_projection, PROMINENCE, TOP_N,
)

ASOF_STR = "2026-04-10"
TICKERS = ("AAPL", "CAR", "SPY", "MSFT", "TSLA")
OUT_DIR = os.path.join(REPO, "research", "ext50_trendlines_truth_2026-04-24")


def _fresh_ext(ticker, exprs, daily, weekly, monthly):
    """Compute ext_avgc50_adr14 for one ticker via _compute_ticker_full."""
    df = _truncate_to_cache_window(daily[ticker])
    df_dict = {
        "date": df["date"].values,
        "open": df["open"].values, "high": df["high"].values,
        "low": df["low"].values, "close": df["close"].values,
        "volume": df["volume"].values,
    }
    w_dict = _df_to_dict(weekly.get(ticker)) if weekly else None
    m_dict = _df_to_dict(monthly.get(ticker)) if monthly else None
    out_t, dates_arr, data = _compute_ticker_full((ticker, df_dict, w_dict, m_dict))
    if data is None:
        raise RuntimeError(f"{ticker}: _compute_ticker_full returned None")
    ext_idx = next(j for j, e in enumerate(exprs) if e["name"] == "ext_avgc50_adr14")
    ext = data[:, ext_idx].astype(np.float64)
    dates = pd.to_datetime(dates_arr).values.astype("datetime64[D]")
    return dates, ext


def _draw(ticker, dates, ext, asof_bar, snap, levels):
    """Render the chart matching research/v7 visual format (line+fill+anchors)."""
    start_bar = max(0, asof_bar - 600)  # show ~2.5y of history; full primitive uses all
    vis_ext = ext[start_bar:asof_bar + 1]
    vis_dates = dates[start_bar:asof_bar + 1]
    mask_nn = ~np.isnan(vis_ext)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    ax.plot(vis_dates[mask_nn], vis_ext[mask_nn], color="#333", lw=1.1, zorder=2)
    ax.fill_between(vis_dates[mask_nn], vis_ext[mask_nn], 0,
                    where=vis_ext[mask_nn] >= 0, color="#4a9", alpha=0.15, zorder=1)
    ax.fill_between(vis_dates[mask_nn], vis_ext[mask_nn], 0,
                    where=vis_ext[mask_nn] < 0, color="#c64", alpha=0.15, zorder=1)
    ax.axhline(0, color="#333", lw=0.8)

    upside_1 = levels.get("upside_1", float("nan"))
    upside_2 = levels.get("upside_2", float("nan"))
    chop_upper = levels.get("chop_upper", float("nan"))
    downside_1 = levels.get("downside_1", float("nan"))
    downside_2 = levels.get("downside_2", float("nan"))
    for lvl_val, lvl_lbl, c in (
        (upside_1, "u1", "#9a3"), (upside_2, "u2", "#7a2"),
        (downside_1, "d1", "#c63"), (downside_2, "d2", "#a42"),
        (chop_upper, "chop_u", "#888"),
    ):
        if not np.isnan(lvl_val):
            ax.axhline(lvl_val, color=c, lw=0.6, ls=":", alpha=0.8)
            ax.text(vis_dates[-1] + np.timedelta64(2, "D"), lvl_val,
                    f" {lvl_lbl}={lvl_val:+.2f}", color=c, fontsize=7, va="center")

    def draw_line(L, color, lw, label):
        i0, i1 = L["i0"], L["i1"]
        v0, v1 = L["v0"], L["v1"]
        if i0 < start_bar or i1 < start_bar:
            # Anchor outside visible window — extend visible portion only
            t0 = max(i0, start_bar)
            v_at_t0 = line_projection(i0, v0, i1, v1, t0)
            ax.plot([dates[t0], dates[i1]], [v_at_t0, v1], color=color, lw=lw, zorder=4,
                    ls="-", alpha=0.9)
        else:
            ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, zorder=4,
                    label=f"{label} ({L['touches']}t)")
            ax.scatter([dates[i0], dates[i1]], [v0, v1], color=color, s=34,
                       zorder=6, edgecolor="#000", lw=0.5)
        if asof_bar > i1:
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            ax.plot([dates[i1], dates[asof_bar]], [v1, proj], color=color, lw=lw,
                    ls=":", alpha=0.7, zorder=3)

    upper_shades = ["#a00", "#d33", "#f66"]
    lower_shades = ["#00a", "#33d", "#66f"]
    for n, L in enumerate(snap["upper"]):
        draw_line(L, upper_shades[n % len(upper_shades)], 2.0 - 0.3 * n, f"U{n + 1}")
    for n, L in enumerate(snap["lower"]):
        draw_line(L, lower_shades[n % len(lower_shades)], 2.0 - 0.3 * n, f"L{n + 1}")

    ax.axvline(dates[asof_bar], color="#555", lw=0.9, ls="-.", alpha=0.6)
    ax.set_xlim(vis_dates[0], vis_dates[-1] + np.timedelta64(60, "D"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    agg = snap["aggregates"]
    ax.set_title(
        f"{ticker} 50-ext as of {ASOF_STR} — full-history primitive  "
        f"(total={agg['total_candidates']} desc={agg['count_descending']} "
        f"asc={agg['count_ascending']}, upper={len(snap['upper'])} lower={len(snap['lower'])})",
        fontsize=11)
    ax.set_ylabel("ext (ADR)")
    if snap["upper"] or snap["lower"]:
        ax.legend(loc="upper left", fontsize=7)
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_trendlines_{ticker}_{ASOF_STR}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Output dir: {OUT_DIR}")

    print("Loading expressions + initializing worker...")
    exprs = _load_expressions()
    _init_worker(exprs)
    daily = _load_daily_cache()
    weekly = _load_htf_cache("weekly") or {}
    monthly = _load_htf_cache("monthly") or {}

    asof = np.datetime64(ASOF_STR)
    for ticker in TICKERS:
        if ticker not in daily:
            print(f"  {ticker}: missing from cache, skipped")
            continue
        dates, ext = _fresh_ext(ticker, exprs, daily, weekly, monthly)
        asof_bar = int(np.searchsorted(dates, asof, side="right") - 1)
        # Compute Levels at this ticker's asof via reversal_profile primitive
        rp = compute_all_reversal_profile_series(ext)
        levels = {nm: float(rp[nm][asof_bar]) for nm in
                  ("upside_1", "upside_2", "downside_1", "downside_2", "chop_upper")}
        snap = cascade_at(ext, asof_bar, levels)
        out_path = _draw(ticker, dates, ext, asof_bar, snap, levels)
        agg = snap["aggregates"]
        print(f"  {ticker} asof_bar={asof_bar} ext={ext[asof_bar]:+.3f}  "
              f"upper={len(snap['upper'])} lower={len(snap['lower'])}  "
              f"total={agg['total_candidates']} desc={agg['count_descending']} asc={agg['count_ascending']}  "
              f"u1={levels['upside_1']:+.2f} u2={levels['upside_2']:+.2f} d2={levels['downside_2']:+.2f}")
        for n, L in enumerate(snap["upper"]):
            print(f"    U{n+1}: {L['v0']:+.2f}@bar{L['i0']} -> {L['v1']:+.2f}@bar{L['i1']}  "
                  f"slope={L['slope']:+.4f} proj={L['proj_asof']:+.2f} dist={L['signed_dist']:+.2f}  "
                  f"touches={L['touches']} cross={L['total_cross']} span={L['span']}")
        for n, L in enumerate(snap["lower"]):
            print(f"    L{n+1}: {L['v0']:+.2f}@bar{L['i0']} -> {L['v1']:+.2f}@bar{L['i1']}  "
                  f"slope={L['slope']:+.4f} proj={L['proj_asof']:+.2f} dist={L['signed_dist']:+.2f}  "
                  f"touches={L['touches']} cross={L['total_cross']} span={L['span']}")
        print(f"    -> {out_path}")


if __name__ == "__main__":
    main()
