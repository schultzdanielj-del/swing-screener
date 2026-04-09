"""
Fundamentals Fetcher — Yahoo Finance Ticker Data Cache Builder.

Fetches static fundamental data for every ticker in the tradable universe:
  - Sector (GICS-like from Yahoo)
  - Industry
  - Shares outstanding (current snapshot)
  - Float shares (current snapshot)

These feed the EV grinder's setup-specific features:
  - Market cap = shares_outstanding × close at signal date
  - Volume/float ratio = daily volume / float_shares
  - RS vs sector = ticker RS − avg sector RS
  - Sector RS vs SPY = avg sector RS − SPY RS

Data source: Yahoo Finance quoteSummary API (free, no key needed).
Rate: ~10 tickers/second. Full universe (~4,167 tickers) takes ~10-15 min.

Resumable — completed tickers are saved incrementally. Re-run to fill gaps.
Mirrors final cache to Railway via file_mirror.

Usage:
    python scripts/fetch_fundamentals.py
    python scripts/fetch_fundamentals.py --delay 0.3        # slower, safer
    python scripts/fetch_fundamentals.py --max-tickers 100  # test run
    python scripts/fetch_fundamentals.py --retry-failed     # retry only failed tickers
"""

import os
import sys
import json
import time
import pickle
import argparse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
OUTPUT_FILE = os.path.join(CACHE_DIR, "fundamentals_cache.json")

# Yahoo Finance API
YF_BASE = "https://query2.finance.yahoo.com"
YF_MODULES = "defaultKeyStatistics,assetProfile"

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
    """Fetch fundamentals for one ticker. Returns dict or None on failure."""
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

    r = result[0]
    ks = r.get("defaultKeyStatistics", {})
    ap = r.get("assetProfile", {})

    return {
        "sector": ap.get("sector") or None,
        "industry": ap.get("industry") or None,
        "shares_outstanding": ks.get("sharesOutstanding", {}).get("raw"),
        "float_shares": ks.get("floatShares", {}).get("raw"),
    }


# ══════════════════════════════════════════════════════════════
# TICKER LIST
# ══════════════════════════════════════════════════════════════

def load_universe_tickers():
    """Load ticker list from the daily OHLCV cache."""
    for name in ("universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                cache = pickle.load(f)
            tickers = sorted(cache.keys())
            print(f"  Loaded {len(tickers)} tickers from {name}")
            return tickers

    raise FileNotFoundError(
        "No OHLCV cache found in local_runner/cache/. "
        "Run cache_builder.py --daily first."
    )


# ══════════════════════════════════════════════════════════════
# CACHE MANAGEMENT
# ══════════════════════════════════════════════════════════════

def load_existing_cache():
    """Load existing cache if present. Returns dict."""
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
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
    sectors = set(v.get("sector") for v in tickers_data.values()
                  if v.get("sector"))

    cache = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_tickers": n_total,
        "n_success": n_ok,
        "n_errors": n_err,
        "n_sectors": len(sectors),
        "sectors": sorted(sectors),
        "tickers": tickers_data,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    return cache


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(delay=DEFAULT_DELAY, max_tickers=None, retry_failed=False):
    print("\n" + "=" * 70)
    print("  FUNDAMENTALS FETCHER -- Yahoo Finance")
    print("=" * 70)

    # Load ticker list
    print("\n  Loading universe tickers...")
    all_tickers = load_universe_tickers()

    # Load existing cache
    existing = load_existing_cache()
    print(f"  Existing cache: {len(existing)} tickers")

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
    n_skip = 0
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
    print(f"  Sectors: {cache['n_sectors']}")

    # Mirror to Railway
    try:
        from file_mirror import mirror_file
        mirror_file(OUTPUT_FILE)
        print(f"  Mirrored to Railway.")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    # Summary
    print(f"\n  Sector breakdown:")
    sector_counts = {}
    for v in results.values():
        s = v.get("sector") or "Unknown/Error"
        sector_counts[s] = sector_counts.get(s, 0) + 1
    for s, c in sorted(sector_counts.items(), key=lambda x: -x[1]):
        print(f"    {s:<35} {c:>5}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch fundamentals from Yahoo Finance"
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
