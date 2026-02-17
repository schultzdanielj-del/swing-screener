"""
yfinance data fetching with bulk download and file-based caching.
Uses yf.download() to fetch all tickers in a single request,
avoiding per-ticker rate limits from Yahoo Finance.
"""
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf


CACHE_DIR = ".cache/data"


def _cache_path(ticker: str, date_str: str) -> str:
    """Get cache file path for a ticker on a given date."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{ticker}_{date_str}.parquet")


def _load_cached(tickers: list, date_str: str) -> dict:
    """Load any cached data for today. Returns dict of ticker -> DataFrame."""
    cached = {}
    for ticker in tickers:
        cache_file = _cache_path(ticker, date_str)
        if os.path.exists(cache_file):
            try:
                cached[ticker] = pd.read_parquet(cache_file)
            except Exception:
                pass  # Cache corrupted, will re-fetch
    return cached


def _save_cache(data: dict, date_str: str):
    """Save fetched data to cache."""
    for ticker, df in data.items():
        try:
            cache_file = _cache_path(ticker, date_str)
            df.to_parquet(cache_file)
        except Exception:
            pass  # Non-critical, skip silently


def fetch_batch(tickers: list, lookback_days: int = 120, use_cache: bool = True) -> dict:
    """
    Fetch OHLCV data for multiple tickers using yf.download() bulk request.
    
    This makes a single API call for all tickers instead of one per ticker,
    dramatically reducing the chance of rate limiting from Yahoo Finance.
    
    Args:
        tickers: List of stock ticker symbols
        lookback_days: Number of trading days of history
        use_cache: Whether to use file-based cache
    
    Returns:
        Dict mapping ticker -> DataFrame (skips failed fetches)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    results = {}
    
    # Step 1: Load cached tickers
    if use_cache:
        cached = _load_cached(tickers, today)
        if cached:
            print(f"  Loaded {len(cached)} tickers from cache")
            results.update(cached)
    
    # Step 2: Determine which tickers still need fetching
    remaining = [t for t in tickers if t not in results]
    
    if not remaining:
        print(f"  All {len(tickers)} tickers loaded from cache")
        return results
    
    # Step 3: Bulk download remaining tickers
    calendar_days = int(lookback_days * 1.6)
    start_date = (datetime.now() - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    
    print(f"  Bulk downloading {len(remaining)} tickers...")
    
    try:
        raw = yf.download(
            remaining,
            start=start_date,
            end=today,
            group_by='ticker',
            threads=True,
            progress=True
        )
        
        if raw.empty:
            print("  ⚠ Bulk download returned no data")
            return results
        
        # Step 4: Split bulk data into per-ticker DataFrames
        fetched = {}
        
        if len(remaining) == 1:
            # Single ticker: yf.download returns flat columns
            ticker = remaining[0]
            df = raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
            df = df.dropna()
            df = df.tail(lookback_days)
            if not df.empty:
                fetched[ticker] = df
                print(f"  ✓ {ticker} ({len(df)} bars)")
            else:
                print(f"  ✗ {ticker} (no data)")
        else:
            # Multiple tickers: grouped by ticker
            for ticker in remaining:
                try:
                    df = raw[ticker][['Open', 'High', 'Low', 'Close', 'Volume']].copy()
                    df = df.dropna()
                    df = df.tail(lookback_days)
                    if not df.empty:
                        fetched[ticker] = df
                        print(f"  ✓ {ticker} ({len(df)} bars)")
                    else:
                        print(f"  ✗ {ticker} (no data)")
                except (KeyError, Exception) as e:
                    print(f"  ✗ {ticker} ({e})")
        
        # Step 5: Cache newly fetched data
        if use_cache and fetched:
            _save_cache(fetched, today)
        
        results.update(fetched)
        
    except Exception as e:
        print(f"  ✗ Bulk download failed: {e}")
        print("  Falling back to individual fetches...")
        
        # Fallback: fetch one at a time with delays
        import time
        for ticker in remaining:
            try:
                stock = yf.Ticker(ticker)
                df = stock.history(start=start_date, end=today)
                if not df.empty:
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
                    df = df.tail(lookback_days)
                    results[ticker] = df
                    if use_cache:
                        _save_cache({ticker: df}, today)
                    print(f"  ✓ {ticker} ({len(df)} bars)")
                else:
                    print(f"  ✗ {ticker} (no data)")
                time.sleep(0.5)  # Be gentle on fallback
            except Exception as e2:
                print(f"  ✗ {ticker} ({e2})")
    
    print(f"\n  Fetched {len(results)}/{len(tickers)} tickers successfully")
    return results
