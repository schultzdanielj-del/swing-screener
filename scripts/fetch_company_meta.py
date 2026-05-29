"""
Company Meta Fetcher -- Yahoo Finance Identity Data Cache Builder.

Fetches per-ticker identity data for every name in theme_map.UNIVERSE:
  - longName (full legal name, e.g. "NVIDIA Corporation")
  - longBusinessSummary (multi-sentence business description from Yahoo)

These feed the theme dashboard's identity surface:
  - Mini-chart card line 2 = longName (truncated)
  - Mini-chart hover tooltip = first 1-2 sentences of longBusinessSummary
  - Composite chart narrative annotation (planned)
  - validate_theme_sectors() evidence printout (planned)

Data source: Yahoo Finance quoteSummary API (free, no key needed).
Module: assetProfile only -- skips defaultKeyStatistics since fundamentals
already cover that.

Ticker source: local_runner.theme_map.UNIVERSE (~490 hand-picked tickers),
NOT the full ~11.5k OHLCV cache. Run is bounded.

Resumable -- completed tickers are saved incrementally. Re-run to fill gaps.
Mirrors final cache to Railway via file_mirror.

Usage:
    python scripts/fetch_company_meta.py
    python scripts/fetch_company_meta.py --delay 0.3        # slower, safer
    python scripts/fetch_company_meta.py --max-tickers 25   # test run
    python scripts/fetch_company_meta.py --retry-failed     # retry only failed tickers
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
OUTPUT_FILE = os.path.join(CACHE_DIR, "company_meta.json")

# Yahoo Finance API
YF_BASE = "https://query2.finance.yahoo.com"
YF_MODULES = "assetProfile,quoteType"  # quoteType carries longName/shortName

DEFAULT_DELAY = 0.15  # seconds between requests


# ══════════════════════════════════════════════════════════════
# YAHOO FINANCE AUTH
# ══════════════════════════════════════════════════════════════

def create_yahoo_session():
    """Create an authenticated Yahoo Finance session with cookie + crumb."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]

    # Get auth cookie
    try:
        opener.open("https://fc.yahoo.com")
    except urllib.error.HTTPError:
        pass  # 404 is expected, cookie still gets set

    # Get crumb
    crumb_url = f"{YF_BASE}/v1/test/getcrumb"
    crumb = opener.open(crumb_url).read().decode().strip()
    if not crumb or "error" in crumb.lower():
        raise RuntimeError(f"Failed to get Yahoo Finance crumb: {crumb}")

    return opener, crumb


def fetch_ticker_data(opener, crumb, ticker):
    """Fetch identity data for one ticker. Returns dict or None on failure."""
    url = (
        f"{YF_BASE}/v10/finance/quoteSummary/{ticker}"
        f"?modules={YF_MODULES}&crumb={crumb}"
    )
    try:
        resp = opener.open(url, timeout=10)
        data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "not_found"}
        elif e.code == 429:
            return {"error": "rate_limited"}
        else:
            return {"error": f"http_{e.code}"}
    except Exception as e:
        return {"error": str(e)[:100]}

    result = data.get("quoteSummary", {}).get("result")
    if not result:
        err = data.get("quoteSummary", {}).get("error", {})
        return {"error": err.get("description", "no_result")[:100]}

    ap = result[0].get("assetProfile", {})
    qt = result[0].get("quoteType", {})

    long_name = qt.get("longName") or qt.get("shortName")
    long_summary = ap.get("longBusinessSummary")

    if not long_name and not long_summary:
        return {"error": "no_identity_data"}

    return {
        "longName": long_name or None,
        "longBusinessSummary": long_summary or None,
    }


# ══════════════════════════════════════════════════════════════
# TICKER LIST
# ══════════════════════════════════════════════════════════════

def load_theme_universe():
    """Load ticker list from local_runner.theme_map.UNIVERSE."""
    from local_runner.theme_map import UNIVERSE
    tickers = sorted(set(UNIVERSE))
    print(f"  Loaded {len(tickers)} unique tickers from theme_map.UNIVERSE")
    if len(tickers) < 400 or len(tickers) > 2000:
        raise RuntimeError(
            f"UNIVERSE size {len(tickers)} is outside expected 400-2000 range. "
            f"Refusing to proceed -- check theme_map.py."
        )
    return tickers


# ══════════════════════════════════════════════════════════════
# CACHE MANAGEMENT
# ══════════════════════════════════════════════════════════════

def load_existing_cache():
    """Load existing cache if present. Returns dict."""
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tickers", {})
    return {}


