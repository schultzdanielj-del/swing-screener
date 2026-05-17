"""
NAAIM Exposure Index — worktree-local ingestion.

Per-run flow:
  1. Discover this week's xlsx URL by scraping naaim.org's NAAIM Exposure Index page.
  2. Download xlsx -> save raw to regime_meter/cache/naaim_raw.xlsx.
  3. Parse xlsx, keep Date + Mean/Average columns only.
  4. Forward-fill to SPY's trading-day calendar from the main repo (read-only).
  5. Write daily series to regime_meter/cache/naaim_daily.parquet.
  6. Print sanity checks.

Default: re-fetch from network. --no-fetch: re-derive from cached raw xlsx.

All writes stay inside the regime-meter worktree. The main repo's
market_ohlcv.pkl is opened read-only for the SPY trading-day calendar.
"""
import argparse
import os
import pickle
import re
import sys
import urllib.error
import urllib.request

import pandas as pd


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
RAW_XLSX_PATH = os.path.join(CACHE_DIR, "naaim_raw.xlsx")
DAILY_PARQUET = os.path.join(CACHE_DIR, "naaim_daily.parquet")

MAIN_REPO_CACHE = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
SPY_PICKLE_PATH = os.path.join(MAIN_REPO_CACHE, "market_ohlcv.pkl")

# --- Network ---------------------------------------------------------------
NAAIM_PAGE_URL = "https://www.naaim.org/programs/naaim-exposure-index/"
XLSX_LINK_REGEX = re.compile(
    r'https?://[^\s"\']+USE_Data-since-Inception_(\d{4}-\d{2}-\d{2})\.xlsx',
    re.IGNORECASE,
)
HTTP_HEADERS   = {"User-Agent": "ScanPerfect-RegimeMeter/1.0"}
HTTP_TIMEOUT_S = 30

# --- Schema ----------------------------------------------------------------
DATE_HEADER_ALIASES = ["date"]
MEAN_HEADER_ALIASES = [
    "mean/average",
    "naaim number mean",
    "mean",
    "average",
    "number",
]

# --- Window / sanity-check bounds -----------------------------------------
HISTORY_START   = "2016-01-01"
EARLIEST_NAAIM  = "2006-08-01"   # first raw reading must be on or before this
GAP_WARN_DAYS   = 14


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def discover_latest_xlsx_url():
    """Scrape the NAAIM page; return URL of the xlsx with the latest date stamp."""
    print(f"  Discovering latest xlsx URL via {NAAIM_PAGE_URL} ...")
    req = urllib.request.Request(NAAIM_PAGE_URL, headers=HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        sys.exit(f"ABORT: failed to fetch NAAIM page: {e}")

    matches = list(XLSX_LINK_REGEX.finditer(html))
    if not matches:
        snippet = html[:2000]
        sys.exit(
            "ABORT: no xlsx link matching 'USE_Data-since-Inception_YYYY-MM-DD.xlsx' "
            "found on page. Pattern may have changed.\n"
            "Page snippet (first 2KB):\n" + snippet
        )

    latest = max(matches, key=lambda m: m.group(1))
    url = latest.group(0)
    print(f"  -> {url}")
    return url


def download_xlsx(url, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"  Downloading -> {out_path} ...")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        sys.exit(f"ABORT: failed to download xlsx: {e}")
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"  {len(data)} bytes written")


