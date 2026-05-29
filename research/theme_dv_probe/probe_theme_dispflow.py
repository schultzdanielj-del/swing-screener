"""Theme-level disproportionate buying flow vs broad market."""
import pickle
import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS

CACHE_PATH = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT_PATH = Path(__file__).parent / "theme_dispflow.png"

THEMES_TO_PROBE = [
    "quantum_computing",
    "uranium_pure_plays",
    "space_economy",
    "managed_care",   # quiet control
]

BARS = 250
BASELINE_LB = 100
BASELINE_GAP = 20
SMOOTH_WINDOWS = [20, 65]   # trimmed to make room for MACD panel
CLIP = 5.0
MACD_FAST = 6
MACD_SLOW = 20

print("Loading cache...")
with open(CACHE_PATH, "rb") as f:
    cache = pickle.load(f)
print(f"  {len(cache)} tickers")

print("Computing per-ticker normalized signed flow...")
t0 = time.time()
norm_series = {}
close_series = {}
min_bars = BASELINE_LB + BASELINE_GAP + 1
for ticker, df in cache.items():
    if len(df) < min_bars:
        continue
    open_ = df["open"].astype(float).values
    close = df["close"].astype(float).values
    vol = df["volume"].astype(float).values
    raw = (close - open_) * vol
    raw_abs = np.abs(raw)
    s_abs = pd.Series(raw_abs)
    baseline = s_abs.shift(BASELINE_GAP).rolling(BASELINE_LB).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        normalized = np.where(baseline > 0, raw / baseline, np.nan)
    normalized = np.clip(normalized, -CLIP, CLIP)
    dates = pd.to_datetime(df["date"])
    norm_series[ticker] = pd.Series(normalized, index=dates, name=ticker)
    close_series[ticker] = pd.Series(close, index=dates, name=ticker)
print(f"  {len(norm_series)} tickers usable, {time.time()-t0:.1f}s")

print("Universe mean per bar...")
t0 = time.time()
big = pd.concat(norm_series.values(), axis=1)
universe_mean = big.mean(axis=1, skipna=True)
print(f"  done {time.time()-t0:.1f}s")


