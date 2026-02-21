"""
DTSS Scanner — Run 12 conditions against universe OHLCV data.

Translates TC2000 PCF conditions into pandas operations,
scans every ticker×date combination in the lookback window,
and returns dates where ALL 12 conditions fire simultaneously.

Conditions (all tested ANDed on single bars, 23/23 examples pass):
 1. AVGC50 > AVGC50.10 - 0.2 * ATR14       (SMA50 not collapsing)
 2. MAXH20 - MINL20 > 3.0 * ATR14           (big 20-day range)
 3. MAXH20 - H < 3.0 * ATR14                (near recent high)
 4. H - L >= 0.6 * ATR14                    (not tiny candle)
 5. H - C >= 0.05 * ATR14                   (didn't close at HOD)
 6. (H - C) + (H - O) >= 0.5 * ATR14        (selling pressure)
 7. MINC15 < XAVGC8                          (had pullback below EMA8)
 8. V > 0.5 * AVGV20                         (not dead volume)
 9. V < 3.0 * AVGV20                         (not blow-off volume)
10. up_bars_5 >= 1                           (at least 1 up close in 5)
11. H - MINL65 > 3.0 * ATR14                (came from real move)
12. C - MINL65 > 2.5 * ATR14                (still elevated)
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import os
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

    # MAs
    df["sma50"] = df["close"].rolling(50).mean()
    df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()

    # ATR14
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift(1)),
            abs(df["low"] - df["close"].shift(1))
        )
    )
    df["atr14"] = df["tr"].rolling(14).mean()

    # Volume average
    df["avgv20"] = df["volume"].rolling(20).mean()

    # Lagged SMA50 (10 bars ago)
    df["sma50_10ago"] = df["sma50"].shift(10)

    # Rolling highs/lows
    df["maxh20"] = df["high"].rolling(20).max()
    df["minl20"] = df["low"].rolling(20).min()
    df["minl65"] = df["low"].rolling(65).min()
    df["minc15"] = df["close"].rolling(15).min()

    # Up bars in last 5
    df["up1"] = (df["close"] > df["close"].shift(1)).astype(int)
    df["up5"] = df["up1"].rolling(5).sum()

    return df


def check_conditions(df, idx):
    """Check all 12 DTSS conditions on a single bar. Returns (bool, dict)."""
    if idx < 65:  # Need 65 bars for MINL65
        return False, {}

    r = df.iloc[idx]

    atr = r["atr14"]
    if pd.isna(atr) or atr <= 0:
        return False, {}

    sma50 = r["sma50"]
    sma50_10 = r["sma50_10ago"]
    ema8 = r["ema8"]
    h = r["high"]
    l = r["low"]
    c = r["close"]
    o = r["open"]
    v = r["volume"]
    avgv20 = r["avgv20"]
    maxh20 = r["maxh20"]
    minl20 = r["minl20"]
    minl65 = r["minl65"]
    minc15 = r["minc15"]
    up5 = r["up5"]

    # Skip if any needed value is NaN
    needed = [sma50, sma50_10, ema8, avgv20, maxh20, minl20, minl65, minc15, up5]
    if any(pd.isna(x) for x in needed):
        return False, {}

    results = {}

    # 1. SMA50 slope: AVGC50 > AVGC50.10 - 0.2 * ATR14
    results["sma50slope"] = sma50 > sma50_10 - 0.2 * atr

    # 2. Range: MAXH20 - MINL20 > 3.0 * ATR14
    results["range20"] = (maxh20 - minl20) > 3.0 * atr

    # 3. Near high: MAXH20 - H < 3.0 * ATR14
    results["near_high"] = (maxh20 - h) < 3.0 * atr

    # 4. Not tiny: H - L >= 0.6 * ATR14
    results["not_tiny"] = (h - l) >= 0.6 * atr

    # 5. Not HOD close: H - C >= 0.05 * ATR14
    results["not_hod"] = (h - c) >= 0.05 * atr

    # 6. Selling pressure: (H-C) + (H-O) >= 0.5 * ATR14
    results["sell_pressure"] = (h - c) + (h - o) >= 0.5 * atr

    # 7. Pullback: MINC15 < XAVGC8
    results["pullback"] = minc15 < ema8

    # 8. Volume floor: V > 0.5 * AVGV20
    results["vol_floor"] = v > 0.5 * avgv20

    # 9. Volume cap: V < 3.0 * AVGV20
    results["vol_cap"] = v < 3.0 * avgv20

    # 10. Up bars: at least 1 up close in last 5
    results["up_bars"] = up5 >= 1

    # 11. From real move: H - MINL65 > 3.0 * ATR14
    results["h_from_low"] = (h - minl65) > 3.0 * atr

    # 12. Still elevated: C - MINL65 > 2.5 * ATR14
    results["c_from_low"] = (c - minl65) > 2.5 * atr

    all_pass = all(results.values())
    return all_pass, results


def run_scan(lookback_days=77, db_path=None):
    """Scan all universe tickers for DTSS setups in the last N trading days."""
    if db_path is None:
        db_path = str(DB_PATH)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    # Get cutoff date
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Get all tickers in universe_ohlcv
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM universe_ohlcv ORDER BY ticker"
    ).fetchall()]

    print(f"[DTSS] Scanning {len(tickers)} tickers, lookback={lookback_days} days (cutoff={cutoff})")

    signals = []
    tickers_with_signals = set()

    for i, ticker in enumerate(tickers):
        if i % 500 == 0 and i > 0:
            print(f"  ...processed {i}/{len(tickers)}, {len(signals)} signals so far")

        # Fetch OHLCV — need extra history for indicators
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM universe_ohlcv "
            "WHERE ticker = ? ORDER BY date",
            (ticker,)
        ).fetchall()

        if len(rows) < 100:
            continue

        df = pd.DataFrame([dict(r) for r in rows])
        df = compute_indicators(df)

        # Only check bars in lookback window
        mask = df["date"] >= cutoff
        check_indices = df.index[mask].tolist()

        for idx in check_indices:
            passed, results = check_conditions(df, idx)
            if passed:
                r = df.iloc[idx]
                signals.append({
                    "ticker": ticker,
                    "date": r["date"],
                    "close": round(float(r["close"]), 2),
                    "atr14": round(float(r["atr14"]), 2),
                    "volume": int(r["volume"]),
                    "avgv20": int(r["avgv20"]),
                    "range20_atr": round((r["maxh20"] - r["minl20"]) / r["atr14"], 2),
                    "h_from_low65": round((r["high"] - r["minl65"]) / r["atr14"], 2),
                    "near_high": round((r["maxh20"] - r["high"]) / r["atr14"], 2),
                })
                tickers_with_signals.add(ticker)

    conn.close()

    print(f"[DTSS] Done: {len(signals)} signals across {len(tickers_with_signals)} tickers")

    if signals:
        return pd.DataFrame(signals)
    return pd.DataFrame()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 77
    result = run_scan(lookback_days=days)
    if not result.empty:
        print(f"\n{len(result)} total signals:")
        # Show most recent
        recent = result.sort_values("date", ascending=False).head(20)
        for _, row in recent.iterrows():
            print(f"  {row['ticker']:8s} {row['date']}  close={row['close']:8.2f}  rng20={row['range20_atr']:.1f}x  h_low65={row['h_from_low65']:.1f}x")
    else:
        print("No signals found.")
