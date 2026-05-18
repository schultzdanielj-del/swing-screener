"""
Extended VIX history ingestion (worktree-local).

Fetches VIX.INDX from EODHD back to 2011-01-01 so that the 5-year (1259-bar)
VIX percentile is computable on every regime-vector date >= 2016-01-04.
The main-repo market_ohlcv.pkl only goes back to 2016 and is read-only from
this worktree, hence this side-channel cache.

Default: re-fetch from network. --no-fetch: re-derive from cached raw JSON.

Writes only inside the worktree. Reads main-repo SPY calendar read-only for
sanity checks; never modifies the main repo.
"""
import argparse
import json
import os
import pickle
import sys
import urllib.error
import urllib.request

import pandas as pd


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
RAW_JSON_PATH = os.path.join(CACHE_DIR, "vix_raw.json")
DAILY_PARQUET = os.path.join(CACHE_DIR, "vix_daily.parquet")

MAIN_REPO_CACHE = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
SPY_PICKLE_PATH = os.path.join(MAIN_REPO_CACHE, "market_ohlcv.pkl")

# --- EODHD -----------------------------------------------------------------
EODHD_BASE      = "https://eodhd.com/api"
EODHD_SYMBOL    = "VIX.INDX"
FETCH_START     = "2011-01-01"
HTTP_HEADERS    = {"User-Agent": "ScanPerfect-RegimeMeter/1.0"}
HTTP_TIMEOUT_S  = 30

# --- Sanity bounds ---------------------------------------------------------
EARLIEST_REQUIRED   = "2011-01-04"   # first parsed bar must be on or before
RECENT_STALENESS_DAYS = 5            # last bar must be within this many calendar days of today


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


def fetch_vix_json(token):
    end_date = pd.Timestamp.today().strftime("%Y-%m-%d")
    url = (
        f"{EODHD_BASE}/eod/{EODHD_SYMBOL}"
        f"?from={FETCH_START}&to={end_date}"
        f"&period=d"
        f"&api_token={token}&fmt=json"
    )
    print(f"  Fetching {EODHD_SYMBOL} {FETCH_START} -> {end_date} from EODHD ...")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        sys.exit(f"ABORT: EODHD fetch failed: {e}")
    if not raw.startswith("["):
        sys.exit(f"ABORT: EODHD response not a JSON array. First 500 chars:\n{raw[:500]}")
    data = json.loads(raw)
    if not data:
        sys.exit("ABORT: EODHD returned empty array")
    return data


def write_raw_json(data, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"  Writing raw JSON -> {out_path}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written ({len(data)} bars)")


def parse_json(raw_path):
    print(f"  Parsing {raw_path} ...")
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    df["date"]  = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[["date", "close"]].dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
    print(
        f"  Parsed: {len(df)} rows, "
        f"{df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}"
    )
    return df


def load_spy_calendar():
    if not os.path.exists(SPY_PICKLE_PATH):
        sys.exit(f"ABORT: SPY pickle missing at {SPY_PICKLE_PATH}")
    with open(SPY_PICKLE_PATH, "rb") as f:
        cache = pickle.load(f)
    if "SPY" not in cache:
        sys.exit("ABORT: SPY not in market_ohlcv.pkl")
    return pd.to_datetime(cache["SPY"]["date"]).sort_values().reset_index(drop=True)


def run_sanity_checks(df):
    print("\n  Sanity checks:")

    first = df["date"].iloc[0]
    if first > pd.Timestamp(EARLIEST_REQUIRED):
        sys.exit(
            f"ABORT: first VIX bar {first.date()} > {EARLIEST_REQUIRED}; "
            "EODHD did not return enough history."
        )
    print(f"    First bar:    {first.date()} (<= {EARLIEST_REQUIRED}, OK)")

    last = df["date"].iloc[-1]
    today = pd.Timestamp.today().normalize()
    staleness = (today - last).days
    if staleness > RECENT_STALENESS_DAYS:
        print(
            f"    WARN: last VIX bar {last.date()} is {staleness} days old "
            f"(> {RECENT_STALENESS_DAYS}); EODHD may be lagging."
        )
    else:
        print(f"    Last bar:     {last.date()} ({staleness} days old)")

    n_nan = int(df["close"].isna().sum())
    if n_nan != 0:
        sys.exit(f"ABORT: {n_nan} NaN close values in parsed series")
    print(f"    NaN closes:   0")
    print(f"    Row count:    {len(df)}")

    # Confirm we have 1259 bars before 2016-01-04 (the first SPY trading day)
    spy_dates = load_spy_calendar()
    first_spy = pd.Timestamp(spy_dates.iloc[0])
    before_count = int((df["date"] < first_spy).sum())
    if before_count < 1259:
        sys.exit(
            f"ABORT: only {before_count} VIX bars before SPY's first trading day "
            f"({first_spy.date()}); need >= 1259 for 5y percentile on that date."
        )
    print(f"    Bars before first SPY day ({first_spy.date()}): {before_count} (>= 1259, OK)")


def write_parquet(df, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"\n  Writing -> {out_path}")
    df.to_parquet(out_path, index=False)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written")


def main():
    parser = argparse.ArgumentParser(
        description="Extended VIX history ingestion (worktree-local)"
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip network fetch; re-parse from cached raw JSON if present"
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  VIX INGESTION (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(RAW_JSON_PATH)
    _assert_inside_worktree(DAILY_PARQUET)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Raw JSON:      {RAW_JSON_PATH}")
    print(f"  Output:        {DAILY_PARQUET}")
    print(f"  Main repo:     {MAIN_REPO_CACHE} (SPY calendar, read-only)")

    if args.no_fetch:
        if not os.path.exists(RAW_JSON_PATH):
            sys.exit(f"ABORT: --no-fetch but no cached raw JSON at {RAW_JSON_PATH}")
        print(f"\n  --no-fetch: using cached raw JSON")
    else:
        print()
        token = _load_eodhd_token()
        data = fetch_vix_json(token)
        write_raw_json(data, RAW_JSON_PATH)

    print()
    df = parse_json(RAW_JSON_PATH)
    run_sanity_checks(df)
    write_parquet(df, DAILY_PARQUET)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
