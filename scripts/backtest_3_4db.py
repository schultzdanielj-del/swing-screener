"""
3-4DB Full Backtest — Scan 5 years of history for all tradable tickers.

Runs the 18 PCF conditions against every ticker×date in the universe,
stores raw results in scan_backtest table, then post-processes to remove
biotech, leveraged ETFs, and duplicate instruments.

Designed to run as a background task — can take 30-60+ minutes.
Progress tracked via backtest_status table.
"""

import os
import sqlite3
import time
import traceback
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backtest")


# ── Known leveraged / inverse / derivative ETFs ──────────────────────────
# These prefixes and full tickers cover the vast majority of leveraged products
LEVERAGED_INVERSE_PATTERNS = {
    # Direxion
    "TQQQ", "SQQQ", "SPXL", "SPXS", "TNA", "TZA", "SOXL", "SOXS",
    "LABU", "LABD", "FAS", "FAZ", "TECL", "TECS", "NUGT", "DUST",
    "JNUG", "JDST", "ERX", "ERY", "CURE", "NAIL", "DPST", "DRIP",
    "GUSH", "MIDU", "MIDZ", "SMDD", "SRTY", "URTY", "UDOW", "SDOW",
    "UMDD", "RETL", "DRV", "DRN", "WEBL", "WEBS", "HIBL", "HIBS",
    "WANT", "BERZ", "MEXX", "INDL", "YINN", "YANG", "EDC", "EDZ",
    # ProShares
    "SSO", "SDS", "QLD", "QID", "UWM", "TWM", "MVV", "MZZ",
    "DDM", "DXD", "SAA", "SDD", "ROM", "REW", "UYM", "SMN",
    "UGE", "SZK", "UPW", "SDP", "UCC", "SCC", "UBR", "UBT",
    "UST", "TBT", "TBF", "UPV", "URE", "SRS", "SKF", "UYG",
    "SVXY", "UVXY", "VIXY", "UCO", "SCO", "BOIL", "KOLD",
    "AGQ", "ZSL", "UGL", "GLL", "ULE", "EUO", "YCL", "YCS",
    # GraniteShares / MicroSectors
    "NVDL", "NVDS", "NVDU", "NVDD", "TSLL", "TSLS", "TSLR", "TSDD",
    "CONL", "CONY", "MSTY", "MSFU", "MSFD", "GGLL", "GGLS",
    "AMZU", "AMZD", "AAPD", "AAPU",
    # Volatility
    "VXX", "VXZ", "UVIX", "SVIX", "ZIVB",
    # Leveraged single-stock (Defiance, REX, etc.)
    "TSLY", "NVDY", "APLY", "OARK", "BITX", "BITU", "SBIT",
    "MSTU", "MSTX", "MSTZ", "NFLY", "GOOY", "AMZY",
    # Leveraged crypto
    "BITW", "BITO", "BTF", "GBTC", "ETHE", "ETHU", "ETHD",
    # Other leveraged
    "SPYU", "BULZ", "FNGU", "FNGD", "BNKU", "BNKD", "NRGU", "NRGD",
    "BRZU", "KORU", "JPNL",
}

# Known biotech SIC codes and industry keywords
BIOTECH_INDUSTRIES = {
    "biotechnology", "biopharmaceuticals", "drug manufacturers",
    "pharmaceutical", "drug discovery", "gene therapy",
    "biological products", "medical therapeutics",
}

