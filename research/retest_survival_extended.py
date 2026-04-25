"""Extended-range survival curve report. v1 loose params (RVOL>1 births,
1x ATR bounce threshold, 2-bar forward window) — large sample sizes give
stable estimates. Show S(N) across full range of observed cross_count
buckets with adequate samples."""
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
BOUNCE_THRESHOLD_ATR = None      # use 1 * tolerance (= 0.113 * ATR)
AWAY_TRIGGER_ATR = 2.0 * TOLERANCE_FRAC  # tolerance-relative
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
                        'last_away_dir': None, 'last_close_side': None,
                    })

        for lvl in levels:
            if t <= lvl['birth_bar']:
                continue
            p = lvl['price']

            dist = ct - p
            if dist > tol_t:
                curr_side = 1
            elif dist < -tol_t:
                curr_side = -1
            else:
                curr_side = 0
            if lvl['last_close_side'] is not None and curr_side != 0:
                if curr_side != lvl['last_close_side'] and lvl['last_close_side'] != 0:
                    lvl['cross_count'] += 1
            if curr_side != 0:
                lvl['last_close_side'] = curr_side

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

    tickers = ["AAPL", "MSFT", "TSLA", "RIVN", "SPY", "F", "KO", "NVDA", "META", "RGTU"]
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

        print(f"\n{ticker}  (n_bars={len(close)}, elapsed={elapsed:.2f}s)")
        total_retests = sum(nb + nk for nb, nk in buckets.values())
        print(f"  retests: {total_retests}  buckets_with_sample>={MIN_BUCKET_SAMPLE}: "
              f"{len(survival)}  max_N_observed: "
              f"{max(buckets.keys()) if buckets else 0}")
        print(f"  {'cc':>4} {'n_bounce':>9} {'n_break':>8} {'total':>6} "
              f"{'P(bounce)':>10} {'S(N)':>7}")
        # Print deciles of observed N to keep output tight
        sorted_cc = sorted(survival.keys())
        if len(sorted_cc) > 20:
            step = max(1, len(sorted_cc) // 20)
            to_print = sorted_cc[::step] + [sorted_cc[-1]]
            to_print = sorted(set(to_print))
        else:
            to_print = sorted_cc
        for cc in to_print:
            nb, nk = buckets[cc]
            total = nb + nk
            p = nb / total
            print(f"  {cc:>4d} {nb:>9d} {nk:>8d} {total:>6d} "
                  f"{p:>10.3f} {survival[cc]:>7.3f}")


if __name__ == "__main__":
    main()
