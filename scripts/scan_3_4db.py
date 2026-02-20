"""
3-4DB Scanner — Run 18 PCF conditions against universe OHLCV data.

Translates TC2000 PCF conditions into pandas operations,
scans every ticker×date combination in the lookback window,
and returns dates where ALL 18 conditions fire simultaneously.
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import os
import json
import sys

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def compute_indicators(df):
    """Add all needed indicators to a ticker's OHLCV dataframe."""
    df = df.sort_values("date").reset_index(drop=True)
    
    # Basic MAs
    df["sma50"] = df["close"].rolling(50).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()
    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    
    # ATR14
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift(1)),
            abs(df["low"] - df["close"].shift(1))
        )
    )
    df["atr14"] = df["tr"].rolling(14).mean()
    
    # Volume averages
    df["avgv20"] = df["volume"].rolling(20).mean()
    df["avgc20"] = df["close"].rolling(20).mean()
    
    # Lagged SMA50
    df["sma50_5ago"] = df["sma50"].shift(5)
    
    # Rolling highs/lows
    df["maxh2"] = df["high"].rolling(2).max()
    df["maxh15"] = df["high"].rolling(15).max()
    df["maxh30"] = df["high"].rolling(30).max()
    df["minl2"] = df["low"].rolling(2).min()
    df["minl3"] = df["low"].rolling(3).min()
    df["minl10"] = df["low"].rolling(10).min()
    
    # MAXH15 starting 2 bars ago (the high of bars 2-16 ago)
    df["maxh15_2ago"] = df["high"].shift(2).rolling(15).max()
    
    # Lagged EMA8 for "had close below 8ema in last 15 bars"
    for lag in range(1, 16):
        df[f"c{lag}"] = df["close"].shift(lag)
        df[f"ema8_{lag}ago"] = df["ema8"].shift(lag)
        df[f"ema12_{lag}ago"] = df["ema12"].shift(lag)
    
    return df


def check_conditions(df, idx):
    """Check all 18 conditions for a specific row index. Returns (bool, dict of results)."""
    if idx < 220:  # Need 200+ bars of history for SMA200
        return False, {}
    
    row = df.iloc[idx]
    
    # Quick null check on critical fields
    if pd.isna(row["sma50"]) or pd.isna(row["sma200"]) or pd.isna(row["atr14"]):
        return False, {}
    
    results = {}
    
    # 1. Close above SMA50
    results[1] = row["close"] > row["sma50"]
    
    # 2. SMA50 rising over 5 days
    results[2] = row["sma50"] > row["sma50_5ago"] if pd.notna(row["sma50_5ago"]) else False
    
    # 3. Close at least 0.3 ATR above SMA50
    results[3] = (row["close"] - row["sma50"]) > 0.3 * row["atr14"]
    
    # 4. At least 2 of last 3 days closed higher than prior day
    up_count = 0
    for lag in [1, 2, 3]:
        c_lag = row.get(f"c{lag}")
        c_lag1 = row.get(f"c{lag+1}") if lag < 3 else df.iloc[idx - 4]["close"] if idx >= 4 else None
        if lag == 1:
            c_lag1 = row.get("c2")
        elif lag == 2:
            c_lag1 = row.get("c3")
        elif lag == 3:
            c_lag1 = row.get("c4") if f"c4" in row.index else None
        
        if pd.notna(c_lag) and pd.notna(c_lag1) and c_lag > c_lag1:
            up_count += 1
    results[4] = up_count >= 2
    
    # 5. 3-day low < 2-day low (pullback low at least 3 bars ago)
    results[5] = row["minl3"] < row["minl2"] if pd.notna(row["minl3"]) and pd.notna(row["minl2"]) else False
    
    # 6. Close at least 0.8 ATR above 10-day low
    results[6] = (row["close"] - row["minl10"]) > 0.8 * row["atr14"] if pd.notna(row["minl10"]) else False
    
    # 7. Price at least 1.0 ATR below 15-day high
    results[7] = (row["maxh15"] - row["close"]) > 1.0 * row["atr14"] if pd.notna(row["maxh15"]) else False
    
    # 8. Had close below 8 EMA at some point in last 15 bars
    had_below_ema8 = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema8_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            had_below_ema8 = True
            break
    results[8] = had_below_ema8
    
    # 9. Not at new highs: MAXH15.2 > MAXH2
    results[9] = row["maxh15_2ago"] > row["maxh2"] if pd.notna(row["maxh15_2ago"]) and pd.notna(row["maxh2"]) else False
    
    # 10. Dollar volume floor: AvgC20 * AVGV20 > 5M
    results[10] = (row["avgc20"] * row["avgv20"]) > 5_000_000 if pd.notna(row["avgc20"]) and pd.notna(row["avgv20"]) else False
    
    # 11. Extended above SMA200: C - AVGC200 > 3 * ATR14
    results[11] = (row["close"] - row["sma200"]) > 3 * row["atr14"]
    
    # 12. Peak ext 50 in 30d: MAXH30 - AVGC50 > 0.30 * C
    results[12] = (row["maxh30"] - row["sma50"]) > 0.30 * row["close"] if pd.notna(row["maxh30"]) else False
    
    # 13. Retracement cap: (C - MINL10) / (MAXH30 - MINL10) < 0.7
    denom = row["maxh30"] - row["minl10"] if pd.notna(row["maxh30"]) and pd.notna(row["minl10"]) else 0
    if denom > 0:
        results[13] = (row["close"] - row["minl10"]) / denom < 0.7
    else:
        results[13] = False
    
    # 14. Volume below average: V < AVGV20
    results[14] = row["volume"] < row["avgv20"] if pd.notna(row["avgv20"]) else False
    
    # 15. High near EMA8: H - XAVGC8 < 1.1 * ATR14
    results[15] = (row["high"] - row["ema8"]) < 1.1 * row["atr14"]
    
    # 16. Small range: H - L < 1.1 * ATR14
    results[16] = (row["high"] - row["low"]) < 1.1 * row["atr14"]
    
    # 17. Had close below 12 EMA in last 15 bars
    had_below_ema12 = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema12_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            had_below_ema12 = True
            break
    results[17] = had_below_ema12
    
    # 18. Not too far from high: MAXH30 - C < 4.0 * ATR14
    results[18] = (row["maxh30"] - row["close"]) < 4.0 * row["atr14"] if pd.notna(row["maxh30"]) else False
    
    all_pass = all(results.values())
    return all_pass, results


