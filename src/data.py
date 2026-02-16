"""
yfinance data fetching with simple file-based caching.
Avoids re-downloading data for tickers already fetched today.
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


def fetch_ohlcv(ticker: str, lookback_days: int = 120, use_cache: bool = True) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV data for a ticker.
    
    Args:
        ticker: Stock ticker symbol
        lookback_days: Number of trading days of history
        use_cache: Whether to use file-based cache
    
    Returns:
        DataFrame with OHLCV data, or None if fetch fails
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cache_file = _cache_path(ticker, today)
    
    # Check cache first
    if use_cache and os.path.exists(cache_file):
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass  # Cache corrupted, re-fetch
    
    # Fetch from yfinance
    # Add buffer days for weekends/holidays
    calendar_days = int(lookback_days * 1.6)
    start_date = datetime.now() - timedelta(days=calendar_days)
    
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(start=start_date.strftime("%Y-%m-%d"), end=today)
        
        if df.empty:
            print(f"  \u26a0 No data returned for {ticker}")
            return None
        
        # Keep only the columns we need
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        
        # Trim to exact lookback
        df = df.tail(lookback_days)
        
        # Cache the result
        if use_cache:
            df.to_parquet(cache_file)
        
        return df
        
    except Exception as e:
        print(f"  \u2717 Failed to fetch {ticker}: {e}")
        return None


def fetch_batch(tickers: list, lookback_days: int = 120) -> dict:
    """
    Fetch OHLCV data for multiple tickers.
    
    Returns:
        Dict mapping ticker -> DataFrame (skips failed fetches)
    """
    results = {}
    total = len(tickers)
    
    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i}/{total}] Fetching {ticker}...", end=" ")
        df = fetch_ohlcv(ticker, lookback_days)
        if df is not None:
            results[ticker] = df
            print(f"\u2713 ({len(df)} bars)")
        else:
            print("\u2717")
    
    print(f"\n  Fetched {len(results)}/{total} tickers successfully")
    return results