# ETF-like suffixes/patterns that indicate leveraged or derivative products
ETF_INDICATOR_PATTERNS = [
    # 3x, 2x, -1x, -2x, -3x in name
    "3x", "2x", "-1x", "-2x", "-3x",
    "ultra", "ultrapro", "ultrashort",
    "bull", "bear",  # only in ETF context
    "leveraged", "inverse",
    "direxion", "proshares",
]


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_backtest_tables(db):
    """Create tables for backtest results and status tracking."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS scan_backtest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            atr14 REAL,
            volume INTEGER,
            avgv20 INTEGER,
            pct_above_sma50 REAL,
            pct_above_sma200 REAL,
            retracement REAL,
            UNIQUE(ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_ticker ON scan_backtest(ticker);
        CREATE INDEX IF NOT EXISTS idx_backtest_date ON scan_backtest(date);

        CREATE TABLE IF NOT EXISTS scan_backtest_clean (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            atr14 REAL,
            volume INTEGER,
            avgv20 INTEGER,
            pct_above_sma50 REAL,
            pct_above_sma200 REAL,
            retracement REAL,
            UNIQUE(ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_clean_ticker ON scan_backtest_clean(ticker);
        CREATE INDEX IF NOT EXISTS idx_backtest_clean_date ON scan_backtest_clean(date);

        CREATE TABLE IF NOT EXISTS ticker_sectors (
            ticker TEXT PRIMARY KEY,
            sector TEXT,
            industry TEXT,
            name TEXT,
            is_etf INTEGER DEFAULT 0,
            fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS backtest_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT DEFAULT 'idle',
            started_at TEXT,
            updated_at TEXT,
            total_tickers INTEGER DEFAULT 0,
            completed_tickers INTEGER DEFAULT 0,
            signals_found INTEGER DEFAULT 0,
            current_ticker TEXT,
            phase TEXT DEFAULT 'idle',
            error TEXT
        );

        INSERT OR IGNORE INTO backtest_status (id, state) VALUES (1, 'idle');
    """)
    db.commit()


def update_status(db, **kwargs):
    """Update backtest status row."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    vals.append(datetime.utcnow().isoformat())
    db.execute(f"UPDATE backtest_status SET {sets}, updated_at = ? WHERE id = 1", vals)
    db.commit()


def compute_indicators(df):
    """Add all needed indicators to a ticker's OHLCV dataframe."""
    df = df.sort_values("date").reset_index(drop=True)

    df["sma50"] = df["close"].rolling(50).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    df["ema8"] = df["close"].ewm(span=8, adjust=False).mean()
    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift(1)),
            abs(df["low"] - df["close"].shift(1))
        )
    )
    df["atr14"] = df["tr"].rolling(14).mean()

    df["avgv20"] = df["volume"].rolling(20).mean()
    df["avgc20"] = df["close"].rolling(20).mean()
    df["sma50_5ago"] = df["sma50"].shift(5)

    df["maxh2"] = df["high"].rolling(2).max()
    df["maxh15"] = df["high"].rolling(15).max()
    df["maxh30"] = df["high"].rolling(30).max()
    df["minl2"] = df["low"].rolling(2).min()
    df["minl3"] = df["low"].rolling(3).min()
    df["minl10"] = df["low"].rolling(10).min()
    df["maxh15_2ago"] = df["high"].shift(2).rolling(15).max()

    for lag in range(1, 16):
        df[f"c{lag}"] = df["close"].shift(lag)
        df[f"ema8_{lag}ago"] = df["ema8"].shift(lag)
        df[f"ema12_{lag}ago"] = df["ema12"].shift(lag)

    # c4 needed for condition 4
    df["c4"] = df["close"].shift(4)

    return df


