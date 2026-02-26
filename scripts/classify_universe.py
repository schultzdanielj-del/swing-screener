"""
Classify Universe — One-time (quarterly) scan to identify ETFs, leveraged products,
inverse funds, single-stock derivatives, etc.

Stores results in two Railway DB tables:
  - ticker_classification: full classification for every ticker
  - universe_exclusions: tickers to permanently exclude from tradable_universe

Usage:
    python scripts/classify_universe.py [--dry-run]

The daily universe rebuild checks universe_exclusions and skips anything listed.
Re-run quarterly to catch new tickers.
"""

import sys
import os
import time
import json
import requests
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

API_BASE = "https://web-production-e3025.up.railway.app"
MAX_WORKERS = 10  # yfinance rate limit friendly
BATCH_SIZE = 50   # commit progress every N tickers


def get_all_tickers():
    """Get all tickers from tradable_universe."""
    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": "SELECT ticker FROM tradable_universe ORDER BY ticker",
        "limit": 5000
    }, timeout=30)
    r.raise_for_status()
    return [row["ticker"] for row in r.json()["results"]]


def get_already_classified():
    """Get tickers already in ticker_classification table."""
    try:
        r = requests.post(f"{API_BASE}/api/query/bulk", json={
            "sql": "SELECT ticker FROM ticker_classification",
            "limit": 10000
        }, timeout=30)
        if r.status_code == 200:
            return set(row["ticker"] for row in r.json().get("results", []))
    except:
        pass
    return set()


def classify_ticker(ticker):
    """Classify a single ticker via yfinance. Retries on transient errors."""
    quote_type = "UNKNOWN"
    name = ""
    category = ""
    fund_family = ""

    for attempt in range(3):
        try:
            info = yf.Ticker(ticker).info
            if not info or (info.get("trailingPegRatio") is None and info.get("quoteType") is None):
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
            quote_type = info.get("quoteType", "UNKNOWN")
            name = info.get("shortName") or info.get("longName") or ""
            category = info.get("category") or ""
            fund_family = info.get("fundFamily") or ""
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return {
                "ticker": ticker, "quote_type": "ERROR", "classification": "unknown",
                "name": str(e)[:200], "category": "", "fund_family": "",
                "exclude": False, "exclude_reason": None,
            }
    else:
        return {
            "ticker": ticker, "quote_type": "ERROR", "classification": "unknown",
            "name": "Max retries exceeded", "category": "", "fund_family": "",
            "exclude": False, "exclude_reason": None,
        }

    # --- Classification logic ---
    if quote_type == "EQUITY":
        classification = "equity"
        exclude = False
        reason = None

    elif quote_type == "ETF":
        name_lower = name.lower()
        cat_lower = category.lower()
        fam_lower = fund_family.lower()

        if "leveraged" in cat_lower or "2x" in name_lower or "3x" in name_lower or \
           "ultra" in name_lower or "ultrapro" in name_lower or \
           "bull 2x" in name_lower or "bull 3x" in name_lower or \
           "bear 2x" in name_lower or "bear 3x" in name_lower or \
           "daily bull" in name_lower or "daily bear" in name_lower:
            classification = "etf_leveraged"
            exclude = True
            reason = f"Leveraged ETF: {name}"

        elif "inverse" in cat_lower or "inverse" in name_lower or \
             "short " in name_lower or "bear " in name_lower or \
             "-1x" in name_lower or "-2x" in name_lower or "-3x" in name_lower or \
             cat_lower.startswith("trading--inverse"):
            classification = "etf_inverse"
            exclude = True
            reason = f"Inverse ETF: {name}"

        elif "derivative income" in cat_lower or "option income" in name_lower or \
             "covered call" in name_lower or "yieldmax" in fam_lower or \
             "option strategy" in name_lower:
            classification = "etf_derivative_income"
            exclude = True
            reason = f"Derivative income ETF: {name}"

        elif fam_lower in ("graniteshares", "direxion funds", "rex shares",
                           "axs investments", "tuttle capital management"):
            single_stock_names = [
                "nvda", "tsla", "aapl", "amzn", "msft", "goog", "meta",
                "amd", "nflx", "coin", "mstr", "pltr", "snow", "shop",
                "baba", "nio", "uber", "crwd", "sq ", "pypl", "roku",
            ]
            is_single = any(s in name_lower for s in single_stock_names)
            if is_single:
                classification = "etf_single_stock"
                exclude = True
                reason = f"Single-stock ETF: {name}"
            elif "leveraged" in cat_lower or "2x" in name_lower or "3x" in name_lower:
                classification = "etf_leveraged"
                exclude = True
                reason = f"Leveraged ETF: {name}"
            else:
                classification = "etf_plain"
                exclude = False
                reason = None

        elif "volatility" in cat_lower or "vix" in name_lower or \
             "volatility" in name_lower:
            classification = "etf_volatility"
            exclude = True
            reason = f"Volatility product: {name}"

        elif "bitcoin" in name_lower and ("leveraged" in cat_lower or "2x" in name_lower):
            classification = "etf_leveraged"
            exclude = True
            reason = f"Leveraged crypto: {name}"

        else:
            classification = "etf_plain"
            exclude = False
            reason = None
    else:
        classification = quote_type.lower()
        exclude = True
        reason = f"Non-equity/ETF: {quote_type} - {name}"

    return {
        "ticker": ticker, "quote_type": quote_type, "classification": classification,
        "name": name[:200], "category": category[:100], "fund_family": fund_family[:100],
        "exclude": exclude, "exclude_reason": reason,
    }