def theme_composite_close(members):
    """Equal-weight composite, each member rebased to 100 at first valid bar (matches dashboard)."""
    series_list = []
    for m in members:
        if m not in close_series:
            continue
        s = close_series[m].dropna()
        if len(s) == 0:
            continue
        s_norm = s / s.iloc[0] * 100.0
        series_list.append(s_norm)
    if not series_list:
        return None
    wide = pd.concat(series_list, axis=1)
    # Require at least half the members present at a bar
    n_required = max(1, len(series_list) // 2)
    present = wide.notna().sum(axis=1)
    composite = wide.mean(axis=1, skipna=True)
    composite = composite.where(present >= n_required)
    return composite


def theme_normalized_flow(members):
    """Mean of per-ticker normalized signed flow across theme members per bar."""
    series_list = [norm_series[m] for m in members if m in norm_series]
    if not series_list:
        return None
    wide = pd.concat(series_list, axis=1)
    return wide.mean(axis=1, skipna=True)


# Plot
plt.rcParams.update({
    "figure.facecolor": "black",
    "axes.facecolor": "black",
    "axes.edgecolor": "#666",
    "axes.labelcolor": "#ddd",
    "xtick.color": "#bbb",
    "ytick.color": "#bbb",
    "text.color": "#ddd",
    "grid.color": "#1a1a1c",
    "font.size": 9,
})

n_cols = len(THEMES_TO_PROBE)
# Layout: price + MACD + each smoothing window of disp_flow
n_rows = 2 + len(SMOOTH_WINDOWS)
height_ratios = [2.2, 1.0] + [1.0] * len(SMOOTH_WINDOWS)
fig, axes = plt.subplots(n_rows, n_cols, figsize=(5 * n_cols, 11), sharex="col",
                         gridspec_kw={"height_ratios": height_ratios,
                                      "hspace": 0.10, "wspace": 0.22})


def find_pivots(series, k=3):
    """Return (high_idx, low_idx) of pivots: peaks/troughs with k bars lower/higher on each side."""
    arr = series.values
    n = len(arr)
    highs, lows = [], []
    for i in range(k, n - k):
        v = arr[i]
        if np.isnan(v):
            continue
        left = arr[i-k:i]
        right = arr[i+1:i+1+k]
        if np.all(v > left) and np.all(v > right):
            highs.append(i)
        elif np.all(v < left) and np.all(v < right):
            lows.append(i)
    return highs, lows


def detect_divergences(price, macd, k=3, lookback=240, max_offset=10):
    """Return lists of (idx1, idx2) bullish and bearish divergences."""
    p_highs, p_lows = find_pivots(price, k=k)
    m_highs, m_lows = find_pivots(macd, k=k)
    bull, bear = [], []
    # Bullish: price LL paired with MACD HL
    for i, idx in enumerate(p_lows):
        if i == 0:
            continue
        # search backwards for a prior price low with valid trendline
        for j in range(i-1, -1, -1):
            prev = p_lows[j]
            if idx - prev > lookback:
                break
            if idx - prev < 6:
                continue
            # find MACD low near each price low
            m_near_idx = min(m_lows, key=lambda m: abs(m - idx), default=None)
            m_near_prev = min(m_lows, key=lambda m: abs(m - prev), default=None)
            if m_near_idx is None or m_near_prev is None:
                continue
            if abs(m_near_idx - idx) > max_offset or abs(m_near_prev - prev) > max_offset:
                continue
            # price LL, MACD HL
            if price.iloc[idx] < price.iloc[prev] and macd.iloc[m_near_idx] > macd.iloc[m_near_prev]:
                bull.append((prev, idx))
                break
    # Bearish: price HH paired with MACD LH
    for i, idx in enumerate(p_highs):
        if i == 0:
            continue
        for j in range(i-1, -1, -1):
            prev = p_highs[j]
            if idx - prev > lookback:
                break
            if idx - prev < 6:
                continue
            m_near_idx = min(m_highs, key=lambda m: abs(m - idx), default=None)
            m_near_prev = min(m_highs, key=lambda m: abs(m - prev), default=None)
            if m_near_idx is None or m_near_prev is None:
                continue
            if abs(m_near_idx - idx) > max_offset or abs(m_near_prev - prev) > max_offset:
                continue
            if price.iloc[idx] > price.iloc[prev] and macd.iloc[m_near_idx] < macd.iloc[m_near_prev]:
                bear.append((prev, idx))
                break
    return bull, bear

for col, theme_key in enumerate(THEMES_TO_PROBE):
    members = THEMES[theme_key]
    members_present = [m for m in members if m in norm_series]
    label = THEME_LABELS.get(theme_key, theme_key)
    print(f"{theme_key}: {len(members)} listed, {len(members_present)} present in cache")

    composite = theme_composite_close(members)
    theme_flow = theme_normalized_flow(members)

    # Align indicator on theme_flow's index (which is subset of universe_mean's)
    aligned_uni = universe_mean.reindex(theme_flow.index)
    theme_disp = theme_flow - aligned_uni

    # Display window: last BARS bars of composite (the canonical theme x-axis)
    comp_tail = composite.dropna().iloc[-BARS:]
    tail_idx = comp_tail.index
    dates_plot = tail_idx.values

    ax_p = axes[0, col]
    ax_p.plot(dates_plot, comp_tail.values, color="white", lw=1.0)
    title_str = f"{label}\n({len(members_present)} members)"
    ax_p.set_title(title_str, color="white", fontsize=10, pad=4)
    ax_p.grid(True, alpha=0.25)
    ax_p.tick_params(labelbottom=False)
    if col == 0:
        ax_p.set_ylabel("Composite (norm)", color="#ddd")

    # MACD on composite close
    ema_fast = composite.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = composite.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_tail = macd_line.reindex(tail_idx)

    ax_m = axes[1, col]
    ax_m.plot(dates_plot, macd_tail.values, color="#ffcc00", lw=1.0)
    ax_m.axhline(0, color="#888", lw=0.7, alpha=0.7)
    ax_m.grid(True, alpha=0.25)
    ax_m.tick_params(labelbottom=False)
    if col == 0:
        ax_m.set_ylabel(f"MACD\n({MACD_FAST}/{MACD_SLOW})", color="#ddd")

    # Divergence detection on tail-window subset
    bull, bear = detect_divergences(comp_tail, macd_tail.dropna(),
                                    k=3, lookback=240, max_offset=10)

    print(f"\n=== {theme_key} ===")
    print(f"MACD bull divs ({len(bull)}):")
    for prev, idx in bull:
        print(f"  prior_low {pd.Timestamp(dates_plot[prev]).date()} -> "
              f"recent_low {pd.Timestamp(dates_plot[idx]).date()}")
    print(f"MACD bear divs ({len(bear)}):")
    for prev, idx in bear:
        print(f"  prior_high {pd.Timestamp(dates_plot[prev]).date()} -> "
              f"recent_high {pd.Timestamp(dates_plot[idx]).date()}")
    comp_arr = comp_tail.values
    macd_arr = macd_tail.values
    for prev, idx in bull:
        ax_p.plot([dates_plot[prev], dates_plot[idx]],
                  [comp_arr[prev], comp_arr[idx]],
                  color="#1eff1e", lw=1.0, linestyle="--", alpha=0.85)
        ax_m.plot([dates_plot[prev], dates_plot[idx]],
                  [macd_arr[prev], macd_arr[idx]],
                  color="#1eff1e", lw=1.0, linestyle="--", alpha=0.85)
    for prev, idx in bear:
        ax_p.plot([dates_plot[prev], dates_plot[idx]],
                  [comp_arr[prev], comp_arr[idx]],
                  color="#ff3030", lw=1.0, linestyle="--", alpha=0.85)
        ax_m.plot([dates_plot[prev], dates_plot[idx]],
                  [macd_arr[prev], macd_arr[idx]],
                  color="#ff3030", lw=1.0, linestyle="--", alpha=0.85)

    for row, w in enumerate(SMOOTH_WINDOWS, start=2):
        disp_smooth = theme_disp.rolling(w).sum()
        disp_vals = disp_smooth.reindex(tail_idx).values

        ax_d = axes[row, col]
        ax_d.plot(dates_plot, disp_vals, color="#5fc8ff", lw=1.0)
        ax_d.axhline(0, color="#888", lw=0.7, alpha=0.7)
        valid = ~np.isnan(disp_vals)
        pos_mask = valid & (disp_vals > 0)
        neg_mask = valid & (disp_vals < 0)
        ax_d.fill_between(dates_plot, 0, disp_vals, where=pos_mask,
                          color="#1eff1e", alpha=0.30, interpolate=True)
        ax_d.fill_between(dates_plot, 0, disp_vals, where=neg_mask,
                          color="#ff3030", alpha=0.30, interpolate=True)
        ax_d.grid(True, alpha=0.25)
        if row < n_rows - 1:
            ax_d.tick_params(labelbottom=False)
        else:
            ax_d.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
            ax_d.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
            plt.setp(ax_d.get_xticklabels(), rotation=0, ha="center")
        if col == 0:
            ax_d.set_ylabel(f"{w}d sum", color="#ddd")

        # Detect divergences on this disp_flow window vs composite price
        disp_series = pd.Series(disp_vals, index=tail_idx)
        df_bull, df_bear = detect_divergences(comp_tail, disp_series,
                                              k=3, lookback=240, max_offset=10)
        print(f"Disp_flow {w}d bull divs ({len(df_bull)}):")
        for prev, idx in df_bull:
            print(f"  prior_low {pd.Timestamp(dates_plot[prev]).date()} -> "
                  f"recent_low {pd.Timestamp(dates_plot[idx]).date()}")
        print(f"Disp_flow {w}d bear divs ({len(df_bear)}):")
        for prev, idx in df_bear:
            print(f"  prior_high {pd.Timestamp(dates_plot[prev]).date()} -> "
                  f"recent_high {pd.Timestamp(dates_plot[idx]).date()}")
        for prev, idx in df_bull:
            ax_p.plot([dates_plot[prev], dates_plot[idx]],
                      [comp_arr[prev], comp_arr[idx]],
                      color="#00ffd5", lw=1.0, linestyle=":", alpha=0.95)
            ax_d.plot([dates_plot[prev], dates_plot[idx]],
                      [disp_vals[prev], disp_vals[idx]],
                      color="#00ffd5", lw=1.0, linestyle=":", alpha=0.95)
        for prev, idx in df_bear:
            ax_p.plot([dates_plot[prev], dates_plot[idx]],
                      [comp_arr[prev], comp_arr[idx]],
                      color="#ff5fff", lw=1.0, linestyle=":", alpha=0.95)
            ax_d.plot([dates_plot[prev], dates_plot[idx]],
                      [disp_vals[prev], disp_vals[idx]],
                      color="#ff5fff", lw=1.0, linestyle=":", alpha=0.95)

fig.suptitle(f"Theme-level disproportionate buying vs broad market "
             f"(baseline {BASELINE_LB}-bar trailing, gap {BASELINE_GAP}; "
             f"windows {SMOOTH_WINDOWS}; last {BARS} bars)",
             color="#ddd", fontsize=10, y=0.99)
plt.savefig(OUT_PATH, facecolor="black", dpi=130, bbox_inches="tight")
plt.close()
print(f"Saved: {OUT_PATH}")
