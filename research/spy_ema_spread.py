"""SPY EMA8 - EMA21 spread: build dataset and test reversal correlation.

Reads the main repo daily OHLCV pickle, computes the spread on SPY close,
and runs a reversal-correlation analysis:

  1. Dataset: per-bar SPY close + ema8 + ema21 + spread.
  2. Distribution: percentiles of the spread (full history & last 5y).
  3. Local extrema: bars where close is the max (or min) within +/- N bars
     on each side. We then ask: what does the spread look like at those bars
     versus the unconditional distribution?
  4. Threshold scan: at spread >= X (or <= -X), what fraction of those bars
     are local peaks (or troughs)? Compare against the base rate.
  5. Forward-return panel: bin the spread into deciles and report mean
     forward 5/10/20-day return per bin.

No writes outside the project. Output goes to research/out/.
"""
from __future__ import annotations

import os
import pickle
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
PKL = os.path.join(MAIN_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

TICKER = "SPY"
EMA_FAST = 8
EMA_SLOW = 21
# Local extremum half-window (in trading days).
WIN = 10
# Forward-return horizons (trading days).
FWD_HORIZONS = (5, 10, 20)


def load_spy() -> pd.DataFrame:
    print(f"Loading {PKL} ...")
    with open(PKL, "rb") as f:
        cache = pickle.load(f)
    if TICKER not in cache:
        raise SystemExit(f"{TICKER} not in cache")
    df = cache[TICKER].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  {TICKER}: {len(df)} bars  {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")
    return df[["date", "open", "high", "low", "close", "volume"]]


def build_dataset(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema8"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema21"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    out["spread"] = out["ema8"] - out["ema21"]
    out["spread_pct"] = out["spread"] / out["close"] * 100.0
    return out


def mark_extrema(close: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    """Return boolean arrays (is_peak, is_trough), True when close[i] is
    the strict max / min over indices [i-win, i+win] inclusive (centered).
    Edge bars where the full window is unavailable are False."""
    n = len(close)
    is_peak = np.zeros(n, dtype=bool)
    is_trough = np.zeros(n, dtype=bool)
    for i in range(win, n - win):
        window = close[i - win:i + win + 1]
        c = close[i]
        if c == window.max() and (window > c).sum() == 0:
            # strict-ish max: c is no less than any neighbor
            if (window < c).sum() >= win:  # at least win bars strictly below it
                is_peak[i] = True
        if c == window.min() and (window < c).sum() == 0:
            if (window > c).sum() >= win:
                is_trough[i] = True
    return is_peak, is_trough


def percentiles(x: np.ndarray, ps=(1, 5, 10, 25, 50, 75, 90, 95, 99)) -> dict:
    return {p: float(np.percentile(x, p)) for p in ps}


def fmt_pct_row(label: str, d: dict) -> str:
    return label + "  " + "  ".join(f"p{p}={v:+.3f}" for p, v in d.items())


def analyze(df: pd.DataFrame, tag: str) -> None:
    print()
    print("=" * 72)
    print(f"  {tag}: n={len(df)}  {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")
    print("=" * 72)

    spread = df["spread"].to_numpy()
    close = df["close"].to_numpy()

    # --- Distribution -----------------------------------------------------
    print("\n[1] Spread distribution (absolute $, EMA8 - EMA21 on close)")
    print(fmt_pct_row("  all bars", percentiles(spread)))
    print(f"  mean = {spread.mean():+.3f}    std = {spread.std():.3f}")

    # --- Local extrema ---------------------------------------------------
    is_peak, is_trough = mark_extrema(close, WIN)
    n_peaks = int(is_peak.sum())
    n_troughs = int(is_trough.sum())
    n_valid = len(close) - 2 * WIN
    base_peak = n_peaks / n_valid
    base_trough = n_troughs / n_valid
    print(f"\n[2] Local extrema (window +/-{WIN} bars):  peaks={n_peaks}  troughs={n_troughs}")
    print(f"     base rate per bar:  peak={base_peak*100:.2f}%   trough={base_trough*100:.2f}%")

    if n_peaks >= 5:
        print("\n  Spread $ at PEAK bars:")
        print(fmt_pct_row("    ", percentiles(spread[is_peak])))
        print(f"    mean = {spread[is_peak].mean():+.3f}   median = {np.median(spread[is_peak]):+.3f}")
    if n_troughs >= 5:
        print("\n  Spread $ at TROUGH bars:")
        print(fmt_pct_row("    ", percentiles(spread[is_trough])))
        print(f"    mean = {spread[is_trough].mean():+.3f}   median = {np.median(spread[is_trough]):+.3f}")

    # --- Threshold scan (positive side -> peaks) -------------------------
    # Pick $ thresholds anchored at the data's own percentiles so the scan
    # is comparable across slices of different SPY price ranges.
    pos_thresholds = sorted(set(round(float(np.percentile(spread, p)), 2)
                                for p in (50, 70, 80, 85, 90, 93, 95, 97, 99)))
    neg_thresholds = sorted(set(round(float(np.percentile(spread, p)), 2)
                                for p in (50, 30, 20, 15, 10, 7, 5, 3, 1)), reverse=True)

    print("\n[3] Threshold scan -- positive spread $ vs peak hit rate")
    print(f"    'hit' = bar is a local peak within +/-{WIN} bars")
    print(f"    base rate (all bars): {base_peak*100:.2f}%")
    print(f"    {'thresh$':>9}  {'n_bars':>7}  {'n_peaks':>8}  {'peak_rate':>10}  {'lift_x':>7}")
    for thresh in pos_thresholds:
        mask = spread >= thresh
        valid_mask = np.zeros_like(mask)
        valid_mask[WIN:-WIN] = True
        mask = mask & valid_mask
        n = int(mask.sum())
        if n == 0:
            continue
        peaks_in = int(is_peak[mask].sum())
        rate = peaks_in / n
        lift = rate / base_peak if base_peak > 0 else float("nan")
        print(f"    {thresh:>+8.2f}   {n:>7d}  {peaks_in:>8d}  {rate*100:>9.2f}%  {lift:>6.2f}x")

    print("\n[3b] Threshold scan -- negative spread $ vs trough hit rate")
    print(f"    base rate (all bars): {base_trough*100:.2f}%")
    print(f"    {'thresh$':>9}  {'n_bars':>7}  {'n_trgh':>8}  {'trgh_rate':>10}  {'lift_x':>7}")
    for thresh in neg_thresholds:
        mask = spread <= thresh
        valid_mask = np.zeros_like(mask)
        valid_mask[WIN:-WIN] = True
        mask = mask & valid_mask
        n = int(mask.sum())
        if n == 0:
            continue
        trgh_in = int(is_trough[mask].sum())
        rate = trgh_in / n
        lift = rate / base_trough if base_trough > 0 else float("nan")
        print(f"    {thresh:>+8.2f}   {n:>7d}  {trgh_in:>8d}  {rate*100:>9.2f}%  {lift:>6.2f}x")

    # --- Forward-return panel by spread $ decile -------------------------
    print("\n[4] Mean forward return by spread-$ decile")
    bins = pd.qcut(spread, q=10, labels=False, duplicates="drop")
    panel = pd.DataFrame({"bin": bins, "spread_$": spread, "close": close})
    for h in FWD_HORIZONS:
        fwd_ret = np.full(len(close), np.nan, dtype=float)
        fwd_ret[:-h] = close[h:] / close[:-h] - 1.0
        panel[f"fwd{h}"] = fwd_ret

    rows = []
    for b in sorted(panel["bin"].dropna().unique()):
        sub = panel[panel["bin"] == b]
        row = {
            "decile": int(b) + 1,
            "n": len(sub),
            "spread$_lo": sub["spread_$"].min(),
            "spread$_hi": sub["spread_$"].max(),
        }
        for h in FWD_HORIZONS:
            row[f"fwd{h}_mean%"] = sub[f"fwd{h}"].mean() * 100
            row[f"fwd{h}_pos%"] = (sub[f"fwd{h}"] > 0).mean() * 100
        rows.append(row)
    decile_df = pd.DataFrame(rows)
    pd.set_option("display.float_format", lambda v: f"{v:+.3f}")
    print(decile_df.to_string(index=False))


def main():
    raw = load_spy()
    ds = build_dataset(raw)

    # Save full dataset.
    csv_path = os.path.join(OUT_DIR, "spy_ema_spread.csv")
    ds.to_csv(csv_path, index=False, float_format="%.6f")
    print(f"\nWrote dataset: {csv_path}  ({len(ds)} rows)")

    # Two slices: full history & most recent ~5 years.
    full = ds.iloc[EMA_SLOW * 5:].reset_index(drop=True)  # drop EMA warmup
    cutoff = ds["date"].max() - pd.Timedelta(days=365 * 5)
    last5 = ds[ds["date"] >= cutoff].reset_index(drop=True)

    analyze(full, f"Full history (post-warmup)")
    analyze(last5, "Last 5 years")


if __name__ == "__main__":
    main()