def check_conditions(row, df_idx, df):
    """Check all 18 conditions. Returns True if all pass."""
    if pd.isna(row["sma50"]) or pd.isna(row["sma200"]) or pd.isna(row["atr14"]):
        return False
    if row["atr14"] <= 0:
        return False

    # 1. Close above SMA50
    if not (row["close"] > row["sma50"]):
        return False

    # 2. SMA50 rising
    if pd.isna(row["sma50_5ago"]) or not (row["sma50"] > row["sma50_5ago"]):
        return False

    # 3. Close at least 0.3 ATR above SMA50
    if not ((row["close"] - row["sma50"]) > 0.3 * row["atr14"]):
        return False

    # 4. At least 2 of last 3 closes higher than prior
    up_count = 0
    c1 = row.get("c1")
    c2 = row.get("c2")
    c3 = row.get("c3")
    c4 = row.get("c4")
    if pd.notna(c1) and pd.notna(c2) and c1 > c2:
        up_count += 1
    if pd.notna(c2) and pd.notna(c3) and c2 > c3:
        up_count += 1
    if pd.notna(c3) and pd.notna(c4) and c3 > c4:
        up_count += 1
    if up_count < 2:
        return False

    # 5. 3-day low < 2-day low
    if pd.isna(row["minl3"]) or pd.isna(row["minl2"]) or not (row["minl3"] < row["minl2"]):
        return False

    # 6. Close at least 0.8 ATR above 10-day low
    if pd.isna(row["minl10"]) or not ((row["close"] - row["minl10"]) > 0.8 * row["atr14"]):
        return False

    # 7. Price at least 1.0 ATR below 15-day high
    if pd.isna(row["maxh15"]) or not ((row["maxh15"] - row["close"]) > 1.0 * row["atr14"]):
        return False

    # 8. Had close below 8 EMA in last 15 bars
    had_below = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema8_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            had_below = True
            break
    if not had_below:
        return False

    # 9. Not at new highs
    if pd.isna(row["maxh15_2ago"]) or pd.isna(row["maxh2"]) or not (row["maxh15_2ago"] > row["maxh2"]):
        return False

    # 10. Dollar volume floor
    if pd.isna(row["avgc20"]) or pd.isna(row["avgv20"]) or not ((row["avgc20"] * row["avgv20"]) > 5_000_000):
        return False

    # 11. Extended above SMA200
    if not ((row["close"] - row["sma200"]) > 3 * row["atr14"]):
        return False

    # 12. Peak ext 50 in 30d
    if pd.isna(row["maxh30"]) or not ((row["maxh30"] - row["sma50"]) > 0.30 * row["close"]):
        return False

    # 13. Retracement cap
    denom = row["maxh30"] - row["minl10"] if pd.notna(row["maxh30"]) and pd.notna(row["minl10"]) else 0
    if denom <= 0 or not ((row["close"] - row["minl10"]) / denom < 0.7):
        return False

    # 14. Volume below average
    if pd.isna(row["avgv20"]) or not (row["volume"] < row["avgv20"]):
        return False

    # 15. High near EMA8
    if not ((row["high"] - row["ema8"]) < 1.1 * row["atr14"]):
        return False

    # 16. Small range
    if not ((row["high"] - row["low"]) < 1.1 * row["atr14"]):
        return False

    # 17. Had close below 12 EMA in last 15 bars
    had_below_12 = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema12_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            had_below_12 = True
            break
    if not had_below_12:
        return False

    # 18. Not too far from high
    if pd.isna(row["maxh30"]) or not ((row["maxh30"] - row["close"]) < 4.0 * row["atr14"]):
        return False

    return True


