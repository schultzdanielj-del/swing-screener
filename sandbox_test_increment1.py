"""
Sandbox Test — Increment 1: Vectorized 2D Intermediates vs Pandas Reference.

Loads 50 real tickers from universe_ohlcv_5yr.pkl, computes all base indicators
both ways (pandas per-ticker vs numpy 2D batch), compares values.

Pass criteria: all values match within float32 tolerance (1e-4 relative).

Usage:
    python sandbox_test_increment1.py
"""

import os
import sys
import time
import pickle
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_runner"))

from scripts.profiling_engine import (
    sma, ema, hma, rolling_max, rolling_min, rolling_sum,
    atr, rsi, stochastic_k, cci, adx, di_plus, di_minus,
    bop, obv, macd, bollinger_top, bollinger_bot, stddev,
    aroon_up, aroon_down, chaikin_money_flow, kaufman_efficiency,
    true_range,
)
from local_runner.vectorized_indicators import (
    sma_2d, ema_2d, hma_2d, rolling_max_2d, rolling_min_2d, rolling_sum_2d,
    rolling_std_2d, true_range_2d, atr_2d, adr_2d, rsi_2d, stochastic_2d,
    adx_2d, di_plus_2d, di_minus_2d, cci_2d, macd_2d,
    bollinger_top_2d, bollinger_bot_2d, obv_2d, bop_2d,
    aroon_up_2d, aroon_down_2d, cmf_2d, kaufman_eff_2d,
    count_true_2d, since_true_2d, true_in_row_2d,
)


CACHE_PATH = os.path.join("local_runner", "cache", "universe_ohlcv_5yr.pkl")
N_TEST_TICKERS = 50
RTOL = 1e-4  # relative tolerance for float32 comparison
ATOL = 1e-6  # absolute tolerance for near-zero values


