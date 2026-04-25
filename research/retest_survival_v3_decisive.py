"""Decisive-cross survival curve.

v1/v2 counted any close sign-flip as a cross. This v3 counts only decisive
breach-and-hold events:
  - close >= 1 ATR past level (the "breach" move)
  - close[t+1] and close[t+2] both remain on the same far side beyond
    tolerance (the "hold" confirmation)
  - Direction-flip vs previous confirmed side (otherwise it's continuation,
    not a new cross)

Everything else (level aggregation, retest detection, bounce classification)
identical to the v1 loose params that produced large sample sizes.
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
RVOL_BIRTH_MIN = 1.0
RETEST_FWD_M = 2
BREACH_ATR = 1.0       # close must be at least this many ATR past level
HOLD_BARS = 2          # number of bars after breach that must stay on same side
MIN_BUCKET_SAMPLE = 30


def compute_rvol(volume):
    sma = pd.Series(volume).rolling(RVOL_WINDOW, min_periods=1).mean()
    out = (volume / sma.values).astype(float)
    out[~np.isfinite(out)] = 0
    return out


def compute_atr14(high, low, close):
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    return pd.Series(tr).rolling(ATR_WINDOW, min_periods=1).mean().values.astype(float)


def side_of(dist, tol_t):
    if dist > tol_t:
        return 1
    if dist < -tol_t:
        return -1
    return 0


def compute_survival(high, low, close, volume):
    n = len(close)
    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * TOLERANCE_FRAC

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

        if rv > RVOL_BIRTH_MIN:
            for price in (ht, lt):
                matched = False
                for lvl in levels:
                    if abs(lvl['price'] - price) <= tol_t:
                        matched = True
                        break
                if not matched:
                    levels.append({
                        'price': price, 'birth_bar': t, 'cross_count': 0,
                        'last_away_dir': None, 'last_breach_side': None,
                    })

        for lvl in levels:
            if t <= lvl['birth_bar']:
                continue
            p = lvl['price']
            dist = ct - p

            # Decisive breach detection: close is >= 1 ATR past level,
            # AND close[t+1..t+HOLD_BARS] are all on same far side.
            if abs(dist) >= BREACH_ATR * atr_t and t + HOLD_BARS < n:
                breach_side = 1 if dist > 0 else -1
                hold_ok = True
                for k in range(1, HOLD_BARS + 1):
                    fc = close[t + k]
                    fside = side_of(fc - p, tol[t + k])
                    if fside != breach_side:
                        hold_ok = False
                        break
                if hold_ok:
                    if lvl['last_breach_side'] is not None and \
                       lvl['last_breach_side'] != breach_side:
                        lvl['cross_count'] += 1
                    lvl['last_breach_side'] = breach_side

            # Retest machinery — unchanged from v1 (loose params)
            if abs(dist) >= 2.0 * tol_t:
                lvl['last_away_dir'] = 1 if dist > 0 else -1

            contacts = (lt <= p + tol_t) and (ht >= p - tol_t)
            if not contacts or lvl['last_away_dir'] is None:
                continue

            approach_dir = lvl['last_away_dir']
            outcome = None
            for look in range(t + 1, min(n, t + 1 + RETEST_FWD_M)):
                fc = close[look]
                fdist = fc - p
                ftol = tol[look]
                if abs(fdist) < ftol:
                    continue
                future_dir = 1 if fdist > 0 else -1
                outcome = 1 if future_dir == approach_dir else 0
                break

            if outcome is not None:
                retest_cross_counts.append(lvl['cross_count'])
                retest_outcomes.append(outcome)
                lvl['last_away_dir'] = None

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
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)

    tickers = ["AAPL", "MSFT", "TSLA", "NVDA", "META", "SPY", "F", "KO", "RIVN"]
    print(f"\nStrict cross: close >= {BREACH_ATR} ATR past level AND held for "
          f"{HOLD_BARS} bars. Retest params = v1 loose (1 tol bounce, 2-bar window).")
    print("=" * 72)
    for ticker in tickers:
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        high = df['high'].values.astype(float)
        low = df['low'].values.astype(float)
        close = df['close'].values.astype(float)
        volume = df['volume'].values.astype(float)

        t0 = time.time()
        buckets, survival = compute_survival(high, low, close, volume)
        elapsed = time.time() - t0

        print(f"\n{ticker}  (n={len(close)}, elapsed={elapsed:.2f}s, "
              f"decisive_cross buckets w/ sample>={MIN_BUCKET_SAMPLE}: {len(survival)}, "
              f"max_N observed: {max(buckets.keys()) if buckets else 0})")
        total = sum(nb + nk for nb, nk in buckets.values())
        print(f"  total retest events: {total}")
        print(f"  {'cc':>4} {'n_bnc':>7} {'n_brk':>7} {'total':>7} "
              f"{'P(bnc)':>7} {'S(N)':>7}")
        for cc in sorted(survival.keys()):
            nb, nk = buckets[cc]
            tot = nb + nk
            print(f"  {cc:>4d} {nb:>7d} {nk:>7d} {tot:>7d} "
                  f"{nb/tot:>7.3f} {survival[cc]:>7.3f}")


if __name__ == "__main__":
    main()
