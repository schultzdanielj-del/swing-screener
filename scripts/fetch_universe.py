"""
Universe OHLCV Data Fetcher — Bulletproof Edition

Pulls 5 years of daily OHLCV data for all NYSE + NASDAQ tickers
and stores it in the SQLite database.

Features:
- Auto-fetches full ticker list from NASDAQ
- Resumable: tracks completed tickers, picks up where it left off
- Batch downloads via yfinance for speed
- Auto-retry with exponential backoff on failures
- Chunked DB commits (never loses more than one batch)
- Progress tracking via DB table (queryable via API)
- Timeout protection per batch
- Filters out warrants, units, preferred, test issues, OTC

Usage:
    # Triggered via API endpoint POST /api/universe/fetch
    # Or run directly: python scripts/fetch_universe.py
"""

import os
import sqlite3
import csv
import io
import json
import time
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BATCH_SIZE = 40          # tickers per yfinance batch call (conservative)
BATCH_DELAY = 8.0        # seconds between batches — spread over ~3 hours
RETRY_MAX = 3            # retries per failed batch
RETRY_BACKOFF = 10.0     # base seconds for exponential backoff
YEARS = 5                # how many years of history
DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("universe")


def get_db_path():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def get_db():
    path = get_db_path()
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_universe_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS universe_ohlcv (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_universe_ticker ON universe_ohlcv(ticker);
        CREATE INDEX IF NOT EXISTS idx_universe_date ON universe_ohlcv(date);

        CREATE TABLE IF NOT EXISTS universe_fetch_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT DEFAULT 'idle',
            started_at TEXT,
            updated_at TEXT,
            total_tickers INTEGER DEFAULT 0,
            completed_tickers INTEGER DEFAULT 0,
            failed_tickers INTEGER DEFAULT 0,
            skipped_tickers INTEGER DEFAULT 0,
            current_batch TEXT,
            errors TEXT DEFAULT '[]',
            completed_list TEXT DEFAULT '[]'
        );
        INSERT OR IGNORE INTO universe_fetch_status (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS universe_tickers (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            exchange TEXT,
            etf INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            rows_stored INTEGER DEFAULT 0,
            fetched_at TEXT
        );
    """)
    db.commit()


# ---------------------------------------------------------------------------
# Ticker list fetching
# ---------------------------------------------------------------------------
def fetch_ticker_list():
    """Fetch all NYSE + NASDAQ traded tickers from NASDAQ FTP."""
    url = "https://www.nasdaqtrader.com/dynamic/SymInfo/nasdaqtraded.txt"
    log.info(f"Fetching ticker list from {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read().decode()
    lines = data.strip().split("\n")

    reader = csv.DictReader(lines, delimiter="|")
    tickers = []
    for row in reader:
        sym = row.get("Symbol", "").strip()
        test = row.get("Test Issue", "").strip()
        etf = row.get("ETF", "").strip()
        name = row.get("Security Name", "").strip()
        exchange = row.get("Listing Exchange", "").strip()

        # Skip empty, test issues
        if not sym or test == "Y":
            continue
        # Skip warrants, units, preferred, rights, weird tickers
        if any(c in sym for c in [" ", ".", "$", "-", "/", "^"]):
            continue
        # Skip tickers > 5 chars (usually warrants/units)
        if len(sym) > 5:
            continue
        # Skip if "warrant" or "unit" or "right" in name
        name_lower = name.lower()
        if any(w in name_lower for w in ["warrant", "unit ", "units ", "right ", "rights "]):
            continue

        tickers.append({
            "ticker": sym,
            "name": name[:200],
            "exchange": exchange,
            "etf": 1 if etf == "Y" else 0,
        })

    log.info(f"Found {len(tickers)} tradeable tickers")
    return tickers


def fetch_ticker_list_fallback():
    """Fallback: read from bundled Universe.txt file."""
    # Try multiple paths (Railway deploy vs local dev)
    for path in [
        Path("/app/data/universe_tickers.txt"),
        Path("data/universe_tickers.txt"),
        Path(__file__).parent.parent / "data" / "universe_tickers.txt",
    ]:
        if path.exists():
            log.info(f"Using bundled ticker file: {path}")
            lines = path.read_text().replace("\r", "").strip().split("\n")
            tickers = []
            for line in lines:
                sym = line.strip().upper()
                if not sym:
                    continue
                tickers.append({
                    "ticker": sym,
                    "name": "",
                    "exchange": "",
                    "etf": 0,
                })
            log.info(f"Loaded {len(tickers)} tickers from file")
            return tickers

    raise RuntimeError("No ticker list available. Upload via POST /api/universe/tickers")


def load_or_fetch_tickers(db):
    """Load tickers from DB if already fetched, otherwise fetch fresh."""
    existing = db.execute("SELECT COUNT(*) FROM universe_tickers").fetchone()[0]
    if existing > 0:
        log.info(f"Using {existing} tickers already in DB")
        return

    try:
        ticker_list = fetch_ticker_list()
    except Exception as e:
        log.error(f"Failed to fetch ticker list: {e}")
        ticker_list = fetch_ticker_list_fallback()

    db.executemany(
        "INSERT OR IGNORE INTO universe_tickers (ticker, name, exchange, etf) VALUES (?,?,?,?)",
        [(t["ticker"], t["name"], t["exchange"], t["etf"]) for t in ticker_list]
    )
    db.commit()
    log.info(f"Stored {len(ticker_list)} tickers in universe_tickers")


# ---------------------------------------------------------------------------
# OHLCV fetching
# ---------------------------------------------------------------------------
def update_status(db, **kwargs):
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values())
    db.execute(f"UPDATE universe_fetch_status SET {sets} WHERE id=1", vals)
    db.commit()


def fetch_batch(tickers, start_date, end_date, attempt=1):
    """Download OHLCV for a batch of tickers. Returns dict of {ticker: DataFrame}."""
    try:
        raw = yf.download(
            tickers,
            start=start_date,
            end=end_date,
            progress=False,
            group_by="ticker",
            threads=True,
            timeout=60,
        )
        if raw is None or raw.empty:
            return {}

        results = {}
        if len(tickers) == 1:
            # Single ticker: no multi-level columns
            t = tickers[0]
            df = raw.copy()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(0)
            df = df.dropna(subset=["Close"])
            if len(df) > 0:
                results[t] = df
        else:
            # Multi-ticker: grouped columns
            for t in tickers:
                try:
                    if t in raw.columns.get_level_values(0):
                        df = raw[t].copy()
                        df = df.dropna(subset=["Close"])
                        if len(df) > 0:
                            results[t] = df
                except Exception:
                    continue
        return results

    except Exception as e:
        if attempt < RETRY_MAX:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning(f"Batch failed (attempt {attempt}): {e}. Retrying in {wait}s...")
            time.sleep(wait)
            return fetch_batch(tickers, start_date, end_date, attempt + 1)
        else:
            log.error(f"Batch failed after {RETRY_MAX} attempts: {e}")
            return {}


def store_ohlcv_batch(db, ticker, df):
    """Store OHLCV rows for a single ticker."""
    rows = []
    for idx, row in df.iterrows():
        dt = idx
        if hasattr(dt, "strftime"):
            dt = dt.strftime("%Y-%m-%d")
        else:
            dt = str(dt)[:10]

        def safe(v):
            try:
                v = float(v)
                return v if pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        rows.append((
            ticker, dt,
            safe(row.get("Open")),
            safe(row.get("High")),
            safe(row.get("Low")),
            safe(row.get("Close")),
            safe(row.get("Volume")),
        ))

    if rows:
        db.executemany(
            "INSERT OR REPLACE INTO universe_ohlcv (ticker, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            rows
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Main fetch orchestrator
# ---------------------------------------------------------------------------
def run_full_fetch():
    """Main entry point — fetches everything, resumable, bulletproof."""
    db = get_db()
    init_universe_tables(db)

    # Load ticker list
    load_or_fetch_tickers(db)

    # Get pending tickers (not yet fetched or failed)
    pending = [r["ticker"] for r in db.execute(
        "SELECT ticker FROM universe_tickers WHERE status IN ('pending', 'failed') ORDER BY ticker"
    ).fetchall()]

    total_in_db = db.execute("SELECT COUNT(*) FROM universe_tickers").fetchone()[0]
    already_done = total_in_db - len(pending)

    if not pending:
        log.info("All tickers already fetched!")
        update_status(db, state="complete",
                      total_tickers=total_in_db,
                      completed_tickers=total_in_db)
        db.close()
        return

    log.info(f"Starting fetch: {len(pending)} pending, {already_done} already done, {total_in_db} total")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=YEARS * 365 + 30)).strftime("%Y-%m-%d")

    update_status(
        db,
        state="running",
        started_at=datetime.utcnow().isoformat(),
        total_tickers=total_in_db,
        completed_tickers=already_done,
        failed_tickers=0,
        skipped_tickers=0,
        current_batch="starting",
        errors="[]",
    )

    completed = already_done
    failed = 0
    skipped = 0
    errors = []

    # Process in batches
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
        pct = (completed / total_in_db) * 100

        log.info(f"Batch {batch_num}/{total_batches} ({pct:.1f}%) — {len(batch)} tickers: {batch[0]}...{batch[-1]}")

        update_status(db,
                      current_batch=f"Batch {batch_num}/{total_batches}: {batch[0]}-{batch[-1]}",
                      completed_tickers=completed,
                      failed_tickers=failed)

        # Fetch
        results = fetch_batch(batch, start_date, end_date)

        # Store results
        for ticker in batch:
            if ticker in results and len(results[ticker]) > 0:
                try:
                    n = store_ohlcv_batch(db, ticker, results[ticker])
                    db.execute(
                        "UPDATE universe_tickers SET status='done', rows_stored=?, fetched_at=? WHERE ticker=?",
                        (n, datetime.utcnow().isoformat(), ticker)
                    )
                    completed += 1
                except Exception as e:
                    log.error(f"  Store failed for {ticker}: {e}")
                    db.execute("UPDATE universe_tickers SET status='failed' WHERE ticker=?", (ticker,))
                    failed += 1
                    errors.append(f"{ticker}: {str(e)[:100]}")
            else:
                # No data returned — might be delisted, OTC, etc.
                db.execute("UPDATE universe_tickers SET status='skipped' WHERE ticker=?", (ticker,))
                skipped += 1

        # Commit after each batch — never lose more than one batch of work
        db.commit()

        # Update status
        update_status(db,
                      completed_tickers=completed,
                      failed_tickers=failed,
                      skipped_tickers=skipped,
                      errors=json.dumps(errors[-50:]))  # keep last 50 errors

        # Delay between batches
        if i + BATCH_SIZE < len(pending):
            time.sleep(BATCH_DELAY)

    # Done
    update_status(db,
                  state="complete",
                  current_batch="done",
                  completed_tickers=completed,
                  failed_tickers=failed,
                  skipped_tickers=skipped,
                  errors=json.dumps(errors[-50:]))

    log.info(f"COMPLETE: {completed} done, {failed} failed, {skipped} skipped out of {total_in_db}")
    db.close()


# ---------------------------------------------------------------------------
# Direct execution
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run_full_fetch()
