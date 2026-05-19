"""
regime_vector(date) -> pd.Series of 24 named floats.

Assembles the regime-similarity input vector for a given trading date
from four worktree-local read-only sources:

  - regime_meter/cache/market_ohlcv_extended.pkl   (all raw closes, 1985+)
  - regime_meter/cache/market_series/*.npz         (ext columns only, 1993+)
  - regime_meter/cache/vix_daily.parquet           (VIX 1990+)
  - regime_meter/cache/naaim_daily.parquet         (NAAIM 2006+)
  - regime_meter/cache/breadth_daily.parquet       (breadth 2016+)

NaN-tolerant: any column whose inputs aren't available at the target
date returns NaN for that column instead of raising. The only raise is
when `target` is not in SPY's trading-day calendar at all (i.e. the
requested date is not a US equity trading day).

Reads only inside the worktree. Writes nothing. The main repo's own
market_ohlcv.pkl and market_series/ are intentionally NOT used.
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

NAAIM_PARQUET     = os.path.join(CACHE_DIR, "naaim_daily.parquet")
BREADTH_PARQUET   = os.path.join(CACHE_DIR, "breadth_daily.parquet")
VIX_PARQUET       = os.path.join(CACHE_DIR, "vix_daily.parquet")
MARKET_OHLCV_PKL  = os.path.join(CACHE_DIR, "market_ohlcv_extended.pkl")
MARKET_SERIES_DIR = os.path.join(CACHE_DIR, "market_series")

# Main repo import root (for live generate_all() only -- no data access).
MAIN_REPO_ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
if MAIN_REPO_ROOT not in sys.path:
    sys.path.insert(0, MAIN_REPO_ROOT)

# --- Window / lookbacks ----------------------------------------------------
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

# Chain-link inception dates: a child ETF carved out of a parent ETF later
# than the parent's own inception. Before the child's inception, the
# close_matrix's child column is the parent column scaled by
# ratio = child[inception] / parent[inception], so 20-bar log returns
# remain continuous across the splice.
#   XLC  was carved out of XLK on 2018-06-19.
#   XLRE was carved out of XLF on 2015-10-08.
XLC_INCEPTION  = pd.Timestamp("2018-06-19")
XLRE_INCEPTION = pd.Timestamp("2015-10-08")

# Instruments we need a close-series-on-SPY-calendar for. VIX is NOT here:
# it comes from regime_meter/cache/vix_daily.parquet (1990+, via
# regime_meter/vix_ingest.py) because the 5y trailing percentile needs
# more history than market_ohlcv_extended.pkl's SPY-calendar reindex provides.
ALL_OHLCV_KEYS = sorted(set([
    KEY_SPY, KEY_QQQ, KEY_RSP,
    KEY_DXY, KEY_GLD, KEY_BTC,
    KEY_VX3M,
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
        raise ValueError(f"{KEY_SPY!r} not in market_ohlcv_extended.pkl")
    dates = pd.to_datetime(cache[KEY_SPY]["date"]).sort_values().reset_index(drop=True)
    return pd.DatetimeIndex(dates)


@lru_cache(maxsize=1)
def _load_market_ohlcv() -> dict:
    if not os.path.exists(MARKET_OHLCV_PKL):
        raise ValueError(
            f"market_ohlcv_extended.pkl missing at {MARKET_OHLCV_PKL} "
            "(run regime_meter/extended_ohlcv_ingest.py)"
        )
    with open(MARKET_OHLCV_PKL, "rb") as f:
        return pickle.load(f)


def _chain_link(child: pd.Series, parent: pd.Series,
                inception_date: pd.Timestamp,
                child_name: str, parent_name: str) -> pd.Series:
    """Chain-link `child` backward onto `parent` before `inception_date`.

    Pre-inception dates in the aligned child series are NaN. To make 20-bar
    log returns straddling the splice continuous, scale pre-inception parent
    closes by ratio = child[inception] / parent[inception] and splice them
    into the child series.
    """
    spy_dates = child.index
    inception_pos = spy_dates.get_indexer([inception_date], method="bfill")[0]
    if inception_pos < 0:
        raise ValueError(
            f"{child_name} inception {inception_date.date()} past SPY calendar end"
        )
    splice_date = spy_dates[inception_pos]

    child_at_inception  = child.iloc[inception_pos]
    parent_at_inception = parent.iloc[inception_pos]
    if (pd.isna(child_at_inception) or pd.isna(parent_at_inception)
            or parent_at_inception <= 0):
        raise ValueError(
            f"{child_name}<-{parent_name} chain-link splice failed at "
            f"{splice_date.date()}: child={child_at_inception}, "
            f"parent={parent_at_inception}"
        )
    ratio = float(child_at_inception) / float(parent_at_inception)

    chained = child.copy()
    pre_mask = chained.index < splice_date
    chained.loc[pre_mask] = parent.loc[pre_mask] * ratio
    return chained


@lru_cache(maxsize=1)
def _load_close_matrix() -> pd.DataFrame:
    """All needed instruments' closes reindexed to SPY calendar, forward-filled.

    Columns = ALL_OHLCV_KEYS. Index = SPY trading-day DatetimeIndex.
    FRED/BTC series get forward-filled to cover non-trading-day gaps; the
    target-date row is always a SPY trading day so the value is well-defined.

    XLC column is chain-linked to XLK before 2018-06-19 and XLRE column
    is chain-linked to XLF before 2015-10-08 (see _chain_link).
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

    # Chain-link carve-out children to their parent ETF for pre-inception
    # dates. No-op for date ranges where the child already has native data.
    for child, parent, inception in [
        ("XLC",  "XLK", XLC_INCEPTION),
        ("XLRE", "XLF", XLRE_INCEPTION),
    ]:
        if child not in cols or parent not in cols:
            raise ValueError(
                f"{child} chain-link requires both {child} and {parent} "
                "in close_matrix"
            )
        cols[child] = _chain_link(cols[child], cols[parent], inception,
                                  child, parent)

    return pd.DataFrame(cols, index=spy_dates)


