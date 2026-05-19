"""
Extended OHLCV ingestion (worktree-local).

Fetches every regime-vector input instrument from EODHD back to its
earliest available date (no HISTORY_START cap), and writes a single
worktree-local pickle:

    regime_meter/cache/market_ohlcv_extended.pkl

Keys mirror the main-repo market_ohlcv.pkl naming (the .US suffix is
stripped from equity ETFs so 'SPY' / 'XLRE' / 'XLC' / etc. line up
1:1 with regime_vector.py's KEY_* constants). T10Y2Y_CALC is computed
inline at fetch time (US10Y - US2Y), mirroring main repo's
market_cache_builder.py.

VIX is NOT included here -- it is handled separately by
regime_meter/vix_ingest.py so the 5y-percentile parquet path stays
independent.

NAAIM is NOT included here -- it has its own xlsx-scraped ingestion
path in regime_meter/naaim_ingest.py.

Writes only inside the worktree. Reads no main-repo state.
"""
import argparse
import json
import os
import pickle
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
OUTPUT_PKL    = os.path.join(CACHE_DIR, "market_ohlcv_extended.pkl")

# --- EODHD -----------------------------------------------------------------
EODHD_BASE     = "https://eodhd.com/api"
HTTP_HEADERS   = {"User-Agent": "ScanPerfect-RegimeMeter/1.0"}
HTTP_TIMEOUT_S = 30
FETCH_FROM     = "1985-01-01"   # EODHD returns what it has; no harm in deep floor.

# --- Instrument registry ---------------------------------------------------
# Each entry is (eodhd_symbol, pickle_key). The pickle_key drops the
# .US suffix so equity ETFs match main-repo market_ohlcv.pkl naming.
INSTRUMENTS = [
    # Broad market
    ("SPY.US",       "SPY"),
    ("QQQ.US",       "QQQ"),
    ("RSP.US",       "RSP"),
    # Safe-haven + credit + rates
    ("GLD.US",       "GLD"),
    ("HYG.US",       "HYG"),
    ("IEF.US",       "IEF"),
    # SPDR sectors
    ("XLY.US",       "XLY"),
    ("XLP.US",       "XLP"),
    ("XLE.US",       "XLE"),
    ("XLB.US",       "XLB"),
    ("XLI.US",       "XLI"),
    ("XLV.US",       "XLV"),
    ("XLF.US",       "XLF"),
    ("XLK.US",       "XLK"),
    ("XLU.US",       "XLU"),
    ("XLRE.US",      "XLRE"),
    ("XLC.US",       "XLC"),
    # Macro / cross-asset
    ("DXY.INDX",     "DXY.INDX"),
    ("BTC-USD.CC",   "BTC-USD.CC"),
    # Volatility term structure (long only; VIX itself lives in vix_ingest.py)
    ("VIX3M.INDX",   "VIX3M.INDX"),
    # Yield components for T10Y2Y_CALC
    ("US10Y.INDX",   "US10Y.INDX"),
    ("US2Y.INDX",    "US2Y.INDX"),
]

# Derived instruments computed from raw components after fetch.
DERIVED_KEY_T10Y2Y = "T10Y2Y_CALC"


# --- Guards ----------------------------------------------------------------
def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def _load_eodhd_token():
    tok = os.environ.get("EODHD_API_TOKEN", "")
    if not tok:
        sys.exit("ABORT: EODHD_API_TOKEN not set in environment")
    return tok


