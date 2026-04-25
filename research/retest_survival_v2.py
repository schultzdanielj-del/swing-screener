"""Per-ticker survival curve v2 — level aggregation + stricter thresholds.

v1 flat-curve problem: tracked every RVOL>1 H/L as a distinct level, but
levels clustered within tolerance overlapped each other. Bounce/breakthrough
outcomes at one "level" were measuring generic intraday behavior around
many nearby levels, not specific level-strength. And bounce threshold was
too small (0.113 x ATR) — most bars move that much anyway.

v2 fixes:
  1. Levels = unique price clusters. An RVOL>1 H/L print joins an existing
     level if within tolerance, else births a new one. Matches how the MOC
     primitive itself tracks levels.
  2. Bounce = >= 1 * ATR14 move within 5 forward bars (real reversal).
  3. Cross = close traverses beyond tolerance on opposite side (not just
     sign-flip within tolerance).
  4. Retest contact = bar whose range contacts tolerance zone, after close
     was >= 2 * ATR14 away at some prior bar.
"""
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

CACHE_OHLCV = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl"

TOLERANCE_FRAC = np.sqrt(1.0 / 78.0)
RVOL_WINDOW = 50
ATR_WINDOW = 14
RVOL_BIRTH_MIN = 2.0
RETEST_FWD_M = 10
BOUNCE_THRESHOLD_ATR = 2.0      # move away by >= 2 ATR for bounce
AWAY_TRIGGER_ATR = 2.0           # must have been >= 2 ATR away to register a retest
MIN_BUCKET_SAMPLE = 10


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
    n = len(close)
    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * TOLERANCE_FRAC

    # Build level set by walking forward. A level is a price cluster.
    # levels: list of dicts with 'price', 'birth_bar', 'cross_count',
    #   'last_away_dir', 'last_close_side' (sign of close-level vs tol).
    levels = []

    retest_cross_counts = []
    retest_outcomes = []

    for t in range(n):
        rv = rvol[t]
        tol_t = tol[t]
        atr_t = atr[t]
        ct = close[t]
        ht = high[t]
        lt = low[t]

        # 1. Level birth / stacking. Eligible H and L if RVOL > 1.
        if rv > RVOL_BIRTH_MIN:
            for price in (ht, lt):
                # Find nearest existing level within tolerance_t
                matched = False
                for lvl in levels:
                    if abs(lvl['price'] - price) <= tol_t:
                        matched = True
                        break
                if not matched:
                    levels.append({
                        'price': price,
                        'birth_bar': t,
                        'cross_count': 0,
                        'last_away_dir': None,
                        'last_close_side': None,
                    })

        # 2. For each level, update cross_count, check retest, classify.
        for lvl in levels:
            if t <= lvl['birth_bar']:
                continue
            p = lvl['price']

            # Cross: close traverses >= tol beyond level on opposite side
            # from last close_side.
            dist = ct - p
            if dist > tol_t:
                curr_side = 1
            elif dist < -tol_t:
                curr_side = -1
            else:
                curr_side = 0  # inside tolerance band, neutral

            if lvl['last_close_side'] is not None and curr_side != 0:
                if curr_side != lvl['last_close_side'] and lvl['last_close_side'] != 0:
                    lvl['cross_count'] += 1
            if curr_side != 0:
                lvl['last_close_side'] = curr_side

            # Update "far away" tracker
            if abs(dist) >= AWAY_TRIGGER_ATR * atr_t:
                lvl['last_away_dir'] = 1 if dist > 0 else -1

            # Retest contact detection: bar range hits tolerance zone
            contacts = (lt <= p + tol_t) and (ht >= p - tol_t)
            if not contacts or lvl['last_away_dir'] is None:
                continue

            approach_dir = lvl['last_away_dir']

            # Classify within M forward bars
            outcome = None
            for look in range(t + 1, min(n, t + 1 + RETEST_FWD_M)):
                fc = close[look]
                fdist = fc - p
                # Bounce: moved away from level by >= BOUNCE_THRESHOLD_ATR * ATR
                # in approach direction.
                if approach_dir > 0 and fdist >= BOUNCE_THRESHOLD_ATR * atr[look]:
                    outcome = 1
                    break
                if approach_dir < 0 and fdist <= -BOUNCE_THRESHOLD_ATR * atr[look]:
                    outcome = 1
                    break
                # Breakthrough: moved to opposite side by >= 1 * ATR
                if approach_dir > 0 and fdist <= -BOUNCE_THRESHOLD_ATR * atr[look]:
                    outcome = 0
                    break
                if approach_dir < 0 and fdist >= BOUNCE_THRESHOLD_ATR * atr[look]:
                    outcome = 0
                    break

            if outcome is not None:
                retest_cross_counts.append(lvl['cross_count'])
                retest_outcomes.append(outcome)
                lvl['last_away_dir'] = None  # reset, need to go far again

    # Bucket by cross_count
    buckets = {}
    for cc, out in zip(retest_cross_counts, retest_outcomes):
        if cc not in buckets:
            buckets[cc] = [0, 0]
        if out == 1:
            buckets[cc][0] += 1
        else:
            buckets[cc][1] += 1

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


def main():
    print("Loading universe_ohlcv_daily.pkl...")
    t0 = time.time()
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)
    print(f"Loaded in {time.time()-t0:.1f}s. {len(daily_cache)} tickers.")

    tickers = ["AAPL", "MSFT", "TSLA", "RIVN", "SPY"]
    for ticker in tickers:
        if ticker not in daily_cache:
            print(f"\n{ticker}: not in cache")
            continue
        df = daily_cache[ticker]
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
