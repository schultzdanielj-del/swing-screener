"""
regime_vector(date) -> pd.Series of 24 named floats.

Assembles the regime-similarity input vector for a given trading date
from four read-only sources:

  - regime_meter/cache/naaim_daily.parquet               (worktree-local)
  - regime_meter/cache/breadth_daily.parquet             (worktree-local)
  - <main-repo>/local_runner/cache/market_series/*.npz   (ext columns only)
  - <main-repo>/local_runner/cache/market_ohlcv.pkl      (all raw closes)

Raises ValueError on any sourcing/validation failure.

Reads main-repo caches read-only. Writes nothing.
"""
from __future__ import annotations

import os
import pickle
import sys
from functools import lru_cache

import numpy as np
import pandas as pd


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")

NAAIM_PARQUET   = os.path.join(CACHE_DIR, "naaim_daily.parquet")
BREADTH_PARQUET = os.path.join(CACHE_DIR, "breadth_daily.parquet")

MAIN_REPO_CACHE   = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
MARKET_OHLCV_PKL  = os.path.join(MAIN_REPO_CACHE, "market_ohlcv.pkl")
MARKET_SERIES_DIR = os.path.join(MAIN_REPO_CACHE, "market_series")

# Main repo import root (for live generate_all()).
MAIN_REPO_ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
if MAIN_REPO_ROOT not in sys.path:
    sys.path.insert(0, MAIN_REPO_ROOT)

# --- Window / lookbacks ----------------------------------------------------
HISTORY_START      = "2016-01-01"
TREND_SLOPE_BARS   = 20
SMA200_BARS        = 200
VIX_PCTILE_BARS    = 1259      # trailing bars BEFORE target; window = 1260 incl. target
SECTOR_RETURN_BARS = 20

# --- Source key maps -------------------------------------------------------
# market_ohlcv.pkl uses dotted/dashed ticker keys; map them by role.
KEY_SPY  = "SPY"
KEY_QQQ  = "QQQ"
KEY_RSP  = "RSP"
KEY_DXY  = "DXY.INDX"
KEY_GLD  = "GLD"
KEY_BTC  = "BTC-USD.CC"
KEY_VIX  = "VIX.INDX"
KEY_VX3M = "VIX3M.INDX"
KEY_HYG  = "HYG"
KEY_IEF  = "IEF"
KEY_XLY  = "XLY"
KEY_XLP  = "XLP"
KEY_T2T10 = "T10Y2Y_CALC"

SECTOR_KEYS = [
    "XLE", "XLB", "XLI", "XLY", "XLP",
    "XLV", "XLF", "XLK", "XLU", "XLRE", "XLC",
]

# Instruments we need a close-series-on-SPY-calendar for.
ALL_OHLCV_KEYS = sorted(set([
    KEY_SPY, KEY_QQQ, KEY_RSP,
    KEY_DXY, KEY_GLD, KEY_BTC,
    KEY_VIX, KEY_VX3M,
    KEY_HYG, KEY_IEF,
    KEY_XLY, KEY_XLP,
    KEY_T2T10,
] + SECTOR_KEYS))

# --- Output column order (24) ---------------------------------------------
OUTPUT_COLUMNS = [
    "spy_ext50",
    "spy_ext200",
    "qqq_ext50",
    "qqq_ext200",
    "spy_trend_slope_20",
    "qqq_trend_slope_20",
    "dxy_trend_slope_20",
    "gold_trend_slope_20",
    "btc_trend_slope_20",
    "vix_pctile_5y",
    "vix_vix3m_ratio",
    "pct_above_50ma",
    "sector_return_dispersion_20",
    "xly_xlp_ratio",
    "hyg_ief_ratio",
    "yield_2s10s_spread",
    "dxy_dist_200ma_pct",
    "gold_dist_200ma_pct",
    "btc_dist_200ma_pct",
    "stockbee_4pct_ratio_10d",
    "stockbee_25pct_up_count_63d",
    "stockbee_25pct_down_count_63d",
    "rsp_spy_ratio",
    "naaim_mean",
]
assert len(OUTPUT_COLUMNS) == 24