def run_backtest():
    """Run full 5-year backtest across tradable universe."""
    db = get_db()
    init_backtest_tables(db)

    # Get tradable tickers
    tradable_check = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tradable_universe'"
    ).fetchone()[0]

    if tradable_check:
        tickers = [r[0] for r in db.execute(
            "SELECT ticker FROM tradable_universe ORDER BY ticker"
        ).fetchall()]
    else:
        tickers = [r[0] for r in db.execute(
            "SELECT DISTINCT ticker FROM universe_ohlcv ORDER BY ticker"
        ).fetchall()]

    total = len(tickers)
    log.info(f"Starting full backtest: {total} tickers")

    # Clear previous results
    db.execute("DELETE FROM scan_backtest")
    db.commit()

    update_status(db, state="running", phase="scanning",
                  started_at=datetime.utcnow().isoformat(),
                  total_tickers=total, completed_tickers=0, signals_found=0,
                  current_ticker="", error="")

    signals_total = 0
    batch_signals = []
    BATCH_SIZE = 500  # Commit every N signals

    for i, ticker in enumerate(tickers):
        try:
            rows = db.execute(
                "SELECT date, open, high, low, close, volume FROM universe_ohlcv "
                "WHERE ticker = ? ORDER BY date",
                (ticker,)
            ).fetchall()

            if len(rows) < 250:  # Need 200+ for SMA200 + some buffer
                continue

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
            df = compute_indicators(df)

            # Scan every day from bar 250 onward
            for idx in range(250, len(df)):
                row = df.iloc[idx]
                if check_conditions(row, idx, df):
                    batch_signals.append((
                        ticker,
                        row["date"],
                        round(float(row["close"]), 2),
                        round(float(row["atr14"]), 2),
                        int(row["volume"]),
                        int(row["avgv20"]),
                        round(float((row["close"] - row["sma50"]) / row["sma50"] * 100), 1),
                        round(float((row["close"] - row["sma200"]) / row["sma200"] * 100), 1),
                        round(float((row["close"] - row["minl10"]) / (row["maxh30"] - row["minl10"]) * 100), 1)
                            if (row["maxh30"] - row["minl10"]) > 0 else 0,
                    ))

            # Flush batch
            if len(batch_signals) >= BATCH_SIZE:
                db.executemany(
                    "INSERT OR IGNORE INTO scan_backtest "
                    "(ticker, date, close, atr14, volume, avgv20, pct_above_sma50, pct_above_sma200, retracement) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    batch_signals
                )
                db.commit()
                signals_total += len(batch_signals)
                batch_signals = []

        except Exception as e:
            log.warning(f"Error scanning {ticker}: {e}")
            continue

        # Update status every 100 tickers
        if (i + 1) % 100 == 0:
            update_status(db, completed_tickers=i + 1, signals_found=signals_total + len(batch_signals),
                          current_ticker=ticker)
            log.info(f"  Progress: {i+1}/{total} tickers, {signals_total + len(batch_signals)} signals")

    # Flush remaining
    if batch_signals:
        db.executemany(
            "INSERT OR IGNORE INTO scan_backtest "
            "(ticker, date, close, atr14, volume, avgv20, pct_above_sma50, pct_above_sma200, retracement) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            batch_signals
        )
        db.commit()
        signals_total += len(batch_signals)

    log.info(f"Scan complete: {signals_total} raw signals from {total} tickers")
    update_status(db, state="running", phase="filtering",
                  completed_tickers=total, signals_found=signals_total,
                  current_ticker="filtering...")

    # ── Phase 2: Filter ──
    try:
        filter_results(db)
    except Exception as e:
        log.error(f"Filtering failed: {e}")
        traceback.print_exc()
        update_status(db, state="error", phase="filter_error", error=str(e))
        db.close()
        return

    final_count = db.execute("SELECT COUNT(*) FROM scan_backtest_clean").fetchone()[0]
    raw_count = db.execute("SELECT COUNT(*) FROM scan_backtest").fetchone()[0]
    unique_tickers_raw = db.execute("SELECT COUNT(DISTINCT ticker) FROM scan_backtest").fetchone()[0]
    unique_tickers_clean = db.execute("SELECT COUNT(DISTINCT ticker) FROM scan_backtest_clean").fetchone()[0]

    log.info(f"DONE: {raw_count} raw → {final_count} clean signals "
             f"({unique_tickers_raw} → {unique_tickers_clean} unique tickers)")

    update_status(db, state="complete", phase="done",
                  signals_found=final_count,
                  current_ticker=f"raw={raw_count}, clean={final_count}")
    db.close()