def _find_header_row(raw):
    """Scan the first 15 rows for one containing 'Date'. Return its row index."""
    for i in range(min(15, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if any(c in DATE_HEADER_ALIASES for c in cells):
            return i
    sys.exit(
        "ABORT: could not find header row containing 'Date' in first 15 rows."
    )


def _find_value_column(columns):
    """Pick the first column header that matches one of MEAN_HEADER_ALIASES."""
    norm = {str(c).strip().lower(): c for c in columns}
    for alias in MEAN_HEADER_ALIASES:
        if alias in norm:
            return norm[alias]
    sys.exit(
        f"ABORT: no header matched expected aliases {MEAN_HEADER_ALIASES}.\n"
        f"Actual headers: {list(columns)}"
    )


def parse_xlsx(path):
    print(f"  Parsing {path} ...")
    raw = pd.read_excel(path, engine="openpyxl", header=None)
    header_row = _find_header_row(raw)
    print(f"  Header row detected at xlsx index {header_row}")
    df = pd.read_excel(path, engine="openpyxl", header=header_row)

    norm_cols = {str(c).strip().lower(): c for c in df.columns}
    date_col = next((norm_cols[a] for a in DATE_HEADER_ALIASES if a in norm_cols), None)
    if date_col is None:
        sys.exit(f"ABORT: no Date column found. Headers: {list(df.columns)}")

    value_col = _find_value_column(df.columns)
    print(f"  Date column: {date_col!r}, value column: {value_col!r}")

    out = pd.DataFrame({
        "date":       pd.to_datetime(df[date_col], errors="coerce"),
        "naaim_mean": pd.to_numeric(df[value_col], errors="coerce"),
    })
    out = out.dropna(subset=["date", "naaim_mean"])
    out = out.sort_values("date").reset_index(drop=True)
    print(
        f"  Parsed: {len(out)} rows, "
        f"{out['date'].iloc[0].date()} -> {out['date'].iloc[-1].date()}"
    )
    return out


def load_spy_calendar():
    if not os.path.exists(SPY_PICKLE_PATH):
        sys.exit(f"ABORT: SPY pickle missing at {SPY_PICKLE_PATH}")
    with open(SPY_PICKLE_PATH, "rb") as f:
        cache = pickle.load(f)
    if "SPY" not in cache:
        sys.exit("ABORT: SPY not in market_ohlcv.pkl")
    dates = pd.to_datetime(cache["SPY"]["date"]).sort_values().reset_index(drop=True)
    dates = dates[dates >= pd.Timestamp(HISTORY_START)].reset_index(drop=True)
    print(
        f"  SPY calendar: {len(dates)} trading days "
        f"({dates.iloc[0].date()} -> {dates.iloc[-1].date()})"
    )
    return dates


def forward_fill_to_daily(weekly, spy_dates):
    indexed = weekly.set_index("date")["naaim_mean"].sort_index()

    n_dupe = int(indexed.index.duplicated().sum())
    if n_dupe > 0:
        dupe_dates = indexed.index[indexed.index.duplicated(keep=False)].unique()
        print(
            f"  WARN: {n_dupe} duplicate NAAIM date row(s); "
            f"keeping last reading per date ({len(dupe_dates)} distinct date(s))"
        )
        for d in list(dupe_dates)[:10]:
            print(f"    duplicate: {pd.Timestamp(d).date()}")
        indexed = indexed[~indexed.index.duplicated(keep="last")]

    spy_index = pd.DatetimeIndex(spy_dates)
    if spy_index.duplicated().any():
        sys.exit("ABORT: SPY calendar has duplicate dates")

    daily = indexed.reindex(spy_index, method="ffill")
    return pd.DataFrame({"date": spy_index, "naaim_mean": daily.values})


def run_sanity_checks(weekly, daily, spy_dates):
    print("\n  Sanity checks:")

    first_raw = weekly["date"].iloc[0]
    if first_raw > pd.Timestamp(EARLIEST_NAAIM):
        sys.exit(
            f"ABORT: first raw NAAIM date {first_raw.date()} > {EARLIEST_NAAIM}; "
            "spec requires 2006+ history."
        )
    print(f"    Raw first date:    {first_raw.date()} (<= {EARLIEST_NAAIM}, OK)")

    print(f"    Raw weekly rows:   {len(weekly)}")

    if len(daily) != len(spy_dates):
        sys.exit(
            f"ABORT: daily rows {len(daily)} != SPY trading days {len(spy_dates)}"
        )
    print(f"    Daily rows:        {len(daily)} (matches SPY calendar)")

    n_nan = int(daily["naaim_mean"].isna().sum())
    if n_nan != 0:
        sys.exit(f"ABORT: {n_nan} NaN values in daily series after forward-fill")
    print(f"    NaN in daily:      0")

    gaps_days = weekly["date"].diff().dt.days.dropna()
    if len(gaps_days) > 0:
        max_gap = int(gaps_days.max())
        if max_gap > GAP_WARN_DAYS:
            print(
                f"    WARN: largest inter-reading gap is {max_gap} days "
                f"(> {GAP_WARN_DAYS}; possible NAAIM publication skip "
                "or parse problem)"
            )
        else:
            print(f"    Max inter-reading gap: {max_gap} days")


def write_daily(daily, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"\n  Writing -> {out_path}")
    daily.to_parquet(out_path, index=False)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written")


def main():
    parser = argparse.ArgumentParser(
        description="NAAIM Exposure Index ingestion (worktree-local)"
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip network fetch; re-parse from cached raw xlsx if present"
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  NAAIM INGESTION (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(RAW_XLSX_PATH)
    _assert_inside_worktree(DAILY_PARQUET)
    print(f"  Worktree root:    {WORKTREE_ROOT}")
    print(f"  Cache dir:        {CACHE_DIR}")
    print(f"  Raw xlsx path:    {RAW_XLSX_PATH}")
    print(f"  Daily output:     {DAILY_PARQUET}")
    print(f"  Main repo cache:  {MAIN_REPO_CACHE}")

    if args.no_fetch:
        if not os.path.exists(RAW_XLSX_PATH):
            sys.exit(
                f"ABORT: --no-fetch but no cached raw xlsx at {RAW_XLSX_PATH}"
            )
        print(f"\n  --no-fetch: using cached raw xlsx")
    else:
        print()
        url = discover_latest_xlsx_url()
        download_xlsx(url, RAW_XLSX_PATH)

    print()
    weekly = parse_xlsx(RAW_XLSX_PATH)

    print()
    spy_dates = load_spy_calendar()

    daily = forward_fill_to_daily(weekly, spy_dates)
    run_sanity_checks(weekly, daily, spy_dates)
    write_daily(daily, DAILY_PARQUET)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