# --- Source loaders (cached at module level) ------------------------------
@lru_cache(maxsize=1)
def _load_spy_calendar() -> pd.DatetimeIndex:
    cache = _load_market_ohlcv()
    if KEY_SPY not in cache:
        raise ValueError(f"{KEY_SPY!r} not in market_ohlcv.pkl")
    dates = pd.to_datetime(cache[KEY_SPY]["date"]).sort_values().reset_index(drop=True)
    dates = dates[dates >= pd.Timestamp(HISTORY_START)].reset_index(drop=True)
    return pd.DatetimeIndex(dates)


@lru_cache(maxsize=1)
def _load_market_ohlcv() -> dict:
    if not os.path.exists(MARKET_OHLCV_PKL):
        raise ValueError(f"market_ohlcv.pkl missing at {MARKET_OHLCV_PKL}")
    with open(MARKET_OHLCV_PKL, "rb") as f:
        return pickle.load(f)


@lru_cache(maxsize=1)
def _load_close_matrix() -> pd.DataFrame:
    """All needed instruments' closes reindexed to SPY calendar, forward-filled.

    Columns = ALL_OHLCV_KEYS. Index = SPY trading-day DatetimeIndex.
    FRED/BTC series get forward-filled to cover non-trading-day gaps; the
    target-date row is always a SPY trading day so the value is well-defined.
    """
    cache = _load_market_ohlcv()
    spy_dates = _load_spy_calendar()

    cols = {}
    for k in ALL_OHLCV_KEYS:
        if k not in cache:
            raise ValueError(f"{k!r} missing from market_ohlcv.pkl")
        df = cache[k]
        s = (
            df.set_index(pd.to_datetime(df["date"]))["close"]
            .sort_index()
            .reindex(spy_dates, method="ffill")
        )
        cols[k] = s
    return pd.DataFrame(cols, index=spy_dates)


@lru_cache(maxsize=1)
def _load_expression_name_index() -> list[str]:
    from local_runner.brute_expressions import generate_all
    return [e["name"] for e in generate_all()]


@lru_cache(maxsize=4)
def _load_npz(ticker: str) -> tuple[np.ndarray, pd.DatetimeIndex]:
    path = os.path.join(MARKET_SERIES_DIR, f"{ticker}.npz")
    if not os.path.exists(path):
        raise ValueError(f"market_series npz missing at {path}")
    z = np.load(path, allow_pickle=True)
    data  = z["data"]
    dates = pd.DatetimeIndex(pd.to_datetime(z["dates"]))

    expected = len(_load_expression_name_index())
    if data.shape[1] != expected:
        raise ValueError(
            f"{ticker}.npz column count {data.shape[1]} != "
            f"live generate_all() length {expected}; cache is stale or "
            "brute_expressions has been modified."
        )
    if data.shape[0] != len(dates):
        raise ValueError(
            f"{ticker}.npz row count {data.shape[0]} != dates length {len(dates)}"
        )
    return data, dates