def load_test_data():
    """Load N_TEST_TICKERS from the cache, return list of DataFrames and 2D arrays."""
    with open(CACHE_PATH, "rb") as f:
        cache = pickle.load(f)

    # Pick tickers with varying bar counts
    tickers = sorted(cache.keys())
    # Sample: first 20, middle 15, last 15
    n = len(tickers)
    sample = tickers[:20] + tickers[n//2:n//2+15] + tickers[-15:]
    sample = sample[:N_TEST_TICKERS]

    dfs = []
    for t in sample:
        df = cache[t].copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        dfs.append((t, df))

    return dfs


def build_2d_arrays(dfs):
    """Build padded 2D arrays from list of DataFrames."""
    max_bars = max(len(df) for _, df in dfs)
    n_t = len(dfs)

    O = np.full((n_t, max_bars), np.nan)
    H = np.full((n_t, max_bars), np.nan)
    L = np.full((n_t, max_bars), np.nan)
    C = np.full((n_t, max_bars), np.nan)
    V = np.full((n_t, max_bars), np.nan)

    for i, (_, df) in enumerate(dfs):
        n = len(df)
        # Right-align: pad leading NaNs
        offset = max_bars - n
        O[i, offset:] = df["open"].values
        H[i, offset:] = df["high"].values
        L[i, offset:] = df["low"].values
        C[i, offset:] = df["close"].values
        V[i, offset:] = df["volume"].values

    return O, H, L, C, V, max_bars


def compare(name, pandas_vals, numpy_vals, ticker_idx, offset):
    """Compare pandas 1D result against numpy 2D result for one ticker.
    
    Returns (n_compared, n_mismatched, worst_rel_error).
    """
    n = len(pandas_vals)
    np_slice = numpy_vals[ticker_idx, offset:offset + n]

    # Both NaN = match. One NaN = mismatch.
    p_nan = np.isnan(pandas_vals)
    n_nan = np.isnan(np_slice)

    both_nan = p_nan & n_nan
    one_nan = p_nan ^ n_nan
    both_valid = ~p_nan & ~n_nan

    n_compared = int(both_valid.sum())
    n_nan_mismatch = int(one_nan.sum())

    if n_compared == 0:
        return n_compared, n_nan_mismatch, 0.0

    p_valid = pandas_vals[both_valid]
    n_valid = np_slice[both_valid]

    abs_diff = np.abs(p_valid - n_valid)
    denom = np.maximum(np.abs(p_valid), ATOL)
    rel_err = abs_diff / denom
    worst = float(np.max(rel_err))

    n_bad = int(np.sum(rel_err > RTOL))

    return n_compared, n_nan_mismatch + n_bad, worst


def run_test(name, pandas_fn, numpy_result, dfs, offset_map):
    """Run comparison for one indicator across all test tickers."""
    total_compared = 0
    total_mismatched = 0
    worst_err = 0.0
    worst_ticker = ""

    for i, (ticker, df) in enumerate(dfs):
        try:
            p_vals = np.asarray(pandas_fn(df), dtype=np.float64)
        except Exception as e:
            print(f"  WARN: pandas failed for {ticker}: {e}")
            continue

        offset = offset_map[i]
        n_comp, n_mis, worst = compare(name, p_vals, numpy_result, i, offset)
        total_compared += n_comp
        total_mismatched += n_mis
        if worst > worst_err:
            worst_err = worst
            worst_ticker = ticker

    status = "PASS" if total_mismatched == 0 else "FAIL"
    print(f"  {status}  {name:<30s}  compared={total_compared:>8,}  "
          f"mismatched={total_mismatched:>6}  worst_rel_err={worst_err:.2e}"
          f"{'  ('+worst_ticker+')' if worst_err > RTOL else ''}")

    return total_mismatched == 0


def main():
    print("=" * 80)
    print("  SANDBOX TEST — Increment 1: Vectorized 2D Intermediates")
    print("=" * 80)

    # Load data
    print(f"\nLoading {N_TEST_TICKERS} tickers from cache...")
    dfs = load_test_data()
    print(f"  Loaded {len(dfs)} tickers, bar range: "
          f"{min(len(df) for _, df in dfs)}-{max(len(df) for _, df in dfs)}")

    # Build 2D arrays
    print("Building 2D arrays...")
    O, H, L, C, V, max_bars = build_2d_arrays(dfs)
    print(f"  Shape: ({O.shape[0]}, {O.shape[1]})")

    # Offset map: how many leading NaN bars per ticker
    offset_map = [max_bars - len(df) for _, df in dfs]

    # Run tests
    print("\nRunning comparisons...\n")
    t0 = time.time()
    all_pass = True

    # --- SMA ---
    for period in [5, 10, 20, 50, 200]:
        result = sma_2d(C, period)
        ok = run_test(f"sma({period})",
                      lambda df, p=period: sma(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- EMA ---
    for period in [5, 9, 12, 21, 50, 200]:
        result = ema_2d(C, period)
        ok = run_test(f"ema({period})",
                      lambda df, p=period: ema(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- HMA ---
    for period in [9, 21]:
        result = hma_2d(C, period)
        ok = run_test(f"hma({period})",
                      lambda df, p=period: hma(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Rolling max/min ---
    for period in [5, 20, 50, 120]:
        result = rolling_max_2d(H, period)
        ok = run_test(f"rolling_max(H,{period})",
                      lambda df, p=period: rolling_max(df["high"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

        result = rolling_min_2d(L, period)
        ok = run_test(f"rolling_min(L,{period})",
                      lambda df, p=period: rolling_min(df["low"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Rolling sum ---
    for period in [10, 20]:
        result = rolling_sum_2d(V, period)
        ok = run_test(f"rolling_sum(V,{period})",
                      lambda df, p=period: rolling_sum(df["volume"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Rolling std ---
    for period in [20]:
        result = rolling_std_2d(C, period)
        ok = run_test(f"rolling_std(C,{period})",
                      lambda df, p=period: df["close"].rolling(p, min_periods=p).std().values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- True Range ---
    tr_result = true_range_2d(H, L, C)
    ok = run_test("true_range",
                  lambda df: true_range(df).values,
                  tr_result, dfs, offset_map)
    all_pass &= ok

    # --- ATR ---
    for period in [14]:
        result = atr_2d(H, L, C, period)
        ok = run_test(f"atr({period})",
                      lambda df, p=period: atr(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- ADR ---
    for period in [14]:
        result = adr_2d(H, L, period)
        ok = run_test(f"adr({period})",
                      lambda df, p=period: sma(df["high"] - df["low"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- RSI ---
    for period in [6, 14]:
        result = rsi_2d(C, period)
        ok = run_test(f"rsi({period})",
                      lambda df, p=period: rsi(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Stochastic ---
    for period in [14]:
        result = stochastic_2d(H, L, C, period)
        ok = run_test(f"stochastic({period})",
                      lambda df, p=period: stochastic_k(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- ADX ---
    for period in [14]:
        result = adx_2d(H, L, C, period)
        ok = run_test(f"adx({period})",
                      lambda df, p=period: adx(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- DI+ / DI- ---
    for period in [14]:
        result = di_plus_2d(H, L, C, period)
        ok = run_test(f"di_plus({period})",
                      lambda df, p=period: di_plus(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

        result = di_minus_2d(H, L, C, period)
        ok = run_test(f"di_minus({period})",
                      lambda df, p=period: di_minus(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- CCI ---
    for period in [14, 20]:
        result = cci_2d(H, L, C, period)
        ok = run_test(f"cci({period})",
                      lambda df, p=period: cci(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- MACD ---
    result = macd_2d(C, 12, 26)
    ok = run_test("macd(12,26)",
                  lambda df: macd(df["close"], 12, 26).values,
                  result, dfs, offset_map)
    all_pass &= ok

    # --- Bollinger ---
    for period in [20]:
        result = bollinger_top_2d(C, period)
        ok = run_test(f"bollinger_top({period})",
                      lambda df, p=period: bollinger_top(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

        result = bollinger_bot_2d(C, period)
        ok = run_test(f"bollinger_bot({period})",
                      lambda df, p=period: bollinger_bot(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- OBV ---
    result = obv_2d(C, V)
    ok = run_test("obv",
                  lambda df: obv(df).values,
                  result, dfs, offset_map)
    all_pass &= ok

    # --- BOP ---
    for period in [14]:
        result = bop_2d(O, H, L, C, period)
        ok = run_test(f"bop({period})",
                      lambda df, p=period: bop(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Aroon ---
    for period in [14, 25]:
        result = aroon_up_2d(H, period)
        ok = run_test(f"aroon_up({period})",
                      lambda df, p=period: aroon_up(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

        result = aroon_down_2d(L, period)
        ok = run_test(f"aroon_down({period})",
                      lambda df, p=period: aroon_down(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- CMF ---
    for period in [20]:
        result = cmf_2d(H, L, C, V, period)
        ok = run_test(f"cmf({period})",
                      lambda df, p=period: chaikin_money_flow(df, p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Kaufman Efficiency ---
    for period in [10]:
        result = kaufman_eff_2d(C, period)
        ok = run_test(f"kaufman_eff({period})",
                      lambda df, p=period: kaufman_efficiency(df["close"], p).values,
                      result, dfs, offset_map)
        all_pass &= ok

    # --- Boolean aggregates ---
    # Test with a simple boolean: close > SMA(50)
    sma50 = sma_2d(C, 50)
    bool_arr = C > sma50

    # count_true
    from scripts.profiling_engine import count_true as ct_pandas, since_true as st_pandas, true_in_row as tir_pandas
    for period in [10, 20]:
        ct_result = count_true_2d(bool_arr, period)
        ok = run_test(f"count_true(c>sma50,{period})",
                      lambda df, p=period: ct_pandas(
                          df["close"] > sma(df["close"], 50), p).values,
                      ct_result, dfs, offset_map)
        all_pass &= ok

    # since_true
    for period in [20]:
        st_result = since_true_2d(bool_arr, period)
        ok = run_test(f"since_true(c>sma50,{period})",
                      lambda df, p=period: st_pandas(
                          df["close"] > sma(df["close"], 50), p).values,
                      st_result, dfs, offset_map)
        all_pass &= ok

    # true_in_row
    for period in [20]:
        tir_result = true_in_row_2d(bool_arr, period)
        ok = run_test(f"true_in_row(c>sma50,{period})",
                      lambda df, p=period: tir_pandas(
                          df["close"] > sma(df["close"], 50), p).values,
                      tir_result, dfs, offset_map)
        all_pass &= ok

    elapsed = time.time() - t0
    print(f"\n{'=' * 80}")
    if all_pass:
        print(f"  ALL TESTS PASSED  ({elapsed:.1f}s)")
    else:
        print(f"  SOME TESTS FAILED  ({elapsed:.1f}s)")
    print(f"{'=' * 80}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