# --- Normalization ---------------------------------------------------------
def _standard_df(df, min_bars=50):
    """Coerce to standard schema. No HISTORY_START trim.

    Returns DataFrame with columns [date, open, high, low, close, volume],
    sorted ascending by date, NaN-close rows dropped, close > 0.
    Returns None if fewer than min_bars valid rows.
    """
    if df is None or len(df) == 0:
        return None
    required = ["date", "open", "high", "low", "close"]
    for c in required:
        if c not in df.columns:
            return None
    if "volume" not in df.columns:
        df = df.assign(volume=0.0)
    df = df[required + ["volume"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return df if len(df) >= min_bars else None


# --- Fetch -----------------------------------------------------------------
def _fetch_eodhd(eodhd_symbol, token):
    """Fetch raw EOD JSON from EODHD for one instrument.

    Returns standardized DataFrame, or None on failure / empty / short.
    Mirrors market_cache_builder.py policy: raw `close` (no adjusted_close
    ratio), no User-Agent retry, single try/except.
    """
    end_date = pd.Timestamp.today().strftime("%Y-%m-%d")
    url = (
        f"{EODHD_BASE}/eod/{eodhd_symbol}"
        f"?from={FETCH_FROM}&to={end_date}"
        f"&period=d"
        f"&api_token={token}&fmt=json"
    )
    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
        if not raw.startswith("["):
            return None
        data = json.loads(raw)
        if not data:
            return None
        df = pd.DataFrame(data)
        return _standard_df(df)
    except (urllib.error.URLError, ValueError, KeyError):
        return None


def _fetch_one(item, token):
    eodhd_symbol, pickle_key = item
    df = _fetch_eodhd(eodhd_symbol, token)
    return eodhd_symbol, pickle_key, df


# --- Derived instrument ----------------------------------------------------
def _compute_t10y2y(results):
    """Compute T10Y2Y_CALC = US10Y close minus US2Y close on the date
    intersection.

    Mirrors main-repo market_cache_builder.py logic for the same key.
    Returns the derived DataFrame, or None if either component is missing.
    """
    us10y = results.get("US10Y.INDX")
    us2y  = results.get("US2Y.INDX")
    if us10y is None or us2y is None:
        return None
    merged = pd.merge(
        us10y[["date", "close"]].rename(columns={"close": "y10"}),
        us2y[["date",  "close"]].rename(columns={"close": "y2"}),
        on="date", how="inner",
    ).sort_values("date").reset_index(drop=True)
    if len(merged) < 50:
        return None
    spread = merged["y10"] - merged["y2"]
    return pd.DataFrame({
        "date":   merged["date"],
        "open":   spread,
        "high":   spread,
        "low":    spread,
        "close":  spread,
        "volume": 0.0,
    })


# --- Reporting -------------------------------------------------------------
def _print_summary(results):
    print("\n  Per-instrument summary:")
    print(f"    {'KEY':<14s} {'FIRST':<12s} {'LAST':<12s} {'ROWS':>8s}")
    for key in sorted(results.keys()):
        df = results[key]
        first = df["date"].iloc[0].date()
        last  = df["date"].iloc[-1].date()
        rows  = len(df)
        print(f"    {key:<14s} {str(first):<12s} {str(last):<12s} {rows:>8d}")


# --- Main ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Extended OHLCV ingestion (worktree-local)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch even if pickle exists",
    )
    parser.add_argument(
        "--threads", type=int, default=16,
        help="Fetch thread count (default 16)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  EXTENDED OHLCV INGESTION (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(OUTPUT_PKL)
    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Output:        {OUTPUT_PKL}")
    print(f"  Instruments:   {len(INSTRUMENTS)} (+1 derived: {DERIVED_KEY_T10Y2Y})")
    print(f"  Fetch from:    {FETCH_FROM}")

    if os.path.exists(OUTPUT_PKL) and not args.force:
        print(f"\n  Pickle already exists. Use --force to re-fetch.")
        with open(OUTPUT_PKL, "rb") as f:
            existing = pickle.load(f)
        print(f"  {len(existing)} instruments currently cached.")
        _print_summary(existing)
        return

    token = _load_eodhd_token()
    print(f"\n  Fetching {len(INSTRUMENTS)} instruments "
          f"({args.threads} threads)...")
    t0       = time.time()
    results  = {}
    failures = []

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = {pool.submit(_fetch_one, item, token): item
                   for item in INSTRUMENTS}
        for future in as_completed(futures):
            eodhd_symbol, pickle_key, df = future.result()
            if df is not None:
                results[pickle_key] = df
                first = df["date"].iloc[0].date()
                last  = df["date"].iloc[-1].date()
                print(f"    OK   {eodhd_symbol:<14s} -> {pickle_key:<14s} "
                      f"{len(df):>6d} bars  ({first} -> {last})")
            else:
                failures.append((eodhd_symbol, pickle_key))
                print(f"    FAIL {eodhd_symbol:<14s} -> {pickle_key}")

    fetch_elapsed = time.time() - t0
    print(f"\n  Fetch: {len(results)} OK, {len(failures)} failed  "
          f"[{fetch_elapsed:.1f}s]")

    if failures:
        print("  Failed instruments:")
        for eodhd_symbol, pickle_key in failures:
            print(f"    {eodhd_symbol}  ({pickle_key})")
        sys.exit("ABORT: at least one instrument failed; not writing pickle.")

    print("\n  Computing derived instruments...")
    t10y2y = _compute_t10y2y(results)
    if t10y2y is None:
        sys.exit(f"ABORT: could not compute {DERIVED_KEY_T10Y2Y} "
                 "(US10Y or US2Y missing / too short)")
    results[DERIVED_KEY_T10Y2Y] = t10y2y
    print(f"    OK   {DERIVED_KEY_T10Y2Y:<14s}             "
          f"{len(t10y2y):>6d} bars  "
          f"({t10y2y['date'].iloc[0].date()} -> "
          f"{t10y2y['date'].iloc[-1].date()})")

    _assert_inside_worktree(OUTPUT_PKL)
    with open(OUTPUT_PKL, "wb") as f:
        pickle.dump(results, f, protocol=4)
    size_mb = os.path.getsize(OUTPUT_PKL) / 1024**2
    print(f"\n  Wrote {OUTPUT_PKL}")
    print(f"  {size_mb:.2f} MB  ({len(results)} instruments)")

    _print_summary(results)

    total = time.time() - t0
    print(f"\n  DONE in {total:.1f}s.")


if __name__ == "__main__":
    main()
