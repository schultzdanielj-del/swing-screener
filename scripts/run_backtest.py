"""
3-4DB Full Backtest — Run 18 PCF conditions across ALL universe history.

Scans every tradable ticker × every trading day (with enough history)
and stores every signal in the scan_results table.

Then runs filtering to remove biotech, leveraged ETFs, and duplicates.

Usage:
    POST /api/backtest/run   (triggers via API)
    python scripts/run_backtest.py  (direct)
"""

import os
import sqlite3
import time
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backtest")

# ── Global state for progress tracking ──
backtest_state = {
    "running": False,
    "phase": "idle",
    "total_tickers": 0,
    "scanned_tickers": 0,
    "total_signals": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_backtest_tables(conn):
    """Create scan_results and scan_results_clean tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            close REAL,
            atr14 REAL,
            volume INTEGER,
            avgv20 REAL,
            pct_above_sma50 REAL,
            pct_above_sma200 REAL,
            retracement REAL,
            peak_ext_pct REAL,
            UNIQUE(ticker, signal_date)
        );
        CREATE INDEX IF NOT EXISTS idx_scan_ticker ON scan_results(ticker);
        CREATE INDEX IF NOT EXISTS idx_scan_date ON scan_results(signal_date);

        CREATE TABLE IF NOT EXISTS scan_results_clean (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            close REAL,
            atr14 REAL,
            volume INTEGER,
            avgv20 REAL,
            pct_above_sma50 REAL,
            pct_above_sma200 REAL,
            retracement REAL,
            peak_ext_pct REAL,
            sector TEXT,
            industry TEXT,
            UNIQUE(ticker, signal_date)
        );
        CREATE INDEX IF NOT EXISTS idx_clean_ticker ON scan_results_clean(ticker);
        CREATE INDEX IF NOT EXISTS idx_clean_date ON scan_results_clean(signal_date);

        CREATE TABLE IF NOT EXISTS ticker_info (
            ticker TEXT PRIMARY KEY,
            sector TEXT,
            industry TEXT,
            name TEXT,
            is_etf INTEGER DEFAULT 0,
            is_leveraged INTEGER DEFAULT 0,
            is_inverse INTEGER DEFAULT 0,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS backtest_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT DEFAULT 'idle',
            phase TEXT DEFAULT '',
            total_tickers INTEGER DEFAULT 0,
            scanned_tickers INTEGER DEFAULT 0,
            total_signals INTEGER DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            error TEXT
        );
        INSERT OR IGNORE INTO backtest_status (id) VALUES (1);
    """)
    conn.commit()