def run_scan(lookback_days=77, db_path=None):
    """Run the 3-4DB scan across all universe tickers for the last N trading days."""
    if db_path:
        conn = sqlite3.connect(str(db_path), timeout=30)
    else:
        conn = get_db()
    
    cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    # Get tickers from tradable universe (if available), otherwise all
    tradable_check = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tradable_universe'"
    ).fetchone()[0]
    
    if tradable_check:
        tickers = [r[0] for r in conn.execute(
            "SELECT ticker FROM tradable_universe ORDER BY ticker"
        ).fetchall()]
        print(f"Scanning {len(tickers)} tradable tickers, signals after {cutoff_date}")
    else:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM universe_ohlcv ORDER BY ticker"
        ).fetchall()]
        print(f"Scanning {len(tickers)} universe tickers (no tradable filter), signals after {cutoff_date}")
    print("=" * 70)
    
    signals = []
    
    for i, ticker in enumerate(tickers):
        if (i + 1) % 500 == 0:
            print(f"  Progress: {i+1}/{len(tickers)} tickers scanned, {len(signals)} signals found...")
        
        # Load full history for this ticker (need 200+ bars before cutoff for SMA200)
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM universe_ohlcv "
            "WHERE ticker = ? ORDER BY date",
            (ticker,)
        ).fetchall()
        
        if len(rows) < 220:
            continue
        
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df = compute_indicators(df)
        
        # Find the index where cutoff_date starts
        cutoff_idx = df[df["date"] >= cutoff_date].index.min()
        if pd.isna(cutoff_idx):
            continue
        
        # Check each day in the lookback window
        for idx in range(max(cutoff_idx, 220), len(df)):
            all_pass, results = check_conditions(df, idx)
            if all_pass:
                row = df.iloc[idx]
                signals.append({
                    "ticker": ticker,
                    "date": row["date"],
                    "close": round(row["close"], 2),
                    "atr14": round(row["atr14"], 2),
                    "volume": int(row["volume"]),
                    "avgv20": int(row["avgv20"]),
                    "pct_above_sma50": round((row["close"] - row["sma50"]) / row["sma50"] * 100, 1),
                    "pct_above_sma200": round((row["close"] - row["sma200"]) / row["sma200"] * 100, 1),
                    "retracement": round((row["close"] - row["minl10"]) / (row["maxh30"] - row["minl10"]) * 100, 1) if (row["maxh30"] - row["minl10"]) > 0 else 0,
                })
    
    conn.close()
    
    print(f"\n{'=' * 70}")
    print(f"SCAN COMPLETE: {len(signals)} signals found across {len(tickers)} tickers")
    print(f"{'=' * 70}\n")
    
    if signals:
        sdf = pd.DataFrame(signals).sort_values(["date", "ticker"])
        
        # Group by date
        for date, group in sdf.groupby("date"):
            print(f"\n📅 {date} ({len(group)} signals)")
            print(f"{'─' * 60}")
            for _, s in group.iterrows():
                print(f"  {s['ticker']:6s}  ${s['close']:>8.2f}  ATR={s['atr14']:.2f}  "
                      f"retrace={s['retracement']:.0f}%  "
                      f"+{s['pct_above_sma50']:.0f}% SMA50  +{s['pct_above_sma200']:.0f}% SMA200")
        
        return sdf
    
    return pd.DataFrame()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 77
    run_scan(lookback_days=days)