def filter_results(db):
    """Post-process: remove biotech, leveraged ETFs, and duplicate instruments."""
    log.info("Filtering results...")

    # Get all unique tickers from backtest results
    tickers = [r[0] for r in db.execute(
        "SELECT DISTINCT ticker FROM scan_backtest"
    ).fetchall()]
    log.info(f"  {len(tickers)} unique tickers in raw results")

    # Step 1: Remove known leveraged/inverse ETFs by ticker
    leveraged_set = LEVERAGED_INVERSE_PATTERNS
    remaining = [t for t in tickers if t not in leveraged_set]
    removed_leveraged = len(tickers) - len(remaining)
    log.info(f"  Removed {removed_leveraged} known leveraged/inverse tickers")

    # Step 2: Fetch sector/industry info for remaining tickers that we don't have yet
    already_fetched = set(r[0] for r in db.execute(
        "SELECT ticker FROM ticker_sectors"
    ).fetchall())
    need_fetch = [t for t in remaining if t not in already_fetched]

    if need_fetch:
        log.info(f"  Fetching sector info for {len(need_fetch)} tickers...")
        fetch_sector_info(db, need_fetch)

    # Step 3: Identify biotech tickers
    biotech_tickers = set()
    rows = db.execute(
        "SELECT ticker, sector, industry, is_etf, name FROM ticker_sectors"
    ).fetchall()

    sector_map = {}
    for r in rows:
        sector_map[r[0]] = {
            "sector": (r[1] or "").lower(),
            "industry": (r[2] or "").lower(),
            "is_etf": r[3],
            "name": (r[4] or "").lower(),
        }

    for ticker in remaining:
        info = sector_map.get(ticker, {})
        industry = info.get("industry", "")
        name = info.get("name", "")

        # Check if biotech
        if any(bio in industry for bio in BIOTECH_INDUSTRIES):
            biotech_tickers.add(ticker)
            continue

        # Check if ETF/leveraged by name patterns
        if info.get("is_etf"):
            name_lower = name
            if any(p in name_lower for p in ETF_INDICATOR_PATTERNS):
                biotech_tickers.add(ticker)  # reusing set for removal
                continue

    remaining_after_bio = [t for t in remaining if t not in biotech_tickers]
    log.info(f"  Removed {len(remaining) - len(remaining_after_bio)} biotech/leveraged-by-name tickers")

    # Step 4: Remove duplicate ETFs (leveraged versions of the same underlying)
    # Group remaining ETFs and look for duplicates
    etf_tickers = set()
    for ticker in remaining_after_bio:
        info = sector_map.get(ticker, {})
        if info.get("is_etf"):
            etf_tickers.add(ticker)

    # For ETFs, keep only the most liquid one if multiple track similar things
    # Simple heuristic: remove any ETF that's not in the original universe file
    # (the user's curated tradable list should be the source of truth)
    remaining_final = remaining_after_bio
    log.info(f"  Final: {len(remaining_final)} tickers remain after all filters")

    # Step 5: Copy clean results
    db.execute("DELETE FROM scan_backtest_clean")
    if remaining_final:
        placeholders = ",".join("?" * len(remaining_final))
        db.execute(f"""
            INSERT INTO scan_backtest_clean (ticker, date, close, atr14, volume, avgv20,
                pct_above_sma50, pct_above_sma200, retracement)
            SELECT ticker, date, close, atr14, volume, avgv20,
                pct_above_sma50, pct_above_sma200, retracement
            FROM scan_backtest
            WHERE ticker IN ({placeholders})
        """, remaining_final)
    db.commit()


def fetch_sector_info(db, tickers, batch_size=50):
    """Fetch sector/industry info from yfinance in batches."""
    import yfinance as yf

    total = len(tickers)
    now_iso = datetime.utcnow().isoformat()

    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]
        for ticker in batch:
            try:
                t = yf.Ticker(ticker)
                info = t.info or {}
                sector = info.get("sector", "")
                industry = info.get("industry", "")
                name = info.get("shortName", "") or info.get("longName", "")
                quote_type = info.get("quoteType", "")
                is_etf = 1 if quote_type == "ETF" else 0

                db.execute(
                    "INSERT OR REPLACE INTO ticker_sectors (ticker, sector, industry, name, is_etf, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ticker, sector, industry, name, is_etf, now_iso)
                )
            except Exception:
                # If we can't fetch info, store empty — don't block
                db.execute(
                    "INSERT OR REPLACE INTO ticker_sectors (ticker, sector, industry, name, is_etf, fetched_at) "
                    "VALUES (?, '', '', '', 0, ?)",
                    (ticker, now_iso)
                )

        db.commit()
        if i + batch_size < total:
            log.info(f"    Sector info: {min(i + batch_size, total)}/{total}")
            time.sleep(1)  # Rate limit


if __name__ == "__main__":
    run_backtest()