def update_status(conn, **kwargs):
    """Update backtest_status row."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    conn.execute(f"UPDATE backtest_status SET {sets} WHERE id = 1", vals)
    conn.commit()
    # Also update in-memory state
    backtest_state.update(kwargs)


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


def check_all_conditions(df, idx):
    """Check all 18 conditions for row at idx. Returns (pass, signal_dict or None)."""
    if idx < 220:
        return False, None

    row = df.iloc[idx]

    if pd.isna(row["sma50"]) or pd.isna(row["sma200"]) or pd.isna(row["atr14"]):
        return False, None
    if row["atr14"] <= 0:
        return False, None

    # 1. Close > SMA50
    if not (row["close"] > row["sma50"]):
        return False, None
    # 2. SMA50 rising
    if pd.isna(row["sma50_5ago"]) or not (row["sma50"] > row["sma50_5ago"]):
        return False, None
    # 3. Close >= 0.3 ATR above SMA50
    if not ((row["close"] - row["sma50"]) > 0.3 * row["atr14"]):
        return False, None
    # 4. 2 of last 3 days up
    up = 0
    for lag in [1, 2, 3]:
        c_lag = row.get(f"c{lag}")
        c_lag_next = row.get(f"c{lag+1}") if lag < 4 else None
        if lag == 3:
            c_lag_next = row.get("c4")
        if pd.notna(c_lag) and pd.notna(c_lag_next) and c_lag > c_lag_next:
            up += 1
    if up < 2:
        return False, None
    # 5. MINL3 < MINL2
    if pd.isna(row["minl3"]) or pd.isna(row["minl2"]) or not (row["minl3"] < row["minl2"]):
        return False, None
    # 6. Close >= 0.8 ATR above 10d low
    if pd.isna(row["minl10"]) or not ((row["close"] - row["minl10"]) > 0.8 * row["atr14"]):
        return False, None
    # 7. Price >= 1.0 ATR below 15d high
    if pd.isna(row["maxh15"]) or not ((row["maxh15"] - row["close"]) > 1.0 * row["atr14"]):
        return False, None
    # 8. Had close below 8 EMA in last 15 bars
    below_ema8 = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema8_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            below_ema8 = True
            break
    if not below_ema8:
        return False, None
    # 9. MAXH15.2 > MAXH2
    if pd.isna(row["maxh15_2ago"]) or pd.isna(row["maxh2"]) or not (row["maxh15_2ago"] > row["maxh2"]):
        return False, None
    # 10. Dollar volume > 5M
    if pd.isna(row["avgc20"]) or pd.isna(row["avgv20"]) or not ((row["avgc20"] * row["avgv20"]) > 5_000_000):
        return False, None
    # 11. Extended above SMA200
    if not ((row["close"] - row["sma200"]) > 3 * row["atr14"]):
        return False, None
    # 12. Peak ext 50 in 30d
    if pd.isna(row["maxh30"]) or not ((row["maxh30"] - row["sma50"]) > 0.30 * row["close"]):
        return False, None
    # 13. Retracement cap
    denom = row["maxh30"] - row["minl10"] if pd.notna(row["maxh30"]) and pd.notna(row["minl10"]) else 0
    if denom <= 0 or not ((row["close"] - row["minl10"]) / denom < 0.7):
        return False, None
    # 14. Volume < AVGV20
    if pd.isna(row["avgv20"]) or not (row["volume"] < row["avgv20"]):
        return False, None
    # 15. High near EMA8
    if not ((row["high"] - row["ema8"]) < 1.1 * row["atr14"]):
        return False, None
    # 16. Small range
    if not ((row["high"] - row["low"]) < 1.1 * row["atr14"]):
        return False, None
    # 17. Had close below 12 EMA in last 15 bars
    below_ema12 = False
    for lag in range(1, 16):
        c = row.get(f"c{lag}")
        ema = row.get(f"ema12_{lag}ago")
        if pd.notna(c) and pd.notna(ema) and c < ema:
            below_ema12 = True
            break
    if not below_ema12:
        return False, None
    # 18. Not too far from high
    if pd.isna(row["maxh30"]) or not ((row["maxh30"] - row["close"]) < 4.0 * row["atr14"]):
        return False, None

    # All 18 pass — build signal
    peak_ext = (row["maxh30"] - row["sma50"]) / row["close"] * 100 if row["close"] > 0 else 0
    retrace = ((row["close"] - row["minl10"]) / denom * 100) if denom > 0 else 0

    return True, {
        "ticker": None,  # filled by caller
        "signal_date": row["date"],
        "close": round(float(row["close"]), 2),
        "atr14": round(float(row["atr14"]), 2),
        "volume": int(row["volume"]),
        "avgv20": round(float(row["avgv20"]), 0),
        "pct_above_sma50": round((row["close"] - row["sma50"]) / row["sma50"] * 100, 1),
        "pct_above_sma200": round((row["close"] - row["sma200"]) / row["sma200"] * 100, 1),
        "retracement": round(retrace, 1),
        "peak_ext_pct": round(peak_ext, 1),
    }


def run_full_backtest():
    """Scan all tradable tickers across full history. Store results in scan_results."""
    conn = get_db()
    init_backtest_tables(conn)

    update_status(conn, state="running", phase="loading_tickers",
                  started_at=datetime.utcnow().isoformat(), finished_at=None, error=None,
                  scanned_tickers=0, total_signals=0)

    # Get tradable tickers
    tradable_exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tradable_universe'"
    ).fetchone()[0]

    if tradable_exists:
        tickers = [r[0] for r in conn.execute(
            "SELECT ticker FROM tradable_universe ORDER BY ticker"
        ).fetchall()]
    else:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM universe_ohlcv ORDER BY ticker"
        ).fetchall()]

    total = len(tickers)
    update_status(conn, phase="scanning", total_tickers=total)
    log.info(f"Starting backtest: {total} tickers")

    # Clear old results
    conn.execute("DELETE FROM scan_results")
    conn.commit()

    batch_signals = []
    BATCH_SIZE = 500

    try:
        for i, ticker in enumerate(tickers):
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume FROM universe_ohlcv "
                "WHERE ticker = ? ORDER BY date",
                (ticker,)
            ).fetchall()

            if len(rows) < 250:
                if (i + 1) % 500 == 0:
                    update_status(conn, scanned_tickers=i + 1)
                continue

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
            df = compute_indicators(df)

            for idx in range(220, len(df)):
                passed, sig = check_all_conditions(df, idx)
                if passed:
                    sig["ticker"] = ticker
                    batch_signals.append(sig)

            # Batch insert every BATCH_SIZE tickers
            if (i + 1) % BATCH_SIZE == 0 or i == total - 1:
                if batch_signals:
                    conn.executemany(
                        "INSERT OR IGNORE INTO scan_results "
                        "(ticker, signal_date, close, atr14, volume, avgv20, "
                        "pct_above_sma50, pct_above_sma200, retracement, peak_ext_pct) "
                        "VALUES (:ticker, :signal_date, :close, :atr14, :volume, :avgv20, "
                        ":pct_above_sma50, :pct_above_sma200, :retracement, :peak_ext_pct)",
                        batch_signals
                    )
                    conn.commit()
                    batch_signals = []

                total_sigs = conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
                update_status(conn, scanned_tickers=i + 1, total_signals=total_sigs)
                log.info(f"  {i+1}/{total} tickers, {total_sigs} signals so far")

        total_sigs = conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
        unique_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM scan_results").fetchone()[0]
        log.info(f"Scan complete: {total_sigs} signals from {unique_tickers} unique tickers")

        # Now run filtering
        update_status(conn, phase="filtering", total_signals=total_sigs)
        filter_results(conn)

        final_clean = conn.execute("SELECT COUNT(*) FROM scan_results_clean").fetchone()[0]
        update_status(conn, state="complete", phase="done",
                      finished_at=datetime.utcnow().isoformat(), total_signals=total_sigs)
        log.info(f"Filtering complete: {final_clean} clean signals")

    except Exception as e:
        log.error(f"Backtest failed: {e}\n{traceback.format_exc()}")
        update_status(conn, state="error", error=str(e),
                      finished_at=datetime.utcnow().isoformat())
    finally:
        conn.close()


# ── Leveraged / Inverse / ETF detection ──

LEVERAGED_KEYWORDS = [
    "2x", "3x", "-2x", "-3x", "ultra", "ultrapro", "ultrashort",
    "direxion", "proshares", "leveraged", "inverse", "bear", "bull",
    "daily", "double", "triple",
]

KNOWN_LEVERAGED_PREFIXES = [
    "SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXS", "SPXL",
    "QLD", "QID", "SSO", "SDS", "UDOW", "SDOW", "URTY", "SRTY",
    "TNA", "TZA", "FAS", "FAZ", "LABU", "LABD", "NUGT", "DUST",
    "JNUG", "JDST", "ERX", "ERY", "GUSH", "DRIP", "NAIL", "DRV",
    "DPST", "WEBL", "WEBS", "TECL", "TECS", "CURE", "UVXY", "SVXY",
    "VIXY", "VXX", "UVIX", "SVIX", "BITX", "BITU", "CONL",
    "FNGU", "FNGD", "BULZ", "BERZ", "HIBL", "HIBS",
    "MSTU", "MSTZ", "NVDL", "NVDS", "NVDU", "NVDD",
    "TSLL", "TSLS", "TSLQ", "TSLR", "TSLT", "TSLZ",
    "AMZU", "AMZD", "MSFU", "MSFD", "GGLL", "GGLS",
    "AAPD", "AAPU", "AMDL", "AMDS",
]

BIOTECH_INDUSTRIES = [
    "biotechnology", "biotech", "pharmaceutical preparations",
    "biological products", "pharmaceutical", "drug manufacturers",
    "diagnostics & research", "medical instruments",
]


def classify_ticker_batch(tickers, conn):
    """Fetch sector/industry for tickers not already in ticker_info. Uses yfinance."""
    import yfinance as yf

    # Check which tickers we already have info for
    existing = set()
    for row in conn.execute("SELECT ticker FROM ticker_info").fetchall():
        existing.add(row[0])

    need_fetch = [t for t in tickers if t not in existing]
    if not need_fetch:
        return

    log.info(f"Fetching sector/industry for {len(need_fetch)} tickers...")

    # Batch fetch in chunks
    CHUNK = 50
    for ci in range(0, len(need_fetch), CHUNK):
        chunk = need_fetch[ci:ci + CHUNK]
        batch_data = []

        for ticker in chunk:
            try:
                info = yf.Ticker(ticker).info
                sector = info.get("sector", "")
                industry = info.get("industry", "")
                name = info.get("shortName", info.get("longName", ""))
                quote_type = info.get("quoteType", "")

                is_etf = 1 if quote_type == "ETF" else 0
                is_leveraged = 0
                is_inverse = 0

                # Check name for leveraged/inverse keywords
                name_lower = (name or "").lower()
                if any(kw in name_lower for kw in ["2x", "3x", "ultra", "leveraged", "double", "triple"]):
                    is_leveraged = 1
                if any(kw in name_lower for kw in ["inverse", "short", "bear", "-1x", "-2x", "-3x"]):
                    is_inverse = 1
                if ticker in KNOWN_LEVERAGED_PREFIXES:
                    is_leveraged = 1

                batch_data.append((ticker, sector, industry, name, is_etf, is_leveraged, is_inverse,
                                   datetime.utcnow().isoformat()))
            except Exception:
                batch_data.append((ticker, "", "", "", 0, 0, 0, datetime.utcnow().isoformat()))

        conn.executemany(
            "INSERT OR REPLACE INTO ticker_info "
            "(ticker, sector, industry, name, is_etf, is_leveraged, is_inverse, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch_data
        )
        conn.commit()

        if ci + CHUNK < len(need_fetch):
            log.info(f"  Fetched info: {ci + len(chunk)}/{len(need_fetch)}")
            time.sleep(1)  # Rate limit


def filter_results(conn):
    """Filter scan_results → scan_results_clean.
    Removes: biotech, leveraged/inverse ETFs, duplicate underlying exposure.
    """
    # Get unique tickers from scan_results
    signal_tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM scan_results"
    ).fetchall()]

    if not signal_tickers:
        log.info("No signals to filter")
        return

    log.info(f"Filtering {len(signal_tickers)} unique signal tickers...")

    # Fetch sector/industry info
    classify_ticker_batch(signal_tickers, conn)

    # Clear old clean results
    conn.execute("DELETE FROM scan_results_clean")

    # Build exclusion sets
    # 1) Biotech tickers
    biotech_tickers = set()
    for row in conn.execute("SELECT ticker, industry FROM ticker_info").fetchall():
        ind = (row[1] or "").lower()
        if any(bio in ind for bio in BIOTECH_INDUSTRIES):
            biotech_tickers.add(row[0])

    # 2) Leveraged / inverse tickers
    leveraged_tickers = set()
    for row in conn.execute(
        "SELECT ticker FROM ticker_info WHERE is_leveraged = 1 OR is_inverse = 1"
    ).fetchall():
        leveraged_tickers.add(row[0])

    # Also add known leveraged from hardcoded list
    leveraged_tickers.update(KNOWN_LEVERAGED_PREFIXES)

    # 3) ETF duplicates — for ETFs tracking the same thing, keep the most liquid one
    # Simple approach: if multiple ETFs have very similar daily returns correlation,
    # they're duplicates. But that's expensive. Instead, just flag all leveraged/inverse
    # and let the rest through — most non-leveraged ETFs track different things.

    exclude = biotech_tickers | leveraged_tickers
    log.info(f"Excluding: {len(biotech_tickers)} biotech, {len(leveraged_tickers)} leveraged/inverse")

    # Copy passing signals to clean table
    conn.execute(f"""
        INSERT INTO scan_results_clean
        (ticker, signal_date, close, atr14, volume, avgv20,
         pct_above_sma50, pct_above_sma200, retracement, peak_ext_pct, sector, industry)
        SELECT
            sr.ticker, sr.signal_date, sr.close, sr.atr14, sr.volume, sr.avgv20,
            sr.pct_above_sma50, sr.pct_above_sma200, sr.retracement, sr.peak_ext_pct,
            ti.sector, ti.industry
        FROM scan_results sr
        LEFT JOIN ticker_info ti ON sr.ticker = ti.ticker
        WHERE sr.ticker NOT IN ({','.join('?' for _ in exclude)})
    """, list(exclude))
    conn.commit()

    clean_count = conn.execute("SELECT COUNT(*) FROM scan_results_clean").fetchone()[0]
    clean_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM scan_results_clean").fetchone()[0]
    log.info(f"Clean results: {clean_count} signals from {clean_tickers} tickers")


if __name__ == "__main__":
    run_full_backtest()
