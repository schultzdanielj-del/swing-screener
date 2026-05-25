"""Render a multi-panel chart for theme-eyeball: candidate ticker overlaid on each candidate theme's synthetic cohort chart.

Usage:
    python scripts/render_theme_eyeball.py TICKER theme_id1 theme_id2 ...

Output: PNG saved to local_runner/cache/theme_eyeball/<TICKER>.png
Each panel: candidate (red line) overlaid on cohort median (black line) + P25-P75 band (gray fill).
If cohort n<5: shows individual member lines (thin gray) instead of band.
"""
from __future__ import annotations
import sys
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from local_runner.theme_map import THEMES  # noqa: E402

OHLCV_PATH = REPO_ROOT / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT_DIR = REPO_ROOT / "local_runner" / "cache" / "theme_eyeball"
WINDOW_DAYS = 250  # ~1 year of trading days
SMALL_COHORT_THRESHOLD = 5


def load_ohlcv() -> dict[str, pd.DataFrame]:
    with open(OHLCV_PATH, "rb") as f:
        return pickle.load(f)


def rebased_close(df: pd.DataFrame, window: int) -> pd.Series | None:
    """Return last `window` days of close, rebased so first value = 100. Index is date."""
    if df is None or len(df) == 0:
        return None
    tail = df.tail(window).copy()
    if len(tail) < 30:
        return None
    s = pd.Series(tail["close"].values, index=pd.to_datetime(tail["date"]))
    base = s.iloc[0]
    if base <= 0 or not np.isfinite(base):
        return None
    return (s / base) * 100.0


def render_panel(ax, theme: str, members: list[str], candidate: str, ohlcv: dict, window: int):
    cand_df = ohlcv.get(candidate)
    cand_series = rebased_close(cand_df, window) if cand_df is not None else None

    member_series = {}
    for m in members:
        df = ohlcv.get(m)
        if df is None:
            continue
        s = rebased_close(df, window)
        if s is None:
            continue
        member_series[m] = s

    if not member_series:
        ax.text(0.5, 0.5, f"{theme}\n(no member data)", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(theme, fontsize=10)
        return

    # Align all member series on a common date index
    member_df = pd.DataFrame(member_series)
    # Drop rows where < half the members have data (handles new tickers)
    min_members = max(1, len(member_series) // 2)
    member_df = member_df.dropna(thresh=min_members)
    if member_df.empty:
        ax.text(0.5, 0.5, f"{theme}\n(alignment failed)", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(theme, fontsize=10)
        return

    n = len(member_series)
    if n >= SMALL_COHORT_THRESHOLD:
        median = member_df.median(axis=1)
        p25 = member_df.quantile(0.25, axis=1)
        p75 = member_df.quantile(0.75, axis=1)
        ax.fill_between(median.index, p25, p75, alpha=0.25, color="steelblue", label="P25-P75")
        ax.plot(median.index, median, color="navy", lw=2.0, label=f"cohort median (n={n})")
    else:
        # Small cohort — show individual member lines
        for m, s in member_series.items():
            ax.plot(s.index, s, color="gray", lw=0.7, alpha=0.7)
        median = member_df.median(axis=1)
        ax.plot(median.index, median, color="navy", lw=1.8, label=f"cohort median (n={n})")

    # Overlay candidate
    if cand_series is not None:
        ax.plot(cand_series.index, cand_series, color="crimson", lw=2.2, label=candidate)
    else:
        ax.text(0.5, 0.5, "no candidate data", ha="center", va="center", transform=ax.transAxes, color="red")

    ax.set_title(theme, fontsize=10)
    ax.axhline(100, color="black", lw=0.5, alpha=0.3)
    ax.grid(alpha=0.2)
    ax.legend(fontsize=7, loc="best")
    # Rotate date labels
    for label in ax.get_xticklabels():
        label.set_rotation(30)
        label.set_ha("right")
        label.set_fontsize(7)


def render(candidate: str, candidate_themes: list[str], window: int = WINDOW_DAYS):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ohlcv = load_ohlcv()
    n_themes = len(candidate_themes)

    fig_width = max(5, 4 * n_themes)
    fig, axes = plt.subplots(1, n_themes, figsize=(fig_width, 4.5), squeeze=False)
    axes = axes[0]

    for ax, theme in zip(axes, candidate_themes):
        if theme not in THEMES:
            ax.text(0.5, 0.5, f"{theme}\n(unknown theme)", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(theme, fontsize=10)
            continue
        members = [m for m in THEMES[theme] if m != candidate]  # exclude candidate from its own cohort
        render_panel(ax, theme, members, candidate, ohlcv, window)

    fig.suptitle(f"{candidate} vs candidate theme cohorts (last ~{window} trading days, rebased to 100)", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out_path = OUT_DIR / f"{candidate}.png"
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main():
    if len(sys.argv) < 3:
        print("Usage: render_theme_eyeball.py TICKER theme_id1 [theme_id2 ...]")
        sys.exit(1)
    candidate = sys.argv[1].upper()
    themes = sys.argv[2:]
    out = render(candidate, themes)
    print(out)


if __name__ == "__main__":
    main()
