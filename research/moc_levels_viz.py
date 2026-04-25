"""Visualize MOC levels on the D1 chart.

Walks the D1 bars chronologically, building the MOC level list per the
EXPRESSION_ENGINE_V2.md §5 spec:
  - RVOL = volume / SMA(volume, 50)
  - Tolerance = ATR14 * sqrt(1/78) ~= ATR14 * 0.113
  - Level birth: D1 H or L with RVOL > 1 that is NOT within tolerance of an
    existing active level
  - Stacking: an H or L within tolerance of an existing level adds
    max(0, RVOL - 1) to that level's stack_weight, increments stack_count
  - Cross event: D1 close crosses from one side of the level to the other
    beyond tolerance -> cross_count increments (exposed as raw feature)

One PNG per ticker: full D1 history with each level drawn as a horizontal
line. Line alpha + line thickness encode stack_weight so the eye picks out
the stronger levels first; weaker levels fade into the background.
"""
import os
import pickle
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_OHLCV = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl"
TICKERS = ["F", "SPY", "TSLA"]

RVOL_WINDOW = 50
ATR_WINDOW = 14
TOLERANCE_FRAC = np.sqrt(1.0 / 78.0)
RVOL_BIRTH_MIN = 1.0


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


def build_moc_levels(high, low, close, volume):
    """Return list of level dicts populated over full D1 history."""
    n = len(close)
    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * TOLERANCE_FRAC

    levels = []

    for t in range(n):
        rv = rvol[t]
        tol_t = tol[t]
        ct = close[t]

        # Birth / stack
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
                        "price": price,
                        "birth_bar": t,
                        "stack_weight": w,
                        "stack_count": 1,
                        "max_contributor_rvol": rv,
                        "cross_count": 0,
                        "last_contribution_bar": t,
                        "last_close_side": None,
                    })
                else:
                    matched_lvl["stack_weight"] += w
                    matched_lvl["stack_count"] += 1
                    if rv > matched_lvl["max_contributor_rvol"]:
                        matched_lvl["max_contributor_rvol"] = rv
                    matched_lvl["last_contribution_bar"] = t

        # Cross tracking
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

    return levels


def _extract_dates(df):
    """Best-effort extraction of a date array aligned to the bar axis."""
    n = len(df)
    # DatetimeIndex
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index.to_numpy()
    # 'date' column
    if "date" in df.columns:
        try:
            return pd.to_datetime(df["date"]).to_numpy()
        except Exception:
            pass
    # 'timestamp' column
    if "timestamp" in df.columns:
        try:
            return pd.to_datetime(df["timestamp"]).to_numpy()
        except Exception:
            pass
    return None


def plot_ticker(ticker, df, levels):
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    n = len(close)

    dates = _extract_dates(df)
    x = dates if dates is not None else np.arange(n)
    x0_val = x[0]
    x1_val = x[-1]

    fig, ax = plt.subplots(figsize=(18, 9))

    ax.fill_between(x, low, high, color="gray", alpha=0.15, linewidth=0,
                    label="HL range")
    ax.plot(x, close, color="black", linewidth=0.7, label="close")

    if levels:
        weights = np.array([lvl["stack_weight"] for lvl in levels])
        wmax = weights.max() if weights.max() > 0 else 1.0
    else:
        wmax = 1.0

    for lvl in levels:
        w = lvl["stack_weight"]
        norm = w / wmax
        alpha = 0.15 + 0.75 * norm
        lw = 0.3 + 2.7 * norm
        x_birth = x[lvl["birth_bar"]]
        ax.hlines(lvl["price"], x_birth, x1_val, colors="steelblue",
                  alpha=alpha, linewidth=lw)

    ax.set_title(
        f"{ticker}  MOC levels on D1 chart\n"
        f"{len(levels)} levels | line thickness + opacity scale with stack_weight | "
        f"span from birth bar to last bar",
        fontsize=11,
    )
    ax.set_ylabel("price", fontsize=10)
    ax.set_xlabel("date" if dates is not None else "bar index", fontsize=10)
    ax.set_xlim(x0_val, x1_val)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.85)
    fig.tight_layout()
    out_path = os.path.join(HERE, f"moc_levels_{ticker}.png")
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main():
    print("Loading universe_ohlcv_daily.pkl...")
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)

    for ticker in TICKERS:
        if ticker not in daily_cache:
            print(f"\n{ticker}: MISSING in cache — skipped")
            continue
        print(f"\n{ticker}:")
        df = daily_cache[ticker]
        n_bars = len(df)
        t0 = time.time()
        levels = build_moc_levels(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
        )
        dt = time.time() - t0
        total_stack = sum(lvl["stack_weight"] for lvl in levels)
        max_stack = max((lvl["stack_weight"] for lvl in levels), default=0.0)
        print(f"  n_bars={n_bars}  levels={len(levels)}  "
              f"total_stack_weight={total_stack:.1f}  "
              f"max_level_stack_weight={max_stack:.1f}  build={dt:.1f}s")
        out = plot_ticker(ticker, df, levels)
        print(f"  saved: {out}")


if __name__ == "__main__":
    main()