def save_cache(tickers_data):
    """Save full cache to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Stats
    n_total = len(tickers_data)
    n_ok = sum(1 for v in tickers_data.values() if "error" not in v)
    n_err = sum(1 for v in tickers_data.values() if "error" in v)
    n_named = sum(1 for v in tickers_data.values()
                  if "error" not in v and v.get("longName"))
    n_summary = sum(1 for v in tickers_data.values()
                    if "error" not in v and v.get("longBusinessSummary"))

    cache = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_tickers": n_total,
        "n_success": n_ok,
        "n_errors": n_err,
        "n_with_longName": n_named,
        "n_with_longBusinessSummary": n_summary,
        "tickers": tickers_data,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    return cache


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(delay=DEFAULT_DELAY, max_tickers=None, retry_failed=False):
    print("\n" + "=" * 70)
    print("  COMPANY META FETCHER -- Yahoo Finance assetProfile")
    print("=" * 70)

    # Load ticker list
    print("\n  Loading theme_map.UNIVERSE...")
    all_tickers = load_theme_universe()

    # Load existing cache
    existing = load_existing_cache()
    print(f"  Existing cache: {len(existing)} tickers at {OUTPUT_FILE}")

    # Determine which tickers to fetch
    if retry_failed:
        # Only retry tickers that had errors
        to_fetch = [t for t in all_tickers
                    if t in existing and "error" in existing[t]]
        print(f"  Retrying {len(to_fetch)} failed tickers")
    else:
        # Skip tickers that already have successful data
        to_fetch = [t for t in all_tickers
                    if t not in existing or "error" in existing.get(t, {})]
        print(f"  To fetch: {len(to_fetch)} "
              f"(skipping {len(all_tickers) - len(to_fetch)} already cached)")

    if max_tickers:
        to_fetch = to_fetch[:max_tickers]
        print(f"  Limited to {max_tickers} tickers")

    if not to_fetch:
        print("\n  Nothing to fetch -- cache is complete!")
        return existing

    # Authenticate
    print("\n  Authenticating with Yahoo Finance...")
    opener, crumb = create_yahoo_session()
    print(f"  Crumb: {crumb[:8]}...")

    # Fetch loop
    print(f"\n  Fetching {len(to_fetch)} tickers "
          f"(delay: {delay}s, est: {len(to_fetch) * delay / 60:.1f} min)...\n")

    results = dict(existing)  # start with existing data
    n_ok = 0
    n_err = 0
    t0 = time.time()
    save_interval = 100  # save every N tickers

    for i, ticker in enumerate(to_fetch):
        data = fetch_ticker_data(opener, crumb, ticker)

        if data and "error" not in data:
            results[ticker] = data
            n_ok += 1
        elif data and data.get("error") == "rate_limited":
            # Back off and retry once
            print(f"  WARNING: Rate limited at {ticker}. Sleeping 30s...")
            time.sleep(30)
            # Re-authenticate (crumb may have expired)
            try:
                opener, crumb = create_yahoo_session()
            except Exception:
                pass
            data = fetch_ticker_data(opener, crumb, ticker)
            if data and "error" not in data:
                results[ticker] = data
                n_ok += 1
            else:
                results[ticker] = data or {"error": "rate_limit_retry_failed"}
                n_err += 1
        else:
            results[ticker] = data or {"error": "unknown"}
            n_err += 1

        # Progress
        if (i + 1) % 50 == 0 or i == len(to_fetch) - 1:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(to_fetch) - i - 1) / rate if rate > 0 else 0
            print(f"    {i + 1}/{len(to_fetch)}  "
                  f"ok={n_ok} err={n_err}  "
                  f"[{elapsed:.0f}s, {rate:.1f}/s, ~{eta:.0f}s left]")

        # Incremental save
        if (i + 1) % save_interval == 0:
            save_cache(results)

        time.sleep(delay)

    # Final save
    elapsed = time.time() - t0
    print(f"\n  Done. {n_ok} success, {n_err} errors in {elapsed:.0f}s")

    cache = save_cache(results)
    print(f"  Saved: {OUTPUT_FILE}")
    print(f"  Total: {cache['n_tickers']} tickers, "
          f"{cache['n_success']} with data, "
          f"{cache['n_errors']} errors")
    print(f"  With longName: {cache['n_with_longName']}  "
          f"With longBusinessSummary: {cache['n_with_longBusinessSummary']}")

    # Mirror to Railway
    try:
        from file_mirror import mirror_file
        mirror_file(OUTPUT_FILE)
        print(f"  Mirrored to Railway.")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    # Error breakdown
    err_kinds = {}
    for v in results.values():
        if "error" in v:
            k = v["error"]
            err_kinds[k] = err_kinds.get(k, 0) + 1
    if err_kinds:
        print(f"\n  Error breakdown:")
        for k, c in sorted(err_kinds.items(), key=lambda x: -x[1]):
            print(f"    {k:<35} {c:>5}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch company meta (longName + longBusinessSummary) "
                    "from Yahoo Finance for every ticker in theme_map.UNIVERSE"
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=f"Seconds between requests (default: {DEFAULT_DELAY})"
    )
    parser.add_argument(
        "--max-tickers", type=int, default=None,
        help="Max tickers to fetch (for testing)"
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Only retry tickers that previously failed"
    )
    args = parser.parse_args()

    run(delay=args.delay, max_tickers=args.max_tickers,
        retry_failed=args.retry_failed)