def create_tables():
    """Create classification and exclusion tables if they don't exist."""
    # ticker_classification
    requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": """CREATE TABLE IF NOT EXISTS ticker_classification (
            ticker TEXT PRIMARY KEY,
            quote_type TEXT,
            classification TEXT,
            name TEXT,
            category TEXT,
            fund_family TEXT,
            exclude INTEGER DEFAULT 0,
            exclude_reason TEXT,
            classified_at TEXT
        )""",
        "limit": 1
    }, timeout=30)

    # universe_exclusions - the permanent exclude list
    requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": """CREATE TABLE IF NOT EXISTS universe_exclusions (
            ticker TEXT PRIMARY KEY,
            reason TEXT,
            added_at TEXT
        )""",
        "limit": 1
    }, timeout=30)


def store_results(results):
    """Store classification results and exclusions in DB via bulk inserts."""
    now = datetime.utcnow().isoformat()

    # Build bulk classification insert
    values_cls = []
    values_excl = []
    for r in results:
        values_cls.append(
            f"('{r['ticker']}', '{r['quote_type']}', '{r['classification']}', "
            f"'{r['name'].replace(chr(39), '')}', '{r['category'].replace(chr(39), '')}', "
            f"'{r['fund_family'].replace(chr(39), '')}', "
            f"{1 if r['exclude'] else 0}, "
            f"{'NULL' if not r['exclude_reason'] else chr(39) + r['exclude_reason'].replace(chr(39), '') + chr(39)}, "
            f"'{now}')"
        )
        if r["exclude"]:
            reason_safe = r["exclude_reason"].replace("'", "") if r["exclude_reason"] else ""
            values_excl.append(f"('{r['ticker']}', '{reason_safe}', '{now}')")

    # Bulk upsert classifications
    if values_cls:
        sql = (
            "INSERT OR REPLACE INTO ticker_classification "
            "(ticker, quote_type, classification, name, category, fund_family, exclude, exclude_reason, classified_at) "
            "VALUES " + ", ".join(values_cls)
        )
        try:
            requests.post(f"{API_BASE}/api/query/bulk", json={"sql": sql, "limit": 1}, timeout=60)
        except Exception as e:
            print(f"  WARNING: Failed to store classifications batch: {e}")

    # Bulk upsert exclusions
    if values_excl:
        sql = (
            "INSERT OR REPLACE INTO universe_exclusions (ticker, reason, added_at) "
            "VALUES " + ", ".join(values_excl)
        )
        try:
            requests.post(f"{API_BASE}/api/query/bulk", json={"sql": sql, "limit": 1}, timeout=60)
        except Exception as e:
            print(f"  WARNING: Failed to store exclusions batch: {e}")


def remove_excluded_from_universe():
    """Delete excluded tickers from tradable_universe."""
    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": """DELETE FROM tradable_universe 
                  WHERE ticker IN (SELECT ticker FROM universe_exclusions)""",
        "limit": 1
    }, timeout=30)
    return r.json()


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    print("=" * 60)
    print("  UNIVERSE CLASSIFIER")
    print("  Classifies all tradable_universe tickers via yfinance")
    print("=" * 60)

    # Get tickers
    tickers = get_all_tickers()
    print(f"\nTradable universe: {len(tickers)} tickers")

    # Skip already classified unless --force
    if not force:
        already = get_already_classified()
        tickers = [t for t in tickers if t not in already]
        if already:
            print(f"Already classified: {len(already)} (skipping, use --force to redo)")
    
    if not tickers:
        print("Nothing to classify. Use --force to reclassify all.")
        # Still apply exclusions
        if not dry_run:
            print("\nApplying exclusions to tradable_universe...")
            remove_excluded_from_universe()
            r = requests.post(f"{API_BASE}/api/query/bulk", json={
                "sql": "SELECT COUNT(*) as cnt FROM tradable_universe",
                "limit": 1
            }, timeout=30)
            print(f"Tradable universe now: {r.json()['results'][0]['cnt']} tickers")
        return

    print(f"To classify: {len(tickers)} tickers")

    if not dry_run:
        print("\nCreating tables...")
        create_tables()

    # Classify concurrently
    print(f"\nClassifying ({MAX_WORKERS} threads)...")
    t0 = time.time()
    results = []
    batch = []

    stats = {"equity": 0, "etf_plain": 0, "etf_leveraged": 0, "etf_inverse": 0,
             "etf_single_stock": 0, "etf_derivative_income": 0, "etf_volatility": 0,
             "unknown": 0, "error": 0}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(classify_ticker, t): t for t in tickers}
        done = 0

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            batch.append(result)
            done += 1

            cls = result["classification"]
            if cls in stats:
                stats[cls] += 1
            elif result["quote_type"] == "ERROR":
                stats["error"] += 1
            else:
                stats["unknown"] += 1

            # Store in batches
            if len(batch) >= BATCH_SIZE and not dry_run:
                store_results(batch)
                batch = []

            if done % 100 == 0 or done == len(tickers):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(tickers) - done) / rate if rate > 0 else 0
                print(f"  {done:,}/{len(tickers):,} classified "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]")

    # Store remaining batch
    if batch and not dry_run:
        store_results(batch)

    elapsed = time.time() - t0
    print(f"\nClassification complete in {elapsed:.0f}s ({elapsed/60:.1f} min)")

    # Save full results to local JSON as backup
    results_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "local_runner", "cache", "classification.json")
    os.makedirs(os.path.dirname(results_file), exist_ok=True)
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved local backup: {results_file}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"  CLASSIFICATION SUMMARY")
    print(f"{'='*60}")
    total_exclude = sum(1 for r in results if r["exclude"])
    total_keep = sum(1 for r in results if not r["exclude"])
    print(f"  Keep:    {total_keep:,}")
    print(f"  Exclude: {total_exclude:,}")
    print()
    for cls, count in sorted(stats.items(), key=lambda x: -x[1]):
        marker = " ✗" if cls.startswith("etf_") and cls != "etf_plain" else ""
        if cls in ("unknown", "error") and count > 0:
            marker = " ?"
        print(f"  {cls:30s} {count:>5,}{marker}")

    # Show excluded tickers
    excluded = [r for r in results if r["exclude"]]
    if excluded:
        print(f"\n--- Excluded tickers ({len(excluded)}) ---")
        for r in sorted(excluded, key=lambda x: x["classification"]):
            print(f"  {r['ticker']:8s} {r['classification']:25s} {r['name'][:50]}")

    if dry_run:
        print(f"\n[DRY RUN — no changes made. Remove --dry-run to apply.]")
        return

    # Apply exclusions
    print(f"\nRemoving {total_exclude} excluded tickers from tradable_universe...")
    remove_excluded_from_universe()

    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": "SELECT COUNT(*) as cnt FROM tradable_universe",
        "limit": 1
    }, timeout=30)
    print(f"Tradable universe now: {r.json()['results'][0]['cnt']} tickers")
    print("\nDone. Run quarterly to catch new tickers.")


if __name__ == "__main__":
    main()