@lru_cache(maxsize=1)
def _load_vix_extended() -> pd.Series:
    """VIX close series indexed by its native (extended) date range, 1990+.

    Source: regime_meter/cache/vix_daily.parquet, built by
    regime_meter/vix_ingest.py. NOT reindexed to SPY's calendar — the
    5y trailing percentile needs every VIX bar including pre-SPY history.
    """
    if not os.path.exists(VIX_PARQUET):
        raise ValueError(
            f"vix parquet missing at {VIX_PARQUET} "
            "(run regime_meter/vix_ingest.py)"
        )
    df = pd.read_parquet(VIX_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].sort_index()


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
# Each computer returns NaN gracefully when its inputs aren't available at
# the target date (insufficient history, missing date, non-positive close).
# Only structural bugs (e.g. expression rename) still raise.

NAN = float("nan")


def _ext_col(ticker: str, expr_name: str, target: pd.Timestamp) -> float:
    data, dates = _load_npz(ticker)
    names = _load_expression_name_index()
    if expr_name not in names:
        raise ValueError(f"expression {expr_name!r} not in generate_all()")
    col_idx = names.index(expr_name)

    pos = dates.get_indexer([target])[0]
    if pos < 0:
        return NAN
    val = data[pos, col_idx]
    return float(val) if np.isfinite(val) else NAN


def _trend_slope_20(close: pd.Series, target: pd.Timestamp) -> float:
    if target not in close.index:
        return NAN
    end = close.index.get_loc(target)
    start = end - (TREND_SLOPE_BARS - 1)
    if start < 0:
        return NAN
    window = close.iloc[start : end + 1].to_numpy(dtype=float)
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        return NAN
    x = np.arange(TREND_SLOPE_BARS, dtype=float)
    slope = np.polyfit(x, np.log(window), 1)[0]
    return float(slope)


def _vix_pctile_5y(vix_extended: pd.Series, target: pd.Timestamp) -> float:
    if target not in vix_extended.index:
        return NAN
    end = vix_extended.index.get_loc(target)
    start = end - VIX_PCTILE_BARS
    if start < 0:
        return NAN
    window = vix_extended.iloc[start : end + 1].to_numpy(dtype=float)
    if not np.all(np.isfinite(window)):
        return NAN
    n = window.size
    rank = int((window < window[-1]).sum() + 1)
    return float((rank - 1) / (n - 1))


def _dist_200ma_pct(close: pd.Series, target: pd.Timestamp) -> float:
    if target not in close.index:
        return NAN
    end = close.index.get_loc(target)
    start = end - (SMA200_BARS - 1)
    if start < 0:
        return NAN
    window = close.iloc[start : end + 1].to_numpy(dtype=float)
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        return NAN
    sma = window.mean()
    return float((window[-1] / sma - 1.0) * 100.0)


