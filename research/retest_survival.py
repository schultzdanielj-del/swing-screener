"""Per-ticker survival curve derivation for MOC primitive.

S(N) = P(bounce on retest | cross_count = N), normalized so S(0) = 1.0.

For a given ticker's full OHLCV history:
  1. Identify every H/L print with RVOL > 1 as a level-birth event.
  2. Walk forward, track cross_count per level (D1 close crosses through).
  3. Identify retest events: price was >= 2*tol away from level, then contacts
     (bar range intersects tolerance zone).
  4. Classify each retest within M forward bars: bounce (price returns to
     approach side by >= tol) vs breakthrough (price crosses to opposite side
     by >= tol) vs inconclusive (stays in chop band, excluded from sample).
  5. Bucket by cross_count at time of retest, compute P(bounce|N).
  6. Normalize by S(0) = P(bounce|0).

Tolerance = ATR14 * sqrt(1/78) ≈ ATR14 * 0.113 per bar.
RVOL = volume / SMA(volume, 50). Level born if RVOL > 1.
Bounce threshold = 1 * tolerance.
Retest forward window M = 2 bars.
Minimum sample per bucket for inclusion in output = 10 observations.
"""
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

CACHE_OHLCV = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl"

TOLERANCE_FRAC = np.sqrt(1.0 / 78.0)  # ~0.1132
RVOL_WINDOW = 50
ATR_WINDOW = 14
RVOL_BIRTH_MIN = 1.0
RETEST_FWD_M = 2
MIN_BUCKET_SAMPLE = 10

# For the "away from level" requirement before a bar can count as a retest.
AWAY_TOL_MULT = 2.0


def compute_rvol(volume):
    sma = pd.Series(volume).rolling(RVOL_WINDOW, min_periods=1).mean()
    return (volume / sma.values).astype(float)


def compute_atr14(high, low, close):
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(ATR_WINDOW, min_periods=1).mean().values
    return atr.astype(float)


def compute_survival_curve(high, low, close, volume, verbose=False):
    """Returns dict {cross_count: (n_bounce, n_break)} + normalized S(N) dict."""
    n = len(close)
    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * TOLERANCE_FRAC

    # Level births: arrays of (birth_bar, price, side) for all RVOL > 1 H/L prints.
    mask = rvol > RVOL_BIRTH_MIN
    birth_bars = np.nonzero(mask)[0]
    n_levels = 2 * len(birth_bars)

    retest_cross_counts = []
    retest_outcomes = []  # 1 = bounce, 0 = breakthrough

    for i, birth_bar in enumerate(birth_bars):
        for side_kind, level_price in (("H", high[birth_bar]), ("L", low[birth_bar])):
            if birth_bar + 1 >= n:
                continue
            cross_count = 0
            last_side = None
            last_away_dir = None  # +1 = was above level and far; -1 = was below and far

            for t in range(birth_bar + 1, n):
                ct = close[t]
                ht = high[t]
                lt = low[t]
                tol_t = tol[t]

                curr_side = 1 if ct > level_price else -1
                if last_side is not None and curr_side != last_side:
                    cross_count += 1
                last_side = curr_side

                # Update "was far away" tracker
                dist = ct - level_price
                if abs(dist) > AWAY_TOL_MULT * tol_t:
                    last_away_dir = 1 if dist > 0 else -1

                # Retest contact: bar range intersects tolerance zone AND we
                # recently were far away from the level.
                contacts = (lt <= level_price + tol_t) and (ht >= level_price - tol_t)
                if not contacts:
                    continue
                if last_away_dir is None:
                    continue  # never been far away yet — not a retest

                approach_dir = last_away_dir

                # Classify within M forward bars
                outcome = None
                for look in range(t + 1, min(n, t + 1 + RETEST_FWD_M)):
                    fc = close[look]
                    fdist = fc - level_price
                    ftol = tol[look]
                    if abs(fdist) < ftol:
                        continue  # still chopping around level
                    future_dir = 1 if fdist > 0 else -1
                    if future_dir == approach_dir:
                        outcome = 1  # bounce: returned to approach side
                    else:
                        outcome = 0  # breakthrough: crossed to other side
                    break

                if outcome is not None:
                    retest_cross_counts.append(cross_count)
                    retest_outcomes.append(outcome)
                    # Reset the away tracker so we don't double-count consecutive bars
                    last_away_dir = None

    # Bucket by cross_count
    buckets = {}
    for cc, out in zip(retest_cross_counts, retest_outcomes):
        if cc not in buckets:
            buckets[cc] = [0, 0]
        buckets[cc][1 - out] += 0  # placeholder
        if out == 1:
            buckets[cc][0] += 1
        else:
            buckets[cc][1] += 1

    # P(bounce | N)
    p_bounce = {}
    for cc, (nb, nk) in buckets.items():
        total = nb + nk
        if total >= MIN_BUCKET_SAMPLE:
            p_bounce[cc] = nb / total

    if not p_bounce or 0 not in p_bounce:
        return buckets, {}

    s0 = p_bounce[0]
    if s0 <= 0:
        return buckets, {}

    survival = {cc: min(1.0, p / s0) for cc, p in p_bounce.items()}
    return buckets, survival


def load_ticker_ohlcv(ticker, daily_cache):
    """daily_cache is the full universe pickle. Extract one ticker's DataFrame."""
    if ticker not in daily_cache:
        return None
    df = daily_cache[ticker]
    return df


def main():
    print("Loading universe_ohlcv_daily.pkl (~1 GB)...")
    t0 = time.time()
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)
    print(f"Loaded in {time.time()-t0:.1f}s. {len(daily_cache)} tickers.")

    tickers = ["AAPL", "MSFT", "TSLA", "RIVN", "SPY"]
    for ticker in tickers:
        df = load_ticker_ohlcv(ticker, daily_cache)
        if df is None:
            print(f"\n{ticker}: not in cache")
            continue

        # Figure out columns
        high = df['high'].values.astype(float)
        low = df['low'].values.astype(float)
        close = df['close'].values.astype(float)
        volume = df['volume'].values.astype(float)

        t0 = time.time()
        buckets, survival = compute_survival_curve(high, low, close, volume)
        elapsed = time.time() - t0

        print(f"\n{ticker}  (n_bars={len(close)}, elapsed={elapsed:.2f}s)")
        total_retests = sum(nb + nk for nb, nk in buckets.values())
        print(f"  total retest events: {total_retests}")
        print(f"  {'cross_count':>12} {'n_bounce':>9} {'n_break':>8} "
              f"{'P(bounce|N)':>12} {'S(N)':>7}")
        for cc in sorted(buckets.keys())[:15]:
            nb, nk = buckets[cc]
            total = nb + nk
            if total == 0:
                continue
            p = nb / total
            s_str = f"{survival.get(cc, float('nan')):.3f}" if cc in survival else "n/a"
            print(f"  {cc:>12d} {nb:>9d} {nk:>8d} {p:>12.3f} {s_str:>7}")


if __name__ == "__main__":
    main()