@lru_cache(maxsize=1)
def _load_naaim_daily() -> pd.DataFrame:
    if not os.path.exists(NAAIM_PARQUET):
        raise ValueError(f"naaim parquet missing at {NAAIM_PARQUET} "
                         "(run regime_meter/naaim_ingest.py)")
    df = pd.read_parquet(NAAIM_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


@lru_cache(maxsize=1)
def _load_breadth_daily() -> pd.DataFrame:
    if not os.path.exists(BREADTH_PARQUET):
        raise ValueError(f"breadth parquet missing at {BREADTH_PARQUET} "
                         "(run regime_meter/breadth_ingest.py)")
    df = pd.read_parquet(BREADTH_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


# --- Per-column computers --------------------------------------------------
def _ext_col(ticker: str, expr_name: str, target: pd.Timestamp) -> float:
    data, dates = _load_npz(ticker)
    names = _load_expression_name_index()
    if expr_name not in names:
        raise ValueError(f"expression {expr_name!r} not in generate_all()")
    col_idx = names.index(expr_name)

    pos = dates.get_indexer([target])[0]
    if pos < 0:
        raise ValueError(f"target {target.date()} not in {ticker}.npz dates")
    return float(data[pos, col_idx])


def _trend_slope_20(close: pd.Series, target: pd.Timestamp) -> float:
    end = close.index.get_loc(target)
    start = end - (TREND_SLOPE_BARS - 1)
    if start < 0:
        raise ValueError(f"insufficient history for 20-bar trend slope at {target.date()}")
    window = close.iloc[start : end + 1].to_numpy(dtype=float)
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        raise ValueError(f"non-positive or non-finite close in trend-slope window ending {target.date()}")
    x = np.arange(TREND_SLOPE_BARS, dtype=float)
    slope = np.polyfit(x, np.log(window), 1)[0]
    return float(slope)


def _vix_pctile_5y(close: pd.Series, target: pd.Timestamp) -> float:
    end = close.index.get_loc(target)
    start = end - VIX_PCTILE_BARS
    if start < 0:
        raise ValueError(
            f"insufficient history for VIX 5y percentile at {target.date()} "
            f"(need {VIX_PCTILE_BARS + 1} bars, have {end + 1})"
        )
    window = close.iloc[start : end + 1].to_numpy(dtype=float)
    n = window.size
    rank = int((window < window[-1]).sum() + 1)
    return float((rank - 1) / (n - 1))


def _dist_200ma_pct(close: pd.Series, target: pd.Timestamp) -> float:
    end = close.index.get_loc(target)
    start = end - (SMA200_BARS - 1)
    if start < 0:
        raise ValueError(f"insufficient history for 200-MA distance at {target.date()}")
    window = close.iloc[start : end + 1].to_numpy(dtype=float)
    sma = window.mean()
    return float((window[-1] / sma - 1.0) * 100.0)


def _sector_return_dispersion(closes: pd.DataFrame, target: pd.Timestamp) -> float:
    end = closes.index.get_loc(target)
    start = end - SECTOR_RETURN_BARS
    if start < 0:
        raise ValueError(f"insufficient history for sector dispersion at {target.date()}")
    rets = []
    for k in SECTOR_KEYS:
        c_now  = float(closes[k].iloc[end])
        c_then = float(closes[k].iloc[start])
        if c_now <= 0 or c_then <= 0:
            raise ValueError(f"non-positive close for {k} in sector-return window")
        rets.append(np.log(c_now / c_then))
    return float(np.std(rets, ddof=0))


# --- Public API ------------------------------------------------------------
def regime_vector(date) -> pd.Series:
    target = pd.Timestamp(date).normalize()

    spy_dates = _load_spy_calendar()
    if target not in spy_dates:
        raise ValueError(f"target {target.date()} not in SPY trading-day calendar")
    end_pos = spy_dates.get_loc(target)
    if end_pos < VIX_PCTILE_BARS:
        raise ValueError(
            f"target {target.date()} too early for VIX 5y percentile "
            f"(need {VIX_PCTILE_BARS} prior bars, have {end_pos})"
        )

    closes = _load_close_matrix()
    naaim   = _load_naaim_daily()
    breadth = _load_breadth_daily()

    if target not in naaim.index:
        raise ValueError(f"target {target.date()} not in naaim_daily.parquet")
    if target not in breadth.index:
        raise ValueError(f"target {target.date()} not in breadth_daily.parquet")

    vals = {}

    # cols 1-4: pre-computed expression columns from npz
    vals["spy_ext50"]  = _ext_col("SPY", "ext_avgc50_adr14",  target)
    vals["spy_ext200"] = _ext_col("SPY", "ext_avgc200_adr14", target)
    vals["qqq_ext50"]  = _ext_col("QQQ", "ext_avgc50_adr14",  target)
    vals["qqq_ext200"] = _ext_col("QQQ", "ext_avgc200_adr14", target)

    # cols 5-9: 20-bar log-close trend slopes
    vals["spy_trend_slope_20"]  = _trend_slope_20(closes[KEY_SPY], target)
    vals["qqq_trend_slope_20"]  = _trend_slope_20(closes[KEY_QQQ], target)
    vals["dxy_trend_slope_20"]  = _trend_slope_20(closes[KEY_DXY], target)
    vals["gold_trend_slope_20"] = _trend_slope_20(closes[KEY_GLD], target)
    vals["btc_trend_slope_20"]  = _trend_slope_20(closes[KEY_BTC], target)

    # col 10-11: volatility
    vals["vix_pctile_5y"]    = _vix_pctile_5y(closes[KEY_VIX], target)
    vals["vix_vix3m_ratio"]  = float(closes[KEY_VIX].loc[target] / closes[KEY_VX3M].loc[target])

    # col 12: breadth parquet
    vals["pct_above_50ma"] = float(breadth.loc[target, "pct_above_50ma"])

    # col 13-15: sector + cross-asset ratios
    vals["sector_return_dispersion_20"] = _sector_return_dispersion(closes, target)
    vals["xly_xlp_ratio"]               = float(closes[KEY_XLY].loc[target] / closes[KEY_XLP].loc[target])
    vals["hyg_ief_ratio"]               = float(closes[KEY_HYG].loc[target] / closes[KEY_IEF].loc[target])

    # col 16: 2s10s yield spread (FRED — value IS the spread in pct points)
    vals["yield_2s10s_spread"] = float(closes[KEY_T2T10].loc[target])

    # cols 17-19: macro % distance from 200-MA
    vals["dxy_dist_200ma_pct"]  = _dist_200ma_pct(closes[KEY_DXY], target)
    vals["gold_dist_200ma_pct"] = _dist_200ma_pct(closes[KEY_GLD], target)
    vals["btc_dist_200ma_pct"]  = _dist_200ma_pct(closes[KEY_BTC], target)

    # cols 20-22: stockbee breadth from parquet
    vals["stockbee_4pct_ratio_10d"]       = float(breadth.loc[target, "stockbee_4pct_ratio_10d"])
    vals["stockbee_25pct_up_count_63d"]   = float(breadth.loc[target, "stockbee_25pct_up_count_63d"])
    vals["stockbee_25pct_down_count_63d"] = float(breadth.loc[target, "stockbee_25pct_down_count_63d"])

    # col 23: RSP / SPY
    vals["rsp_spy_ratio"] = float(closes[KEY_RSP].loc[target] / closes[KEY_SPY].loc[target])

    # col 24: NAAIM
    vals["naaim_mean"] = float(naaim.loc[target, "naaim_mean"])

    # Assemble in fixed order
    out = pd.Series(
        [vals[c] for c in OUTPUT_COLUMNS],
        index=OUTPUT_COLUMNS,
        dtype="float64",
        name=target,
    )

    # Final validation: no NaN / inf
    bad = [c for c in OUTPUT_COLUMNS if not np.isfinite(out[c])]
    if bad:
        raise ValueError(f"non-finite values in columns: {bad}")

    return out


# --- CLI for smoke-test ----------------------------------------------------
def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Build a regime vector for a date.")
    p.add_argument("date", nargs="?", default=None,
                   help="YYYY-MM-DD. Default: latest SPY trading day.")
    args = p.parse_args()

    if args.date is None:
        target = _load_spy_calendar()[-1]
    else:
        target = pd.Timestamp(args.date).normalize()

    print(f"  regime_vector({target.date()}):")
    vec = regime_vector(target)
    for name, val in vec.items():
        print(f"    {name:<35s} {val: .6f}")


if __name__ == "__main__":
    _cli()