def _sector_return_dispersion(closes: pd.DataFrame, target: pd.Timestamp) -> float:
    if target not in closes.index:
        return NAN
    end = closes.index.get_loc(target)
    start = end - SECTOR_RETURN_BARS
    if start < 0:
        return NAN
    rets = []
    for k in SECTOR_KEYS:
        c_now  = closes[k].iloc[end]
        c_then = closes[k].iloc[start]
        if pd.isna(c_now) or pd.isna(c_then) or c_now <= 0 or c_then <= 0:
            return NAN
        rets.append(np.log(float(c_now) / float(c_then)))
    return float(np.std(rets, ddof=0))


def _ratio(num_series: pd.Series, den_series: pd.Series,
           target: pd.Timestamp) -> float:
    """Two-instrument close ratio at target. NaN if either side is missing
    or denominator is non-positive."""
    if target not in num_series.index or target not in den_series.index:
        return NAN
    num = num_series.loc[target]
    den = den_series.loc[target]
    if pd.isna(num) or pd.isna(den) or den <= 0:
        return NAN
    return float(num / den)


def _at(series: pd.Series, target: pd.Timestamp) -> float:
    """Raw scalar value at target. NaN if missing."""
    if target not in series.index:
        return NAN
    v = series.loc[target]
    return float(v) if pd.notna(v) else NAN


def _from_breadth(breadth: pd.DataFrame, target: pd.Timestamp, col: str) -> float:
    """Lookup one column of the breadth parquet at target. NaN if missing."""
    if target not in breadth.index:
        return NAN
    v = breadth.loc[target, col]
    return float(v) if pd.notna(v) else NAN


# --- Public API ------------------------------------------------------------
def regime_vector(date) -> pd.Series:
    """Build the 24-column regime vector for a target date.

    NaN-tolerant: columns whose inputs aren't available at the target date
    return NaN. The only raise is when `target` isn't in SPY's trading-day
    calendar at all. Downstream code uses the per-column NaN pattern (and
    Phase D's MSD-over-intersection distance) to handle partial vectors.
    """
    target = pd.Timestamp(date).normalize()

    spy_dates = _load_spy_calendar()
    if target not in spy_dates:
        raise ValueError(f"target {target.date()} not in SPY trading-day calendar")

    closes  = _load_close_matrix()
    naaim   = _load_naaim_daily()
    breadth = _load_breadth_daily()
    vix_ext = _load_vix_extended()

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

    # cols 10-11: volatility (VIX from extended parquet, VIX3M from extended pickle)
    vals["vix_pctile_5y"]   = _vix_pctile_5y(vix_ext, target)
    vals["vix_vix3m_ratio"] = _ratio(vix_ext, closes[KEY_VX3M], target)

    # col 12: pct above 50-MA from breadth parquet
    vals["pct_above_50ma"] = _from_breadth(breadth, target, "pct_above_50ma")

    # cols 13-15: sector dispersion + cross-asset ratios
    vals["sector_return_dispersion_20"] = _sector_return_dispersion(closes, target)
    vals["xly_xlp_ratio"]               = _ratio(closes[KEY_XLY], closes[KEY_XLP], target)
    vals["hyg_ief_ratio"]               = _ratio(closes[KEY_HYG], closes[KEY_IEF], target)

    # col 16: 2s10s yield spread (FRED — value IS the spread in pct points)
    vals["yield_2s10s_spread"] = _at(closes[KEY_T2T10], target)

    # cols 17-19: macro % distance from 200-MA
    vals["dxy_dist_200ma_pct"]  = _dist_200ma_pct(closes[KEY_DXY], target)
    vals["gold_dist_200ma_pct"] = _dist_200ma_pct(closes[KEY_GLD], target)
    vals["btc_dist_200ma_pct"]  = _dist_200ma_pct(closes[KEY_BTC], target)

    # cols 20-22: stockbee breadth from parquet
    vals["stockbee_4pct_ratio_10d"]       = _from_breadth(breadth, target, "stockbee_4pct_ratio_10d")
    vals["stockbee_25pct_up_count_63d"]   = _from_breadth(breadth, target, "stockbee_25pct_up_count_63d")
    vals["stockbee_25pct_down_count_63d"] = _from_breadth(breadth, target, "stockbee_25pct_down_count_63d")

    # col 23: RSP / SPY
    vals["rsp_spy_ratio"] = _ratio(closes[KEY_RSP], closes[KEY_SPY], target)

    # col 24: NAAIM
    if target in naaim.index:
        v = naaim.loc[target, "naaim_mean"]
        vals["naaim_mean"] = float(v) if pd.notna(v) else NAN
    else:
        vals["naaim_mean"] = NAN

    return pd.Series(
        [vals[c] for c in OUTPUT_COLUMNS],
        index=OUTPUT_COLUMNS,
        dtype="float64",
        name=target,
    )


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
