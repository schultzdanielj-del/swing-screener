"""ScanPerfect — FastAPI backend with SQLite storage."""

import os
import io
import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="ScanPerfect API")

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "scanperfect.db"

# Legacy flat files live in repo's data/ dir (may be shadowed by volume mount)
# On first deploy, copy them to volume so migration can read them
LEGACY_DATA_DIR = Path("/app/data_legacy") if Path("/app/data_legacy").exists() else Path("data")


# ============================================
# DATABASE
# ============================================

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                chart_date TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(setup_type, ticker, entry_date)
            );
            CREATE TABLE IF NOT EXISTS ohlcv (
                example_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(example_id, date)
            );
            CREATE TABLE IF NOT EXISTS extension (
                example_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                close REAL, sma50 REAL, sma200 REAL, atr14 REAL,
                ext_sma50_xatr REAL, ext_sma200_xatr REAL,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(example_id, date)
            );
            CREATE TABLE IF NOT EXISTS conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                pcf TEXT,
                active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS signal_analysis (
                example_id INTEGER NOT NULL UNIQUE,
                analysis_json TEXT NOT NULL,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ohlcv_example ON ohlcv(example_id);
            CREATE INDEX IF NOT EXISTS idx_extension_example ON extension(example_id);
            CREATE INDEX IF NOT EXISTS idx_examples_setup ON examples(setup_type);
            CREATE TABLE IF NOT EXISTS rejected_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(setup_type, ticker, signal_date)
            );
        """)

init_db()

# Migrate: change UNIQUE(setup_type, ticker) → UNIQUE(setup_type, ticker, entry_date)
def migrate_unique_constraint():
    with get_db() as db:
        table_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='examples'").fetchone()
        if table_sql and "UNIQUE(setup_type, ticker)" in table_sql[0] and "UNIQUE(setup_type, ticker, entry_date)" not in table_sql[0]:
            print("Migrating examples table: UNIQUE(setup_type, ticker) → UNIQUE(setup_type, ticker, entry_date)")
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS examples_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setup_type TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    chart_date TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(setup_type, ticker, entry_date)
                );
                INSERT INTO examples_new (id, setup_type, ticker, chart_date, entry_date, created_at)
                    SELECT id, setup_type, ticker, chart_date, entry_date, created_at FROM examples;
                DROP TABLE examples;
                ALTER TABLE examples_new RENAME TO examples;
                CREATE INDEX IF NOT EXISTS idx_examples_setup ON examples(setup_type);
            """)
            db.execute("PRAGMA foreign_keys = ON")
            print("Migration complete.")

migrate_unique_constraint()

# Migrate extension table: old schema had ext_sma50_pct/ext_sma200_pct, new uses xatr + atr14
with get_db() as db:
    cols = [r[1] for r in db.execute("PRAGMA table_info(extension)").fetchall()]
    if "ext_sma50_pct" in cols:
        print("Migrating extension table from pct to xATR schema...")
        db.executescript("""
            DROP TABLE IF EXISTS extension;
            CREATE TABLE extension (
                example_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                close REAL, sma50 REAL, sma200 REAL, atr14 REAL,
                ext_sma50_xatr REAL, ext_sma200_xatr REAL,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(example_id, date)
            );
            CREATE INDEX IF NOT EXISTS idx_extension_example ON extension(example_id);
        """)
        # Re-fetch extension data for all existing examples
        examples = db.execute("SELECT id, ticker FROM examples").fetchall()
        for ex in examples:
            print(f"  Re-fetching extension for {ex['ticker']}...")
            ext_df = fetch_extension(ex["ticker"])
            if ext_df is not None:
                store_extension(db, ex["id"], ext_df)
        print("Extension migration complete.")


# ============================================
# HELPERS
# ============================================

def clean_val(v):
    if v is None: return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
    if hasattr(v, 'item'): v = v.item()
    return v

def calc_ema(series, period):
    k = 2 / (period + 1)
    ema = [series.iloc[0]]
    for i in range(1, len(series)):
        ema.append(series.iloc[i] * k + ema[-1] * (1 - k))
    return pd.Series(ema, index=series.index)

def calc_sma(series, period):
    return series.rolling(window=period, min_periods=period).mean()

def calc_atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def add_indicators(df):
    df = df.copy()
    df["EMA8"] = calc_ema(df["Close"], 8)
    df["EMA21"] = calc_ema(df["Close"], 21)
    df["SMA50"] = calc_sma(df["Close"], 50)
    df["SMA200"] = calc_sma(df["Close"], 200)
    df["ATR14"] = calc_atr(df, 14)
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    return df

def normalize_ticker_for_yfinance(ticker):
    """Convert common ticker formats to yfinance-compatible symbols.
    e.g. BRK.B or BRKB -> BRK-B, MOG.A -> MOG-A, BF.B -> BF-B"""
    # Known multi-class tickers where letter suffix is a share class
    SHARE_CLASS_TICKERS = {
        "BRKA": "BRK-A", "BRKB": "BRK-B",
        "BRK.A": "BRK-A", "BRK.B": "BRK-B",
        "BF.A": "BF-A", "BF.B": "BF-B",
        "BFA": "BF-A", "BFB": "BF-B",
        "MOG.A": "MOG-A", "MOG.B": "MOG-B",
        "MOGA": "MOG-A", "MOGB": "MOG-B",
        "GEF.B": "GEF-B", "GEFB": "GEF-B",
        "LGF.A": "LGF-A", "LGF.B": "LGF-B",
        "LGFA": "LGF-A", "LGFB": "LGF-B",
    }
    up = ticker.upper().strip()
    if up in SHARE_CLASS_TICKERS:
        return SHARE_CLASS_TICKERS[up]
    # Generic: convert dots to hyphens for yfinance
    if "." in up:
        return up.replace(".", "-")
    return up

def fetch_ohlcv(ticker, chart_date_str):
    ticker = normalize_ticker_for_yfinance(ticker)
    chart_dt = datetime.strptime(chart_date_str, "%Y-%m-%d")
    start = chart_dt - timedelta(days=250)
    end = chart_dt + timedelta(days=60)
    raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), progress=False)
    if raw.empty: return None
    if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    raw["Date"] = pd.to_datetime(raw["Date"])
    return raw.sort_values("Date").reset_index(drop=True)

def fetch_extension(ticker):
    ticker = normalize_ticker_for_yfinance(ticker)
    end = datetime.now()
    start = end - timedelta(days=365 * 5 + 60)
    raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), progress=False)
    if raw.empty: return None
    if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    raw["Date"] = pd.to_datetime(raw["Date"])
    raw = raw.sort_values("Date").reset_index(drop=True)
    raw["SMA50"] = raw["Close"].rolling(50).mean()
    raw["SMA200"] = raw["Close"].rolling(200).mean()
    tr = pd.concat([raw["High"] - raw["Low"], (raw["High"] - raw["Close"].shift(1)).abs(), (raw["Low"] - raw["Close"].shift(1)).abs()], axis=1).max(axis=1)
    raw["ATR14"] = tr.rolling(14).mean()
    raw["ext_sma50_xatr"] = ((raw["Close"] - raw["SMA50"]) / raw["ATR14"]).round(2)
    raw["ext_sma200_xatr"] = ((raw["Close"] - raw["SMA200"]) / raw["ATR14"]).round(2)
    return raw

def store_ohlcv(db, example_id, df):
    rows = [(example_id, r["Date"].strftime("%Y-%m-%d"), clean_val(r["Open"]), clean_val(r["High"]),
             clean_val(r["Low"]), clean_val(r["Close"]), clean_val(r["Volume"])) for _, r in df.iterrows()]
    db.executemany("INSERT OR REPLACE INTO ohlcv (example_id, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)", rows)

def store_extension(db, example_id, df):
    rows = [(example_id, r["Date"].strftime("%Y-%m-%d"), clean_val(r["Close"]), clean_val(r.get("SMA50")),
             clean_val(r.get("SMA200")), clean_val(r.get("ATR14")), clean_val(r.get("ext_sma50_xatr")), clean_val(r.get("ext_sma200_xatr"))) for _, r in df.iterrows()]
    db.executemany("INSERT OR REPLACE INTO extension (example_id, date, close, sma50, sma200, atr14, ext_sma50_xatr, ext_sma200_xatr) VALUES (?,?,?,?,?,?,?,?)", rows)

def get_ohlcv_df(db, example_id):
    rows = db.execute("SELECT date as Date, open as Open, high as High, low as Low, close as Close, volume as Volume FROM ohlcv WHERE example_id=? ORDER BY date", (example_id,)).fetchall()
    if not rows: return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["Date"] = pd.to_datetime(df["Date"])
    return df

def get_extension_rows(db, example_id):
    return [dict(r) for r in db.execute("SELECT date, close, sma50, sma200, atr14, ext_sma50_xatr, ext_sma200_xatr FROM extension WHERE example_id=? ORDER BY date", (example_id,)).fetchall()]


# ============================================
# CHART GENERATION (returns PNG bytes, no files)
# ============================================

def generate_chart_png(df, ticker, entry_date, at_entry=False, setup_type=None):
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    df = add_indicators(df)

    # Setup-specific lookback config
    LOOKBACK = {"dtss": {"at_entry_before": 100, "default_before": 100, "default_after": 30, "min_total": 130}}
    cfg = LOOKBACK.get(setup_type, {})
    at_entry_before = cfg.get("at_entry_before", 50)
    default_before = cfg.get("default_before", 30)
    default_after = cfg.get("default_after", 30)
    min_total = cfg.get("min_total", 60)

    entry_dt = pd.Timestamp(entry_date)
    entry_rows = df[df["Date"] == entry_dt]
    if entry_rows.empty:
        before = df[df["Date"] <= entry_dt]
        if before.empty: return None
        entry_idx = before.index[-1]
    else:
        entry_idx = entry_rows.index[0]

    if at_entry:
        want_before = min(at_entry_before, entry_idx)
        start_idx = entry_idx - want_before
        chart_df = df.iloc[start_idx:entry_idx + 1].copy().reset_index(drop=True)
        entry_pos = want_before
        n = len(chart_df)
        total_width = n + max(int(n * 0.18), 5)
    else:
        avail_after = len(df) - entry_idx - 1
        avail_before = entry_idx
        want_after = min(default_after, avail_after)
        want_before = min(default_before, avail_before)
        total = want_before + 1 + want_after
        if total < min_total:
            extra = min_total - total
            if want_before < default_before: want_after = min(want_after + extra, avail_after)
            else: want_before = min(want_before + extra, avail_before)
        chart_df = df.iloc[entry_idx - want_before:entry_idx + want_after + 1].copy().reset_index(drop=True)
        entry_pos = want_before
        total_width = len(chart_df)

    if chart_df.empty: return None

    fig, (ax, ax_vol) = plt.subplots(2, 1, figsize=(8, 4), dpi=120, gridspec_kw={"height_ratios": [3, 1]}, facecolor="#0a0e17")
    ax.set_facecolor("#0a0e17")
    ax_vol.set_facecolor("#0a0e17")
    n = len(chart_df)
    w = 0.6

    for i, row in chart_df.iterrows():
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        color = "#26A69A" if c >= o else "#EF5350"
        ax.plot([i, i], [l, h], color=color, linewidth=0.8)
        ax.add_patch(Rectangle((i - w/2, min(o, c)), w, max(abs(c - o), 0.001), facecolor=color, edgecolor=color, linewidth=0.5))
        ax_vol.bar(i, row["Volume"], width=w, color=color, alpha=0.7)

    for period, ma_type, color, lw in [(8, "ema", "#ADD8E6", 1.0), (21, "ema", "#D2B48C", 1.0), (50, "sma", "#FFD700", 1.2), (200, "sma", "#FF0000", 1.5)]:
        if n >= period:
            s = chart_df["Close"].ewm(span=period, adjust=False).mean() if ma_type == "ema" else chart_df["Close"].rolling(window=period).mean()
            ax.plot(range(n), s.values, color=color, linewidth=lw, alpha=0.8)

    entry_open = float(chart_df.iloc[entry_pos]["Open"])
    ax.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax.axhline(y=entry_open, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax_vol.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax.set_title(f"{ticker}  •  {entry_date}", color="#e2e8f0", fontsize=11, fontweight="bold", pad=8)
    ax.tick_params(colors="#64748b", labelsize=8); ax_vol.tick_params(colors="#64748b", labelsize=7)
    ax.spines[:].set_color("#2a3550"); ax_vol.spines[:].set_color("#2a3550")
    ax.set_xlim(-1, total_width); ax_vol.set_xlim(-1, total_width)
    ax.set_xticks([]); ax_vol.set_xticks([]); ax_vol.yaxis.set_visible(False)
    ax.grid(True, alpha=0.1, color="#64748b")

    fig.tight_layout(pad=0.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="#0a0e17", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ============================================
# SIGNAL ANALYSIS
# ============================================

def run_signal_analysis(df, ticker, entry_date):
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    df = add_indicators(df)
    df["SMA10"] = calc_sma(df["Close"], 10)

    entry_dt = pd.Timestamp(entry_date)
    entry_idx = df.index[df["Date"] == entry_dt]
    if len(entry_idx) == 0:
        before = df[df["Date"] <= entry_dt]
        if before.empty: raise ValueError(f"No data at or before {entry_date}")
        entry_idx = [before.index[-1]]
    eidx = entry_idx[0]
    if eidx == 0: raise ValueError("Entry date is first row")

    sig_idx = eidx - 1
    sig = df.iloc[sig_idx]
    c, o, h, l = float(sig["Close"]), float(sig["Open"]), float(sig["High"]), float(sig["Low"])
    atr = float(sig["ATR14"]) if pd.notna(sig["ATR14"]) else None
    vol = float(sig["Volume"])
    vol_avg = float(sig["VolAvg20"]) if pd.notna(sig["VolAvg20"]) else None
    ema8 = float(sig["EMA8"]) if pd.notna(sig["EMA8"]) else None
    ema21 = float(sig["EMA21"]) if pd.notna(sig["EMA21"]) else None
    sma10 = float(sig["SMA10"]) if pd.notna(sig["SMA10"]) else None
    sma50 = float(sig["SMA50"]) if pd.notna(sig["SMA50"]) else None
    sma200 = float(sig["SMA200"]) if pd.notna(sig["SMA200"]) else None

    lb30 = df.iloc[max(0, sig_idx - 30):sig_idx + 1]
    swing_high = float(lb30["High"].max())
    high_idx = lb30["High"].idxmax()
    days_from_high = sig_idx - high_idx
    pullback_pct = round((swing_high - c) / swing_high * 100, 2)
    pullback_atr = round((swing_high - c) / atr, 2) if atr else None

    pb_range = df.iloc[high_idx:sig_idx + 1]
    pullback_low = float(pb_range["Low"].min())
    low_idx = pb_range["Low"].idxmin()
    days_since_low = sig_idx - low_idx
    bounce_pct = round((c - pullback_low) / pullback_low * 100, 2)
    bounce_atr = round((c - pullback_low) / atr, 2) if atr else None

    r5 = df.iloc[max(0, sig_idx - 4):sig_idx + 1]
    r3 = df.iloc[max(0, sig_idx - 2):sig_idx + 1]
    sig_body, sig_range = abs(c - o), h - l

    sma50_slope = None
    if sig_idx >= 5 and pd.notna(sig["SMA50"]):
        s5 = df.iloc[sig_idx - 5]["SMA50"]
        if pd.notna(s5) and s5 > 0: sma50_slope = round((float(sig["SMA50"]) - float(s5)) / float(s5) * 100, 3)

    down_days = int((pb_range["Close"] < pb_range["Open"]).sum())
    r20 = df.iloc[max(0, sig_idx - 19):sig_idx + 1]
    r20h, r20l = float(r20["High"].max()), float(r20["Low"].min())

    return {
        "ticker": ticker, "entry_date": entry_date, "signal_date": sig["Date"].strftime("%Y-%m-%d"),
        "close": round(c, 2), "atr14": round(atr, 4) if atr else None,
        "atr_pct": round(atr / c * 100, 2) if atr else None,
        "above_sma50": c > sma50 if sma50 else None, "above_sma200": c > sma200 if sma200 else None,
        "ema8_above_ema21": ema8 > ema21 if (ema8 and ema21) else None,
        "c_vs_ema8_pct": round((c - ema8) / ema8 * 100, 2) if ema8 else None,
        "c_vs_ema21_pct": round((c - ema21) / ema21 * 100, 2) if ema21 else None,
        "c_vs_sma10_pct": round((c - sma10) / sma10 * 100, 2) if sma10 else None,
        "c_vs_sma50_pct": round((c - sma50) / sma50 * 100, 2) if sma50 else None,
        "c_vs_ema8_atr": round((c - ema8) / atr, 2) if (ema8 and atr) else None,
        "c_vs_ema21_atr": round((c - ema21) / atr, 2) if (ema21 and atr) else None,
        "c_vs_sma50_atr": round((c - sma50) / atr, 2) if (sma50 and atr) else None,
        "sma50_slope_5d": sma50_slope, "swing_high_30d": round(swing_high, 2),
        "days_from_high": int(days_from_high), "pullback_pct": pullback_pct, "pullback_atr": pullback_atr,
        "pullback_low": round(pullback_low, 2), "days_since_low": int(days_since_low),
        "bounce_from_low_pct": bounce_pct, "bounce_from_low_atr": bounce_atr,
        "green_candles_3d": int((r3["Close"] > r3["Open"]).sum()),
        "green_candles_5d": int((r5["Close"] > r5["Open"]).sum()),
        "up_close_3d": int((r3["Close"] > r3["Close"].shift(1)).sum()),
        "up_close_5d": int((r5["Close"] > r5["Close"].shift(1)).sum()),
        "sig_is_green": c > o,
        "sig_close_position": round((c - l) / sig_range, 2) if sig_range > 0 else 0.5,
        "sig_body_atr": round(sig_body / atr, 2) if atr else None,
        "sig_range_atr": round(sig_range / atr, 2) if atr else None,
        "sig_vol_vs_20avg": round(vol / vol_avg, 2) if vol_avg else None,
        "total_pullback_days": int(days_from_high), "down_days_in_pullback": down_days,
        "pct_down_in_pullback": round(down_days / max(days_from_high, 1) * 100, 1),
        "range_20d_pct": round((r20h - r20l) / r20l * 100, 1) if r20l > 0 else None,
        "up_days_14": int((df.iloc[max(0, sig_idx - 13):sig_idx + 1]["Close"] > df.iloc[max(0, sig_idx - 13):sig_idx + 1]["Close"].shift(1)).sum()),
    }


# ============================================
# API ROUTES
# ============================================

@app.get("/api/setups")
async def get_setups():
    types = {"3-4db": {"name": "3-4DB", "desc": "3-4 Day Bounce (Short)"}, "dtss": {"name": "DTSS", "desc": "Double Top Short Sell"}, "htf": {"name": "HTF", "desc": "High Tight Flag (Long)"}}
    with get_db() as db:
        for st in types:
            types[st]["examples"] = db.execute("SELECT COUNT(*) FROM examples WHERE setup_type=?", (st,)).fetchone()[0]
    return types

@app.get("/api/examples/{setup_type}")
async def get_examples(setup_type: str):
    with get_db() as db:
        rows = db.execute("SELECT id, ticker, chart_date, entry_date FROM examples WHERE setup_type=? ORDER BY ticker", (setup_type,)).fetchall()
        examples = []
        for r in rows:
            has = db.execute("SELECT 1 FROM signal_analysis WHERE example_id=?", (r["id"],)).fetchone() is not None
            examples.append({"id": r["id"], "ticker": r["ticker"], "chartDate": r["chart_date"], "entryDate": r["entry_date"], "hasAnalysis": has})
    return {"setupType": setup_type, "examples": examples}

@app.get("/api/chart-image/{setup_type}/{example_id}")
async def get_chart_image(setup_type: str, example_id: int, at_entry: int = Query(0)):
    with get_db() as db:
        ex = db.execute("SELECT id, ticker, entry_date FROM examples WHERE id=? AND setup_type=?", (example_id, setup_type)).fetchone()
        if not ex: raise HTTPException(404, f"No example with id {example_id}")
        df = get_ohlcv_df(db, example_id)
        if df is None: raise HTTPException(404, f"No OHLCV data for {ex['ticker']}")
    png = generate_chart_png(df, ex["ticker"], ex["entry_date"], at_entry=bool(at_entry), setup_type=setup_type)
    if png is None: raise HTTPException(500, "Chart generation failed")
    return Response(content=png, media_type="image/png")

@app.get("/api/extension-data/{setup_type}/{example_id}")
async def api_extension_data(setup_type: str, example_id: int, entry_date: str = Query(None)):
    with get_db() as db:
        ex = db.execute("SELECT id, entry_date FROM examples WHERE id=? AND setup_type=?", (example_id, setup_type)).fetchone()
        if not ex: raise HTTPException(404)
        data = get_extension_rows(db, example_id)
        if not data: raise HTTPException(404)
    ed = entry_date or ex["entry_date"]
    if ed:
        entry_idx = len(data) - 1
        for i, d in enumerate(data):
            if d["date"] >= ed: entry_idx = i; break
        after_count = len(data) - entry_idx - 1
        before_count = max(min(after_count * 2, entry_idx), min(200, entry_idx))
        data = data[max(0, entry_idx - before_count):]
    return data

@app.get("/api/ohlcv/local/{setup_type}/{example_id}")
async def get_local_ohlcv(setup_type: str, example_id: int):
    with get_db() as db:
        ex = db.execute("SELECT id, ticker FROM examples WHERE id=? AND setup_type=?", (example_id, setup_type)).fetchone()
        if not ex: raise HTTPException(404)
        df = get_ohlcv_df(db, example_id)
        if df is None: raise HTTPException(404)
    df = add_indicators(df)
    candles = [{"date": row["Date"].strftime("%Y-%m-%d"), "open": clean_val(row["Open"]), "high": clean_val(row["High"]),
                "low": clean_val(row["Low"]), "close": clean_val(row["Close"]), "volume": clean_val(row["Volume"]),
                "ema8": clean_val(row.get("EMA8")), "ema21": clean_val(row.get("EMA21")), "sma50": clean_val(row.get("SMA50")),
                "sma200": clean_val(row.get("SMA200")), "atr14": clean_val(row.get("ATR14")), "volAvg20": clean_val(row.get("VolAvg20"))}
               for _, row in df.tail(150).iterrows()]
    return {"ticker": ex["ticker"], "candles": candles}

@app.get("/api/conditions/{setup_type}")
async def get_conditions(setup_type: str):
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT id, name, description, pcf, active FROM conditions WHERE setup_type=? ORDER BY sort_order", (setup_type,)).fetchall()]

class SaveExampleRequest(BaseModel):
    ticker: str
    chart_date: str
    entry_date: str

@app.post("/api/examples/{setup_type}")
async def save_example(setup_type: str, req: SaveExampleRequest):
    ticker = req.ticker.upper().strip()
    try:
        datetime.strptime(req.chart_date, "%Y-%m-%d")
        datetime.strptime(req.entry_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    ohlcv_df = fetch_ohlcv(ticker, req.chart_date)
    if ohlcv_df is None: raise HTTPException(404, f"No data for {ticker}")
    ext_df = fetch_extension(ticker)

    with get_db() as db:
        existing = db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?", (setup_type, ticker, req.entry_date)).fetchone()
        if existing:
            eid = existing["id"]
            db.execute("UPDATE examples SET chart_date=? WHERE id=?", (req.chart_date, eid))
            db.execute("DELETE FROM ohlcv WHERE example_id=?", (eid,))
            db.execute("DELETE FROM extension WHERE example_id=?", (eid,))
            db.execute("DELETE FROM signal_analysis WHERE example_id=?", (eid,))
        else:
            eid = db.execute("INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                             (setup_type, ticker, req.chart_date, req.entry_date)).lastrowid
        store_ohlcv(db, eid, ohlcv_df)
        if ext_df is not None: store_extension(db, eid, ext_df)
        analysis = None
        try:
            analysis = run_signal_analysis(ohlcv_df, ticker, req.entry_date)
            db.execute("INSERT OR REPLACE INTO signal_analysis (example_id, analysis_json) VALUES (?,?)", (eid, json.dumps(analysis)))
        except Exception as e:
            analysis = {"error": str(e)}
    return {"status": "saved", "ticker": ticker, "entryDate": req.entry_date, "analysis": analysis}

@app.delete("/api/examples/{setup_type}/{example_id}")
async def delete_example(setup_type: str, example_id: int):
    with get_db() as db:
        ex = db.execute("SELECT id FROM examples WHERE id=? AND setup_type=?", (example_id, setup_type)).fetchone()
        if not ex: raise HTTPException(404, f"No example with id {example_id}")
        db.execute("DELETE FROM examples WHERE id=?", (example_id,))
    return {"status": "deleted", "id": example_id}

class UpdateEntryRequest(BaseModel):
    entry_date: str
    ticker: str = None

@app.patch("/api/examples/{setup_type}/{example_id}")
async def update_entry_date(setup_type: str, example_id: int, req: UpdateEntryRequest):
    try:
        datetime.strptime(req.entry_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    with get_db() as db:
        ex = db.execute("SELECT id, ticker, chart_date, entry_date FROM examples WHERE id=? AND setup_type=?", (example_id, setup_type)).fetchone()
        if not ex: raise HTTPException(404)
        ticker = req.ticker if req.ticker else ex["ticker"]
        if req.ticker and req.ticker != ex["ticker"]:
            db.execute("UPDATE examples SET ticker=? WHERE id=?", (req.ticker, example_id))

        # Check if new entry_date falls outside the existing OHLCV range
        ohlcv_range = db.execute("SELECT MIN(date) as min_d, MAX(date) as max_d FROM ohlcv WHERE example_id=?", (example_id,)).fetchone()
        need_refetch = True
        if ohlcv_range and ohlcv_range["min_d"] and ohlcv_range["max_d"]:
            need_refetch = req.entry_date < ohlcv_range["min_d"] or req.entry_date > ohlcv_range["max_d"]

        if need_refetch:
            ohlcv_df = fetch_ohlcv(ticker, req.entry_date)
            if ohlcv_df is None:
                raise HTTPException(404, f"No OHLCV data for {ticker} around {req.entry_date}")
            db.execute("DELETE FROM ohlcv WHERE example_id=?", (example_id,))
            db.execute("DELETE FROM extension WHERE example_id=?", (example_id,))
            store_ohlcv(db, example_id, ohlcv_df)
            ext_df = fetch_extension(ticker)
            if ext_df is not None:
                store_extension(db, example_id, ext_df)
            db.execute("UPDATE examples SET entry_date=?, chart_date=? WHERE id=?", (req.entry_date, req.entry_date, example_id))
            df = ohlcv_df
        else:
            db.execute("UPDATE examples SET entry_date=? WHERE id=?", (req.entry_date, example_id))
            df = get_ohlcv_df(db, example_id)

        analysis = None
        if df is not None:
            try:
                analysis = run_signal_analysis(df, ticker, req.entry_date)
                db.execute("INSERT OR REPLACE INTO signal_analysis (example_id, analysis_json) VALUES (?,?)", (example_id, json.dumps(analysis)))
            except Exception as e:
                analysis = {"error": str(e)}
    return {"status": "updated", "ticker": ticker, "entryDate": req.entry_date, "refetched": need_refetch, "analysis": analysis}


@app.post("/api/repair-data")
async def repair_missing_data():
    """Re-fetch OHLCV and extension data for any examples missing it."""
    repaired = []
    with get_db() as db:
        examples = db.execute("SELECT id, ticker, chart_date, entry_date FROM examples").fetchall()
        for ex in examples:
            has_ohlcv = db.execute("SELECT 1 FROM ohlcv WHERE example_id=? LIMIT 1", (ex["id"],)).fetchone()
            if not has_ohlcv:
                print(f"  Repairing OHLCV for {ex['ticker']} (id={ex['id']})...")
                ohlcv_df = fetch_ohlcv(ex["ticker"], ex["chart_date"])
                if ohlcv_df is not None:
                    store_ohlcv(db, ex["id"], ohlcv_df)
                ext_df = fetch_extension(ex["ticker"])
                if ext_df is not None:
                    store_extension(db, ex["id"], ext_df)
                try:
                    if ohlcv_df is not None:
                        analysis = run_signal_analysis(ohlcv_df, ex["ticker"], ex["entry_date"])
                        db.execute("INSERT OR REPLACE INTO signal_analysis (example_id, analysis_json) VALUES (?,?)", (ex["id"], json.dumps(analysis)))
                except Exception as e:
                    print(f"  Analysis error for {ex['ticker']}: {e}")
                repaired.append(ex["ticker"])
    return {"repaired": repaired, "count": len(repaired)}


# ============================================
# UNIVERSE DATA — full market OHLCV
# ============================================

@app.post("/api/universe/fetch")
async def start_universe_fetch(background_tasks: BackgroundTasks):
    """Kick off full market OHLCV fetch in background. Fire and forget."""
    from scripts.fetch_universe import run_full_fetch, init_universe_tables, get_db as u_get_db

    # Check if already running
    try:
        udb = u_get_db()
        init_universe_tables(udb)
        row = udb.execute("SELECT state, updated_at FROM universe_fetch_status WHERE id=1").fetchone()
        if row and row["state"] == "running":
            # Check if stale (no update in 15 minutes = dead process)
            try:
                from datetime import datetime, timedelta
                last_update = datetime.fromisoformat(row["updated_at"])
                if datetime.utcnow() - last_update < timedelta(minutes=15):
                    udb.close()
                    return {"status": "already_running", "message": "Fetch is already in progress. Check /api/universe/status"}
                else:
                    # Stale — mark as crashed and allow re-trigger
                    udb.execute("UPDATE universe_fetch_status SET state='crashed', current_batch='stale process detected' WHERE id=1")
                    udb.commit()
            except Exception:
                udb.close()
                return {"status": "already_running", "message": "Fetch is already in progress. Check /api/universe/status"}
        udb.close()
    except Exception:
        pass

    background_tasks.add_task(run_full_fetch)
    return {"status": "started", "message": "Universe fetch kicked off in background. Check /api/universe/status for progress."}


@app.get("/api/universe/status")
async def universe_fetch_status():
    """Check progress of universe data fetch."""
    with get_db() as db:
        try:
            row = db.execute("SELECT * FROM universe_fetch_status WHERE id=1").fetchone()
            if not row:
                return {"state": "not_started"}
            result = dict(row)
            result["errors"] = json.loads(result.get("errors", "[]"))
            # Add DB stats
            try:
                total_rows = db.execute("SELECT COUNT(*) FROM universe_ohlcv").fetchone()[0]
                total_tickers_done = db.execute("SELECT COUNT(DISTINCT ticker) FROM universe_ohlcv").fetchone()[0]
                result["db_rows"] = total_rows
                result["db_tickers"] = total_tickers_done
                # Estimate size
                page_count = db.execute("PRAGMA page_count").fetchone()[0]
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                result["db_size_mb"] = round((page_count * page_size) / (1024 * 1024), 1)
            except Exception:
                pass
            return result
        except Exception:
            return {"state": "not_initialized"}


@app.post("/api/universe/tickers")
async def upload_ticker_list(tickers: list[str]):
    """Upload a custom ticker list (fallback if NASDAQ FTP fails)."""
    from scripts.fetch_universe import init_universe_tables
    with get_db() as db:
        init_universe_tables(db)
        count = 0
        for t in tickers:
            t = t.strip().upper()
            if t and len(t) <= 5:
                db.execute("INSERT OR IGNORE INTO universe_tickers (ticker) VALUES (?)", (t,))
                count += 1
        db.commit()
    return {"stored": count}


@app.post("/api/universe/load-file")
async def load_ticker_file():
    """Load tickers from the bundled universe_tickers.txt file on the server."""
    from scripts.fetch_universe import init_universe_tables
    import glob

    # Search everywhere for the file
    candidates = glob.glob("/app/**/*universe*ticker*", recursive=True) + \
                 glob.glob("/app/**/*Universe*", recursive=True) + \
                 glob.glob("./**/*universe*ticker*", recursive=True)

    # Also check known paths
    for p in ["/app/universe_tickers.txt", "universe_tickers.txt",
              "/app/data/universe_tickers.txt", "data/universe_tickers.txt"]:
        if Path(p).exists() and p not in candidates:
            candidates.append(p)

    # Find the first one that exists and has content
    found = None
    for c in candidates:
        p = Path(c)
        if p.is_file() and p.stat().st_size > 100:
            found = p
            break

    if not found:
        return {"error": "No ticker file found", "searched": candidates}

    lines = found.read_text().replace("\r", "").strip().split("\n")
    tickers = [l.strip().upper() for l in lines if l.strip()]

    with get_db() as db:
        init_universe_tables(db)
        count = 0
        for t in tickers:
            if t and len(t) <= 6:
                db.execute("INSERT OR IGNORE INTO universe_tickers (ticker) VALUES (?)", (t,))
                count += 1
        db.commit()

    return {"stored": count, "file": str(found), "candidates_found": candidates}


@app.post("/api/universe/reset")
async def reset_universe_fetch():
    """Reset fetch status so it can be re-run. Does NOT delete existing OHLCV data."""
    with get_db() as db:
        try:
            db.execute("UPDATE universe_tickers SET status='pending', rows_stored=0, fetched_at=NULL")
            db.execute("UPDATE universe_fetch_status SET state='idle', completed_tickers=0, failed_tickers=0, skipped_tickers=0, current_batch=NULL, errors='[]'")
            db.commit()
        except Exception:
            pass
    return {"status": "reset"}


@app.delete("/api/universe/data")
async def delete_universe_data():
    """Nuclear option — delete all universe OHLCV data."""
    with get_db() as db:
        try:
            db.execute("DELETE FROM universe_ohlcv")
            db.execute("DELETE FROM universe_tickers")
            db.execute("UPDATE universe_fetch_status SET state='idle', completed_tickers=0, failed_tickers=0, skipped_tickers=0, current_batch=NULL, errors='[]', total_tickers=0")
            db.commit()
            db.execute("VACUUM")
        except Exception:
            pass
    return {"status": "deleted"}


# ============================================
# MIGRATION — import existing flat file data on first run
# ============================================

@app.on_event("startup")
async def migrate_legacy_data():
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM examples").fetchone()[0]
        if count > 0:
            print(f"DB has {count} examples, skipping migration")
            return

    print("=== Migrating legacy data to SQLite ===")
    legacy_dir = LEGACY_DATA_DIR / "ohlcv"
    if not legacy_dir.exists():
        legacy_dir = Path("data/ohlcv")
    if not legacy_dir.exists():
        print("No legacy data found"); return

    for setup_dir in legacy_dir.iterdir():
        if not setup_dir.is_dir(): continue
        setup_type = setup_dir.name
        entry_file = setup_dir / "entry_dates.json"
        if not entry_file.exists(): continue
        entries = json.loads(entry_file.read_text())

        analysis_map = {}
        af = setup_dir / "signal_day_analysis.json"
        if af.exists():
            for a in json.loads(af.read_text()): analysis_map[a["ticker"]] = a

        with get_db() as db:
            for entry in entries:
                ticker = entry["ticker"]
                chart_date = entry.get("chart_date", entry.get("entry_date"))
                entry_date = entry["entry_date"]
                cur = db.execute("INSERT OR IGNORE INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                                 (setup_type, ticker, chart_date, entry_date))
                if cur.lastrowid == 0: continue
                eid = cur.lastrowid

                csvs = list(setup_dir.glob(f"{ticker}_*.csv"))
                if csvs:
                    raw = pd.read_csv(csvs[0]); raw["Date"] = pd.to_datetime(raw["Date"])
                    raw = raw.sort_values("Date").reset_index(drop=True)
                    store_ohlcv(db, eid, raw)

                ext_csv = LEGACY_DATA_DIR / "extension" / setup_type / f"{ticker}.csv"
                if not ext_csv.exists():
                    ext_csv = Path("data/extension") / setup_type / f"{ticker}.csv"
                if ext_csv.exists():
                    ext = pd.read_csv(ext_csv); ext["Date"] = pd.to_datetime(ext["Date"])
                    # Legacy CSVs have pct columns — skip them, re-fetch fresh xATR data
                    if "ext_sma50_xatr" not in ext.columns:
                        print(f"  {ticker}: legacy ext CSV has pct columns, re-fetching for xATR...")
                        ext = fetch_extension(ticker)
                    if ext is not None:
                        store_extension(db, eid, ext)

                if ticker in analysis_map:
                    db.execute("INSERT OR REPLACE INTO signal_analysis (example_id, analysis_json) VALUES (?,?)",
                               (eid, json.dumps(analysis_map[ticker])))

                print(f"  Migrated: {ticker}")
        print(f"  Setup '{setup_type}': {len(entries)} examples")
    print("=== Migration complete ===")


# ---------------------------------------------------------------------------
# Tradable Universe
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def rebuild_tradable_universe(db):
    """Rebuild the tradable_universe table from universe_ohlcv data.
    Filters: last close >= $1, 20-day avg dollar volume >= $5M.
    Excludes any ticker in universe_exclusions table.
    """
    # Ensure exclusions table exists (no-op if already there)
    db.execute("""CREATE TABLE IF NOT EXISTS universe_exclusions (
        ticker TEXT PRIMARY KEY,
        reason TEXT,
        added_at TEXT
    )""")

    db.execute("DROP TABLE IF EXISTS tradable_universe")
    db.execute("""
        CREATE TABLE tradable_universe (
            ticker TEXT PRIMARY KEY,
            last_close REAL,
            avg_dollar_volume REAL,
            last_date TEXT,
            updated_at TEXT
        )
    """)

    # For each ticker with recent data, compute stats from the last 20 trading days
    now_iso = __import__('datetime').datetime.utcnow().isoformat()
    db.execute("""
        INSERT INTO tradable_universe (ticker, last_close, avg_dollar_volume, last_date, updated_at)
        SELECT
            t.ticker,
            t.last_close,
            t.avg_dv,
            t.last_date,
            ?
        FROM (
            SELECT
                ticker,
                -- last close = close on the most recent date
                (SELECT close FROM universe_ohlcv u2
                 WHERE u2.ticker = u1.ticker ORDER BY u2.date DESC LIMIT 1) as last_close,
                -- avg dollar volume over last 20 trading days
                AVG(close * volume) as avg_dv,
                MAX(date) as last_date
            FROM (
                SELECT ticker, date, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) as rn
                FROM universe_ohlcv
                WHERE close IS NOT NULL AND volume IS NOT NULL AND volume > 0
            ) u1
            WHERE rn <= 20
            GROUP BY ticker
            HAVING COUNT(*) >= 10  -- need at least 10 days of data
        ) t
        WHERE t.last_close >= 1.0
          AND t.avg_dv >= 5000000
          AND t.ticker NOT IN (
              SELECT ticker FROM universe_exclusions
              WHERE 1=1  -- table may not exist on fresh DB, handled by try below
          )
    """, (now_iso,))

    count = db.execute("SELECT COUNT(*) FROM tradable_universe").fetchone()[0]
    db.commit()

    return count


@app.post("/api/universe/append-daily")
async def append_daily_data():
    """
    Nightly append — fetch only missing trading days for all tradable tickers.
    Synchronous (blocks until done) since it's much faster than full fetch.
    Returns immediately if DB is already up to date.
    """
    try:
        from scripts.fetch_universe import append_daily
        result = append_daily()

        # If new data was added, rebuild tradable universe too
        if result.get("status") == "complete" and result.get("new_rows", 0) > 0:
            with get_db() as db:
                tradable_count = rebuild_tradable_universe(db)
            result["tradable_rebuilt"] = True
            result["tradable_count"] = tradable_count

        return result
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "trace": traceback.format_exc()}


@app.post("/api/tradable/rebuild")
async def rebuild_tradable():
    """Rebuild the tradable universe from current OHLCV data."""
    try:
        with get_db() as db:
            count = rebuild_tradable_universe(db)
        return {"status": "ok", "tradable_count": count}
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}


@app.get("/api/tradable")
async def get_tradable(sort: str = "ticker", limit: int = 0):
    """Get current tradable universe list."""
    try:
        with get_db() as db:
            order = "ticker"
            if sort == "volume":
                order = "avg_dollar_volume DESC"
            elif sort == "price":
                order = "last_close DESC"

            q = f"SELECT * FROM tradable_universe ORDER BY {order}"
            if limit > 0:
                q += f" LIMIT {limit}"

            rows = db.execute(q).fetchall()
            count = db.execute("SELECT COUNT(*) FROM tradable_universe").fetchone()[0]

        tickers = [dict(r) for r in rows]
        return {"count": count, "tickers": tickers}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 3-4DB Scanner endpoint
# ---------------------------------------------------------------------------
@app.post("/api/scan/3-4db")
async def scan_3_4db(background_tasks: BackgroundTasks, days: int = 77):
    """Kick off 3-4DB scan in background."""
    from scripts.scan_3_4db import run_scan

    def _run():
        try:
            db = sqlite3.connect(str(DB_PATH), timeout=30)
            db.execute("""CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_type TEXT, ticker TEXT, date TEXT, close REAL,
                atr14 REAL, volume INTEGER, avgv20 INTEGER,
                pct_above_sma50 REAL, pct_above_sma200 REAL, retracement REAL,
                scanned_at TEXT
            )""")
            db.execute("DELETE FROM scan_results WHERE scan_type='3-4db'")
            db.commit()

            sdf = run_scan(lookback_days=days, db_path=str(DB_PATH))

            if not sdf.empty:
                now = datetime.utcnow().isoformat()
                rows = []
                for _, s in sdf.iterrows():
                    rows.append((
                        "3-4db", s["ticker"], s["date"], s["close"],
                        s["atr14"], int(s["volume"]), int(s["avgv20"]),
                        s["pct_above_sma50"], s["pct_above_sma200"],
                        s["retracement"], now
                    ))
                db.executemany(
                    "INSERT INTO scan_results (scan_type, ticker, date, close, atr14, volume, avgv20, pct_above_sma50, pct_above_sma200, retracement, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows
                )
                db.commit()
            db.close()
        except Exception as e:
            import traceback
            traceback.print_exc()

    background_tasks.add_task(_run)
    return {"status": "started", "message": f"Scanning tradable universe for 3-4DB setups (last {days} days). Check GET /api/scan/3-4db/results"}


@app.get("/api/scan/3-4db/results")
async def scan_3_4db_results():
    """Get stored 3-4DB scan results."""
    try:
        with get_db() as db:
            # Check if table exists
            exists = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='scan_results'").fetchone()[0]
            if not exists:
                return {"signals": [], "count": 0, "message": "No scan has been run yet. POST /api/scan/3-4db"}

            rows = db.execute(
                "SELECT ticker, date, close, atr14, volume, avgv20, pct_above_sma50, pct_above_sma200, retracement, scanned_at "
                "FROM scan_results WHERE scan_type='3-4db' ORDER BY date DESC, ticker"
            ).fetchall()

            signals = [dict(r) for r in rows]
            scanned_at = signals[0]["scanned_at"] if signals else None

            # Group count by date
            date_counts = {}
            for s in signals:
                date_counts[s["date"]] = date_counts.get(s["date"], 0) + 1

            return {
                "count": len(signals),
                "signals": signals,
                "dates": date_counts,
                "scanned_at": scanned_at
            }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# ============================================
# DTSS SCAN ENDPOINTS
# ============================================

@app.post("/api/scan/dtss")
async def scan_dtss(background_tasks: BackgroundTasks, days: int = 77):
    """Kick off DTSS scan in background."""
    from scripts.scan_dtss import run_scan

    def _run():
        try:
            db = sqlite3.connect(str(DB_PATH), timeout=30)
            db.execute("""CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_type TEXT, ticker TEXT, date TEXT, close REAL,
                atr14 REAL, volume INTEGER, avgv20 INTEGER,
                pct_above_sma50 REAL, pct_above_sma200 REAL, retracement REAL,
                scanned_at TEXT
            )""")
            db.execute("DELETE FROM scan_results WHERE scan_type='dtss'")
            db.commit()

            sdf = run_scan(lookback_days=days, db_path=str(DB_PATH))

            if not sdf.empty:
                now = datetime.utcnow().isoformat()
                rows = []
                for _, s in sdf.iterrows():
                    rows.append((
                        "dtss", s["ticker"], s["date"], s["close"],
                        s["atr14"], int(s["volume"]), int(s["avgv20"]),
                        s.get("range20_atr", 0), s.get("h_from_low65", 0),
                        s.get("near_high", 0), now
                    ))
                db.executemany(
                    "INSERT INTO scan_results (scan_type, ticker, date, close, atr14, volume, avgv20, pct_above_sma50, pct_above_sma200, retracement, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows
                )
                db.commit()
            db.close()
        except Exception as e:
            import traceback
            traceback.print_exc()

    background_tasks.add_task(_run)
    return {"status": "started", "message": f"Scanning universe for DTSS setups (last {days} days). Check GET /api/scan/dtss/results"}


@app.get("/api/scan/dtss/results")
async def scan_dtss_results():
    """Get stored DTSS scan results."""
    try:
        with get_db() as db:
            exists = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='scan_results'").fetchone()[0]
            if not exists:
                return {"signals": [], "count": 0, "message": "No scan has been run yet. POST /api/scan/dtss"}

            rows = db.execute(
                "SELECT ticker, date, close, atr14, volume, avgv20, "
                "pct_above_sma50 as range20_atr, pct_above_sma200 as h_from_low65, "
                "retracement as near_high, scanned_at "
                "FROM scan_results WHERE scan_type='dtss' ORDER BY date DESC, ticker"
            ).fetchall()

            signals = [dict(r) for r in rows]
            scanned_at = signals[0]["scanned_at"] if signals else None

            date_counts = {}
            for s in signals:
                date_counts[s["date"]] = date_counts.get(s["date"], 0) + 1

            return {
                "count": len(signals),
                "signals": signals,
                "dates": date_counts,
                "scanned_at": scanned_at
            }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# ============================================
# BACKTEST ENDPOINTS
# ============================================

@app.post("/api/backtest/signals/upload")
async def upload_backtest_signals(request: Request):
    """Upload backtest signals from desktop runner.
    
    Expects JSON:
    {
        "setup_type": "dtss",
        "signals": [{"date": "2024-01-15", "ticker": "AAPL"}, ...],
        "conditions_hash": "abc123",  // optional
        "grinder_version": "v2"       // optional
    }
    
    Replaces all existing signals for that setup_type.
    """
    try:
        body = await request.json()
        setup_type = body.get("setup_type", "").lower()
        signals = body.get("signals", [])
        conditions_hash = body.get("conditions_hash", "")
        grinder_version = body.get("grinder_version", "")
        
        if not setup_type:
            raise HTTPException(400, "setup_type required")
        if not signals:
            raise HTTPException(400, "signals array required")
        
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS backtest_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setup_type TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    conditions_hash TEXT,
                    UNIQUE(setup_type, ticker, date)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_bt_sig_setup ON backtest_signals(setup_type)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_bt_sig_date ON backtest_signals(date)")
            
            # Clear old signals for this setup
            db.execute("DELETE FROM backtest_signals WHERE setup_type=?", (setup_type,))
            
            now = datetime.now().isoformat()
            inserted = 0
            for sig in signals:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO backtest_signals (setup_type, ticker, date, uploaded_at, conditions_hash) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (setup_type, sig["ticker"], sig["date"], now, conditions_hash)
                    )
                    inserted += 1
                except Exception:
                    pass
            
            db.commit()
            
            # Aggregate stats
            total = db.execute("SELECT COUNT(*) FROM backtest_signals WHERE setup_type=?", (setup_type,)).fetchone()[0]
            tickers = db.execute("SELECT COUNT(DISTINCT ticker) FROM backtest_signals WHERE setup_type=?", (setup_type,)).fetchone()[0]
            dates = db.execute("SELECT COUNT(DISTINCT date) FROM backtest_signals WHERE setup_type=?", (setup_type,)).fetchone()[0]
            max_per_day = db.execute(
                "SELECT COUNT(*) as c FROM backtest_signals WHERE setup_type=? GROUP BY date ORDER BY c DESC LIMIT 1",
                (setup_type,)
            ).fetchone()
            
        return {
            "status": "ok",
            "setup_type": setup_type,
            "inserted": inserted,
            "total": total,
            "unique_tickers": tickers,
            "unique_dates": dates,
            "max_signals_per_day": max_per_day[0] if max_per_day else 0,
            "uploaded_at": now,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/backtest/signals/{setup_type}")
async def get_backtest_signals(setup_type: str, limit: int = 5000, offset: int = 0):
    """Get backtest signals for a setup type. Used by Historical tab."""
    try:
        with get_db() as db:
            exists = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='backtest_signals'"
            ).fetchone()[0]
            if not exists:
                return {"count": 0, "unique_tickers": 0, "results": [], "message": "No signals uploaded yet."}
            
            total = db.execute(
                "SELECT COUNT(*) FROM backtest_signals WHERE setup_type=?", (setup_type,)
            ).fetchone()[0]
            tickers = db.execute(
                "SELECT COUNT(DISTINCT ticker) FROM backtest_signals WHERE setup_type=?", (setup_type,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT ticker, date FROM backtest_signals WHERE setup_type=? ORDER BY date, ticker LIMIT ? OFFSET ?",
                (setup_type, limit, offset)
            ).fetchall()
            
            return {
                "count": total,
                "unique_tickers": tickers,
                "showing": len(rows),
                "results": [dict(r) for r in rows],
            }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/backtest/run")
async def run_backtest_endpoint(background_tasks: BackgroundTasks):
    """Kick off full 5-year 3-4DB backtest in background."""
    from scripts.backtest_3_4db import run_backtest
    background_tasks.add_task(run_backtest)
    return {
        "status": "started",
        "message": "Full 5-year backtest kicked off. Check GET /api/backtest/status for progress."
    }


@app.get("/api/backtest/status")
async def backtest_status():
    """Get current backtest progress."""
    try:
        with get_db() as db:
            exists = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='backtest_status'"
            ).fetchone()[0]
            if not exists:
                return {"state": "idle", "message": "No backtest has been run yet."}
            row = db.execute("SELECT * FROM backtest_status WHERE id=1").fetchone()
            if not row:
                return {"state": "idle"}
            d = dict(row)
            # Add live counts
            for table in ["scan_backtest", "scan_backtest_clean"]:
                t_exists = db.execute(
                    f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{table}'"
                ).fetchone()[0]
                if t_exists:
                    d[f"{table}_count"] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    d[f"{table}_tickers"] = db.execute(f"SELECT COUNT(DISTINCT ticker) FROM {table}").fetchone()[0]
            return d
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/backtest/results")
async def backtest_results(
    clean: bool = True,
    ticker: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 500,
    offset: int = 0,
):
    """Get backtest results. clean=true for filtered, clean=false for raw."""
    table = "scan_backtest_clean" if clean else "scan_backtest"
    try:
        with get_db() as db:
            exists = db.execute(
                f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{table}'"
            ).fetchone()[0]
            if not exists:
                return {"count": 0, "results": [], "message": f"No results yet. Run backtest first."}

            wheres, params = [], []
            if ticker:
                wheres.append("ticker = ?")
                params.append(ticker.upper())
            if date_from:
                wheres.append("date >= ?")
                params.append(date_from)
            if date_to:
                wheres.append("date <= ?")
                params.append(date_to)

            where = f"WHERE {' AND '.join(wheres)}" if wheres else ""

            total = db.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
            unique = db.execute(f"SELECT COUNT(DISTINCT ticker) FROM {table} {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM {table} {where} ORDER BY date DESC, ticker LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()

            return {
                "count": total,
                "unique_tickers": unique,
                "showing": len(rows),
                "offset": offset,
                "results": [dict(r) for r in rows],
            }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/backtest/summary")
async def backtest_summary():
    """Summary stats of backtest results."""
    try:
        with get_db() as db:
            out = {}
            for table, label in [("scan_backtest", "raw"), ("scan_backtest_clean", "clean")]:
                exists = db.execute(
                    f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{table}'"
                ).fetchone()[0]
                if not exists:
                    out[label] = {"signals": 0, "tickers": 0}
                    continue

                total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                tickers = db.execute(f"SELECT COUNT(DISTINCT ticker) FROM {table}").fetchone()[0]
                dates = db.execute(f"SELECT MIN(date), MAX(date) FROM {table}").fetchone()

                yearly = db.execute(f"""
                    SELECT SUBSTR(date, 1, 4) as year, COUNT(*) as cnt,
                           COUNT(DISTINCT ticker) as tickers
                    FROM {table} GROUP BY year ORDER BY year
                """).fetchall()

                out[label] = {
                    "signals": total,
                    "tickers": tickers,
                    "date_range": [dates[0], dates[1]] if dates[0] else None,
                    "by_year": [dict(r) for r in yearly],
                }

            # Filter info
            sector_exists = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ticker_sectors'"
            ).fetchone()[0]
            if sector_exists:
                out["filters"] = {
                    "total_classified": db.execute("SELECT COUNT(*) FROM ticker_sectors").fetchone()[0],
                    "biotech": db.execute(
                        "SELECT COUNT(*) FROM ticker_sectors WHERE LOWER(industry) LIKE '%biotech%' "
                        "OR LOWER(industry) LIKE '%pharma%' OR LOWER(industry) LIKE '%drug%'"
                    ).fetchone()[0],
                    "etf": db.execute("SELECT COUNT(*) FROM ticker_sectors WHERE is_etf = 1").fetchone()[0],
                }

            return out
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FAST SQL QUERY ENDPOINT
# ---------------------------------------------------------------------------

@app.post("/api/query")
async def run_query(request: Request):
    """Run a read-only SQL query against the database. Returns rows and count."""
    body = await request.json()
    sql = body.get("sql", "")
    if not sql:
        return {"error": "No SQL provided"}
    
    # Safety: read-only
    sql_lower = sql.strip().lower()
    if not sql_lower.startswith("select"):
        return {"error": "Only SELECT queries allowed"}
    
    try:
        with get_db() as db:
            rows = db.execute(sql).fetchall()
            results = [dict(r) for r in rows]
            return {"count": len(results), "results": results[:100]}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# PROFILING ENGINE SUPPORT
# ---------------------------------------------------------------------------

@app.post("/api/query/bulk")
async def run_query_bulk(request: Request):
    """Run a read-only SQL query with higher row limit (for profiling engine)."""
    body = await request.json()
    sql = body.get("sql", "")
    limit = min(body.get("limit", 1000), 5000)  # Max 5000 rows
    if not sql:
        return {"error": "No SQL provided"}
    sql_lower = sql.strip().lower()
    if not sql_lower.startswith("select"):
        return {"error": "Only SELECT queries allowed"}
    try:
        with get_db() as db:
            rows = db.execute(sql).fetchall()
            results = [dict(r) for r in rows]
            return {"count": len(results), "results": results[:limit]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ohlcv/bulk/{ticker}")
async def get_ohlcv_bulk(ticker: str, end_date: str = Query(None),
                         lookback: int = Query(250)):
    """Fetch OHLCV data for a ticker with configurable lookback.
    Returns up to `lookback` bars ending on `end_date`."""
    lookback = min(lookback, 1500)
    try:
        with get_db() as db:
            if end_date:
                rows = db.execute(
                    "SELECT date, open, high, low, close, volume "
                    "FROM universe_ohlcv "
                    "WHERE ticker=? AND date<=? "
                    "ORDER BY date DESC LIMIT ?",
                    (ticker, end_date, lookback)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT date, open, high, low, close, volume "
                    "FROM universe_ohlcv "
                    "WHERE ticker=? "
                    "ORDER BY date DESC LIMIT ?",
                    (ticker, lookback)
                ).fetchall()
            results = [dict(r) for r in rows]
            return {"count": len(results), "results": results}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FAST DTSS CONDITION TESTER
# ---------------------------------------------------------------------------

@app.post("/api/scan/dtss/test")
async def test_dtss_conditions(request: Request):
    """
    Fast condition tester. Pass JSON with threshold overrides.
    Scans last N bars of ALL universe tickers, returns count + ticker list.
    Defaults match the 12 validated conditions.
    """
    import numpy as np
    
    body = await request.json()
    days = body.get("days", 5)  # only check last N trading days
    
    # Thresholds (all in ATR multiples unless noted)
    slope_min = body.get("slope_min", -0.2)
    range20_min = body.get("range20_min", 3.0)
    near_high_max = body.get("near_high_max", 3.0)
    candle_min = body.get("candle_min", 0.6)
    hc_min = body.get("hc_min", 0.05)
    hcho_min = body.get("hcho_min", 0.5)
    pullback = body.get("pullback", True)  # MINC15 < EMA8
    vol_floor = body.get("vol_floor", 0.5)
    vol_cap = body.get("vol_cap", 3.0)
    up5_min = body.get("up5_min", 1)
    h_minl65_min = body.get("h_minl65_min", 3.0)
    c_minl65_min = body.get("c_minl65_min", 2.5)
    
    # Conditions to skip (pass list of names to disable)
    skip = set(body.get("skip", []))
    
    db = sqlite3.connect(str(DB_PATH), timeout=60)
    
    # Get most recent date
    max_date = db.execute("SELECT MAX(date) FROM universe_ohlcv").fetchone()[0]
    
    # Get all tickers
    tickers = [r[0] for r in db.execute("SELECT DISTINCT ticker FROM universe_ohlcv").fetchall()]
    
    signals = []
    
    for ticker in tickers:
        rows = db.execute(
            "SELECT date, open, high, low, close, volume FROM universe_ohlcv WHERE ticker=? ORDER BY date",
            (ticker,)
        ).fetchall()
        
        n = len(rows)
        if n < 100:
            continue
        
        # Convert to arrays for speed
        dates = [r[0] for r in rows]
        O = np.array([r[1] for r in rows], dtype=float)
        H = np.array([r[2] for r in rows], dtype=float)
        L = np.array([r[3] for r in rows], dtype=float)
        C = np.array([r[4] for r in rows], dtype=float)
        V = np.array([r[5] for r in rows], dtype=float)
        
        # ATR14
        tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - C[:-1]), np.abs(L[1:] - C[:-1])))
        tr = np.concatenate([[H[0]-L[0]], tr])
        atr = np.full(n, np.nan)
        atr[13] = np.mean(tr[:14])
        for j in range(14, n):
            atr[j] = (atr[j-1] * 13 + tr[j]) / 14
        
        # SMA50
        sma50 = np.full(n, np.nan)
        cs = np.cumsum(C)
        sma50[49:] = (cs[49:] - np.concatenate([[0], cs[:n-50]])) / 50
        
        # EMA8
        ema8 = np.full(n, np.nan)
        ema8[7] = np.mean(C[:8])
        mult = 2.0 / 9.0
        for j in range(8, n):
            ema8[j] = C[j] * mult + ema8[j-1] * (1 - mult)
        
        # AvgV20
        avgv = np.full(n, np.nan)
        vs = np.cumsum(V)
        avgv[19:] = (vs[19:] - np.concatenate([[0], vs[:n-20]])) / 20
        
        # Only check last `days` bars
        start_idx = max(65, n - days)
        
        for i in range(start_idx, n):
            if np.isnan(atr[i]) or atr[i] <= 0: continue
            if np.isnan(sma50[i]) or np.isnan(ema8[i]) or np.isnan(avgv[i]): continue
            if i < 65: continue
            
            a = atr[i]
            
            # 1. SMA50 slope
            if "slope" not in skip:
                if i < 60 or np.isnan(sma50[i-10]): continue
                if not (sma50[i] > sma50[i-10] - slope_min * a): continue
            
            # 2. Range20
            if "range20" not in skip:
                maxh20 = np.max(H[max(0,i-19):i+1])
                minl20 = np.min(L[max(0,i-19):i+1])
                if not ((maxh20 - minl20) > range20_min * a): continue
            else:
                maxh20 = np.max(H[max(0,i-19):i+1])
            
            # 3. Near high
            if "near_high" not in skip:
                if 'maxh20' not in dir(): maxh20 = np.max(H[max(0,i-19):i+1])
                if not ((maxh20 - H[i]) < near_high_max * a): continue
            
            # 4. Candle size
            if "candle" not in skip:
                if not ((H[i] - L[i]) >= candle_min * a): continue
            
            # 5. H-C
            if "hc" not in skip:
                if not ((H[i] - C[i]) >= hc_min * a): continue
            
            # 6. HC+HO
            if "hcho" not in skip:
                if not ((H[i]-C[i]) + (H[i]-O[i]) >= hcho_min * a): continue
            
            # 7. Pullback
            if "pullback" not in skip and pullback:
                minc15 = np.min(C[max(0,i-14):i+1])
                if not (minc15 < ema8[i]): continue
            
            # 8. Vol floor
            if "vol_floor" not in skip:
                if not (V[i] > vol_floor * avgv[i]): continue
            
            # 9. Vol cap
            if "vol_cap" not in skip:
                if not (V[i] < vol_cap * avgv[i]): continue
            
            # 10. Up bars
            if "up5" not in skip:
                up = sum(1 for j in range(1,6) if i-j>=0 and C[i-j+1]>C[i-j])
                if not (up >= up5_min): continue
            
            # 11. H - MINL65
            if "h_minl65" not in skip:
                minl65 = np.min(L[max(0,i-64):i+1])
                if not ((H[i] - minl65) > h_minl65_min * a): continue
            
            # 12. C - MINL65
            if "c_minl65" not in skip:
                if 'minl65' not in dir(): minl65 = np.min(L[max(0,i-64):i+1])
                if not ((C[i] - minl65) > c_minl65_min * a): continue
            
            signals.append({"ticker": ticker, "date": dates[i], "close": round(float(C[i]),2)})
            break  # one signal per ticker is enough for counting
    
    db.close()
    
    ticker_list = [s["ticker"] for s in signals]
    return {
        "count": len(signals),
        "days_scanned": days,
        "thresholds": {
            "slope_min": slope_min, "range20_min": range20_min,
            "near_high_max": near_high_max, "candle_min": candle_min,
            "hc_min": hc_min, "hcho_min": hcho_min,
            "pullback": pullback, "vol_floor": vol_floor, "vol_cap": vol_cap,
            "up5_min": up5_min, "h_minl65_min": h_minl65_min, "c_minl65_min": c_minl65_min,
            "skip": list(skip)
        },
        "tickers": ticker_list
    }


# ===========================================================================
# ANALYSIS ENGINE ENDPOINTS
# ===========================================================================

# Initialize analysis tables on import
try:
    from scripts.analysis_api import (
        init_analysis_tables, run_profiling, run_discovery,
        run_outcomes, run_optimization, run_full_pipeline,
        _get_status
    )
    init_analysis_tables()
except Exception as _init_err:
    print(f"Analysis tables init note: {_init_err}")


@app.post("/api/analysis/profile/{setup_type}")
async def start_profiling(setup_type: str, background_tasks: BackgroundTasks,
                          universe_n: int = Query(500)):
    """Run profiling engine on all examples + universe sample. Runs in background."""
    background_tasks.add_task(run_profiling, setup_type, universe_n)
    return {
        "status": "started",
        "setup_type": setup_type,
        "universe_n": universe_n,
        "message": f"Profiling {setup_type}. Check GET /api/analysis/status/{setup_type}/profiling"
    }


@app.post("/api/analysis/discover/{setup_type}")
async def start_discovery(setup_type: str, background_tasks: BackgroundTasks):
    """Run discovery engine on stored profiling data. Requires profiling first."""
    background_tasks.add_task(run_discovery, setup_type)
    return {
        "status": "started",
        "setup_type": setup_type,
        "message": f"Discovery for {setup_type}. Check GET /api/analysis/status/{setup_type}/discovery"
    }


@app.post("/api/analysis/outcomes/{setup_type}")
async def start_outcomes(setup_type: str, background_tasks: BackgroundTasks,
                         source: str = Query("examples"),
                         limit: int = Query(None)):
    """Compute forward outcomes. source=examples or source=backtest."""
    background_tasks.add_task(run_outcomes, setup_type, source, limit)
    return {
        "status": "started",
        "setup_type": setup_type,
        "source": source,
        "message": f"Computing {source} outcomes for {setup_type}. Check GET /api/analysis/status/{setup_type}/outcomes"
    }


@app.post("/api/analysis/optimize/{setup_type}")
async def start_optimization(setup_type: str, background_tasks: BackgroundTasks,
                              mode: str = Query("quick"),
                              source: str = Query("examples")):
    """Run management optimizer. mode=quick (~8K combos) or mode=full (~3.6M combos)."""
    background_tasks.add_task(run_optimization, setup_type, mode, source)
    return {
        "status": "started",
        "setup_type": setup_type,
        "mode": mode,
        "message": f"Optimizing {setup_type} ({mode} mode). Check GET /api/analysis/status/{setup_type}/optimization"
    }


@app.post("/api/analysis/pipeline/{setup_type}")
async def start_pipeline(setup_type: str, background_tasks: BackgroundTasks,
                          universe_n: int = Query(500)):
    """Run full analysis pipeline (profile → discover → outcomes → optimize)."""
    background_tasks.add_task(run_full_pipeline, setup_type, universe_n)
    return {
        "status": "started",
        "setup_type": setup_type,
        "message": f"Full pipeline for {setup_type}. Check GET /api/analysis/status/{setup_type}/pipeline"
    }


# --- STATUS ENDPOINTS ---

@app.get("/api/analysis/status/{setup_type}/{engine}")
async def analysis_status(setup_type: str, engine: str):
    """Get status of a running or completed analysis job."""
    try:
        return _get_status(engine, setup_type)
    except Exception as e:
        return {"state": "idle", "error": str(e)}


@app.get("/api/analysis/status/{setup_type}")
async def analysis_status_all(setup_type: str):
    """Get status of all engines for a setup type."""
    engines = ["profiling", "discovery", "outcomes", "optimization", "pipeline"]
    result = {}
    for eng in engines:
        try:
            result[eng] = _get_status(eng, setup_type)
        except Exception:
            result[eng] = {"state": "idle"}
    return result


# --- RESULTS ENDPOINTS ---

@app.get("/api/analysis/profiling/{setup_type}")
async def profiling_results(setup_type: str, examples_only: bool = Query(False)):
    """Get stored profiling results."""
    try:
        with get_db() as db_conn:
            where = f"WHERE setup_type='{setup_type}'"
            if examples_only:
                where += " AND is_example=1"
            rows = db_conn.execute(
                f"SELECT ticker, entry_date, scan_date, is_example, computed_at "
                f"FROM analysis_profiling {where} ORDER BY is_example DESC, ticker"
            ).fetchall()
            return {
                "count": len(rows),
                "results": [dict(r) for r in rows]
            }
    except Exception as e:
        return {"count": 0, "results": [], "error": str(e)}


@app.get("/api/analysis/profiling/{setup_type}/{ticker}")
async def profiling_detail(setup_type: str, ticker: str):
    """Get full profiling measurements for a specific ticker."""
    try:
        with get_db() as db_conn:
            row = db_conn.execute(
                "SELECT * FROM analysis_profiling "
                "WHERE setup_type=? AND ticker=? AND is_example=1 LIMIT 1",
                (setup_type, ticker)
            ).fetchone()
            if not row:
                raise HTTPException(404, f"No profiling data for {ticker}")
            d = dict(row)
            d['measurements'] = json.loads(d['measurements_json'])
            del d['measurements_json']
            return d
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/analysis/discovery/{setup_type}")
async def discovery_results(setup_type: str, n: int = Query(50)):
    """Get top discovery features ranked by combined score."""
    try:
        with get_db() as db_conn:
            # Get meta
            meta = db_conn.execute(
                "SELECT * FROM analysis_discovery_meta WHERE setup_type=? "
                "ORDER BY computed_at DESC LIMIT 1",
                (setup_type,)
            ).fetchone()

            features = db_conn.execute(
                "SELECT * FROM analysis_discovery "
                "WHERE setup_type=? ORDER BY feature_rank LIMIT ?",
                (setup_type, n)
            ).fetchall()

            return {
                "meta": dict(meta) if meta else {},
                "count": len(features),
                "features": [dict(f) for f in features]
            }
    except Exception as e:
        return {"meta": {}, "count": 0, "features": [], "error": str(e)}


@app.get("/api/analysis/discovery/{setup_type}/concepts")
async def discovery_by_concept(setup_type: str):
    """Get discovery features grouped by TA concept."""
    try:
        with get_db() as db_conn:
            features = db_conn.execute(
                "SELECT * FROM analysis_discovery "
                "WHERE setup_type=? ORDER BY concept_group, feature_rank",
                (setup_type,)
            ).fetchall()

            grouped = {}
            for f in features:
                d = dict(f)
                concept = d.get('concept_group', 'other')
                grouped.setdefault(concept, []).append(d)

            return {
                "n_concepts": len(grouped),
                "concepts": grouped
            }
    except Exception as e:
        return {"n_concepts": 0, "concepts": {}, "error": str(e)}


@app.get("/api/analysis/outcomes/{setup_type}")
async def outcome_results(setup_type: str, source: str = Query("examples")):
    """Get outcome summary per signal."""
    try:
        with get_db() as db_conn:
            rows = db_conn.execute(
                "SELECT ticker, entry_date, direction, entry_price, scan_bar_atr, "
                "bars_available, MAX(mfe) as peak_mfe, MIN(mae) as peak_mae "
                "FROM signal_outcomes "
                "WHERE setup_type=? AND source=? "
                "GROUP BY ticker, entry_date "
                "ORDER BY ticker, entry_date",
                (setup_type, source)
            ).fetchall()

            results = [dict(r) for r in rows]

            # Summary stats
            if results:
                mfes = [r['peak_mfe'] for r in results if r['peak_mfe'] is not None]
                maes = [r['peak_mae'] for r in results if r['peak_mae'] is not None]
                return {
                    "count": len(results),
                    "summary": {
                        "median_mfe": round(float(np.median(mfes)), 2) if mfes else 0,
                        "median_mae": round(float(np.median(maes)), 2) if maes else 0,
                        "mean_mfe": round(float(np.mean(mfes)), 2) if mfes else 0,
                        "mean_mae": round(float(np.mean(maes)), 2) if maes else 0,
                    },
                    "signals": results
                }
            return {"count": 0, "signals": []}
    except Exception as e:
        return {"count": 0, "signals": [], "error": str(e)}


@app.get("/api/analysis/outcomes/{setup_type}/{ticker}")
async def outcome_detail(setup_type: str, ticker: str,
                          entry_date: str = Query(None)):
    """Get bar-by-bar outcome data for a specific signal."""
    try:
        with get_db() as db_conn:
            where = "WHERE setup_type=? AND ticker=?"
            params = [setup_type, ticker]
            if entry_date:
                where += " AND entry_date=?"
                params.append(entry_date)

            rows = db_conn.execute(
                f"SELECT * FROM signal_outcomes {where} ORDER BY entry_date, bar_num",
                params
            ).fetchall()

            return {
                "count": len(rows),
                "bars": [dict(r) for r in rows]
            }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/analysis/optimization/{setup_type}")
async def optimization_results(setup_type: str, n: int = Query(20)):
    """Get top optimization strategies."""
    try:
        with get_db() as db_conn:
            strategies = db_conn.execute(
                "SELECT * FROM analysis_optimization "
                "WHERE setup_type=? ORDER BY rank LIMIT ?",
                (setup_type, n)
            ).fetchall()

            plateaus = db_conn.execute(
                "SELECT * FROM analysis_plateaus "
                "WHERE setup_type=? ORDER BY plateau_rank",
                (setup_type,)
            ).fetchall()

            return {
                "strategies": {
                    "count": len(strategies),
                    "results": [dict(s) for s in strategies]
                },
                "plateaus": {
                    "count": len(plateaus),
                    "results": [dict(p) for p in plateaus]
                }
            }
    except Exception as e:
        return {"strategies": {"count": 0}, "plateaus": {"count": 0}, "error": str(e)}


# ============================================
# SETUP-SPECIFIC DATA (LSP, etc.)
# ============================================

@app.get("/api/setup-data/{setup_type}")
async def get_setup_data(setup_type: str):
    """Get setup-specific data (e.g. LSP prices for DTSS)."""
    import json as _json
    data_file = os.path.join("data", f"{setup_type}_lsp_data.json")
    if os.path.exists(data_file):
        with open(data_file) as f:
            return {"data": _json.load(f), "type": "lsp"}
    return {"data": [], "type": "none"}


class LSPEntry(BaseModel):
    ticker: str
    date: str
    price: float
    entry_date: str = ""
    example_id: int = 0


@app.put("/api/setup-data/{setup_type}/lsp")
async def save_lsp_data(setup_type: str, entries: list[LSPEntry]):
    """Save LSP data for a setup type."""
    import json as _json
    data_file = os.path.join("data", f"{setup_type}_lsp_data.json")
    data = [e.dict() for e in entries]
    with open(data_file, "w") as f:
        _json.dump(data, f, indent=2)
    return {"saved": len(data)}


@app.post("/api/setup-data/{setup_type}/lsp")
async def add_lsp_entry(setup_type: str, entry: LSPEntry):
    """Add a single LSP entry."""
    import json as _json
    data_file = os.path.join("data", f"{setup_type}_lsp_data.json")
    data = []
    if os.path.exists(data_file):
        with open(data_file) as f:
            data = _json.load(f)
    data.append(entry.dict())
    with open(data_file, "w") as f:
        _json.dump(data, f, indent=2)
    return {"saved": len(data)}


@app.delete("/api/setup-data/{setup_type}/lsp/{idx}")
async def delete_lsp_entry(setup_type: str, idx: int):
    """Delete an LSP entry by index."""
    import json as _json
    data_file = os.path.join("data", f"{setup_type}_lsp_data.json")
    if not os.path.exists(data_file):
        return {"error": "no data file"}
    with open(data_file) as f:
        data = _json.load(f)
    if idx < 0 or idx >= len(data):
        return {"error": "index out of range"}
    removed = data.pop(idx)
    with open(data_file, "w") as f:
        _json.dump(data, f, indent=2)
    return {"removed": removed, "remaining": len(data)}


# ═══════════════════════════════════════════════════════════
# GRINDER — Desktop agent job queue
# ═══════════════════════════════════════════════════════════

GRINDER_JOBS_FILE = os.path.join("data", "grinder_jobs.json")
GRINDER_RESULTS_FILE = os.path.join("data", "grinder_results.json")
GRINDER_AGENT_FILE = os.path.join("data", "grinder_agent.json")

import json as _grinder_json

def _load_grinder_json(path, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(path):
            with open(path) as f:
                return _grinder_json.load(f)
    except:
        pass
    return default

def _save_grinder_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        _grinder_json.dump(data, f, indent=2, default=str)


class GrinderJobRequest(BaseModel):
    setup_type: str = "dtss"
    grind_level: int = 3
    action: str = "grind"


@app.post("/api/grinder/jobs")
async def create_grinder_job(req: GrinderJobRequest):
    """Create a new grind job for the desktop agent."""
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    for j in jobs:
        if j.get("setup_type") == req.setup_type and j.get("status") == "pending":
            j["status"] = "cancelled"
    job_id = f"{req.setup_type}_{req.action}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job = {
        "job_id": job_id,
        "setup_type": req.setup_type,
        "grind_level": req.grind_level,
        "action": req.action,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "message": "",
        "progress": {"phase": "", "progress_pct": 0, "detail": ""},
    }
    jobs.append(job)
    _save_grinder_json(GRINDER_JOBS_FILE, jobs)
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/grinder/jobs/pending")
async def get_pending_jobs():
    """Get pending jobs for the desktop agent."""
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    pending = [j for j in jobs if j.get("status") == "pending"]
    for j in jobs:
        if j.get("status") == "pending":
            j["status"] = "claimed"
    _save_grinder_json(GRINDER_JOBS_FILE, jobs)
    return {"jobs": pending}


@app.post("/api/grinder/status")
async def update_job_status(request: Request):
    """Update job status from desktop agent."""
    body = await request.json()
    job_id = body.get("job_id")
    status = body.get("status")
    message = body.get("message", "")
    data = body.get("data")
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    for j in jobs:
        if j.get("job_id") == job_id:
            j["status"] = status
            j["message"] = message
            j["updated_at"] = datetime.now().isoformat()
            break
    _save_grinder_json(GRINDER_JOBS_FILE, jobs)
    if status == "complete" and data:
        results = _load_grinder_json(GRINDER_RESULTS_FILE, {})
        setup_type = data.get("setup_type", "unknown")
        results[setup_type] = data
        _save_grinder_json(GRINDER_RESULTS_FILE, results)
    return {"ok": True}


@app.post("/api/grinder/progress")
async def update_job_progress(request: Request):
    """Update job progress from desktop agent."""
    body = await request.json()
    job_id = body.get("job_id")
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    for j in jobs:
        if j.get("job_id") == job_id:
            j["progress"] = {
                "phase": body.get("phase", ""),
                "progress_pct": body.get("progress_pct", 0),
                "detail": body.get("detail", ""),
            }
            j["updated_at"] = datetime.now().isoformat()
            break
    _save_grinder_json(GRINDER_JOBS_FILE, jobs)
    return {"ok": True}


@app.get("/api/grinder/jobs/status")
async def get_job_status(setup_type: str = Query("dtss")):
    """Get current/latest job status for frontend polling."""
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    matching = [j for j in jobs if j.get("setup_type") == setup_type]
    if not matching:
        return {"status": "none", "job": None}
    latest = matching[-1]
    return {"status": latest.get("status"), "job": latest}


@app.post("/api/grinder/jobs/reset")
async def reset_grinder_jobs(setup_type: str = Query("dtss")):
    """Reset all stuck jobs for a setup type."""
    jobs = _load_grinder_json(GRINDER_JOBS_FILE, [])
    for j in jobs:
        if j.get("setup_type") == setup_type and j.get("status") in ("pending", "claimed", "running"):
            j["status"] = "cancelled"
    _save_grinder_json(GRINDER_JOBS_FILE, jobs)
    return {"status": "reset", "setup_type": setup_type}


@app.get("/api/grinder/results/{setup_type}")
async def get_grinder_results(setup_type: str):
    """Get grinder results for frontend display."""
    results = _load_grinder_json(GRINDER_RESULTS_FILE, {})
    if setup_type not in results:
        return {"status": "none", "results": None}
    return {"status": "ok", "results": results[setup_type]}


@app.post("/api/grinder/agent/register")
async def register_agent(request: Request):
    body = await request.json()
    _save_grinder_json(GRINDER_AGENT_FILE, body)
    return {"ok": True}


@app.post("/api/grinder/agent/heartbeat")
async def agent_heartbeat(request: Request):
    body = await request.json()
    agent = _load_grinder_json(GRINDER_AGENT_FILE, {})
    agent["last_heartbeat"] = body.get("timestamp", datetime.now().isoformat())
    agent["status"] = "online"
    _save_grinder_json(GRINDER_AGENT_FILE, agent)
    return {"ok": True}


@app.get("/api/grinder/agent/status")
async def get_agent_status():
    """Check if desktop agent is online."""
    agent = _load_grinder_json(GRINDER_AGENT_FILE, {})
    if not agent:
        return {"status": "unknown", "agent": None}
    last_hb = agent.get("last_heartbeat", "")
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00', '').replace('Z', ''))
            if (datetime.utcnow() - hb_time).total_seconds() > 20:
                agent["status"] = "offline"
        except:
            pass
    return {"status": agent.get("status", "unknown"), "agent": agent}


@app.post("/api/universe/insert-ohlcv")
async def insert_ohlcv(request: Request):
    """Insert OHLCV rows for a ticker. Also adds to tradable_universe and universe_tickers.
    Body: {ticker: str, rows: [{date, open, high, low, close, volume}, ...]}
    """
    body = await request.json()
    ticker = body.get("ticker", "").strip().upper()
    rows = body.get("rows", [])
    if not ticker or not rows:
        return {"error": "Need ticker and rows"}
    try:
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO universe_tickers (ticker, status, rows_stored) VALUES (?, 'done', 0)", (ticker,))
            db.execute("INSERT OR IGNORE INTO tradable_universe (ticker) VALUES (?)", (ticker,))
            ohlcv_rows = []
            for r in rows:
                ohlcv_rows.append((ticker, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]))
            db.executemany(
                "INSERT OR REPLACE INTO universe_ohlcv (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                ohlcv_rows
            )
            db.execute("UPDATE universe_tickers SET rows_stored=?, status='done' WHERE ticker=?", (len(ohlcv_rows), ticker))
            db.commit()
        return {"ok": True, "ticker": ticker, "rows_inserted": len(ohlcv_rows)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/analysis/grinder-results")
async def save_grinder_results(request: Request):
    """Save grinder results."""
    body = await request.json()
    setup_type = body.get("setup_type", "unknown")
    results = _load_grinder_json(GRINDER_RESULTS_FILE, {})
    results[setup_type] = body.get("results", body)
    _save_grinder_json(GRINDER_RESULTS_FILE, results)
    return {"ok": True, "setup_type": setup_type}


# ═══════════════════════════════════════════════════════════
# PIPELINE DASHBOARD — Remote job queue for desktop agent
# ═══════════════════════════════════════════════════════════

PIPELINE_FILE = os.path.join("data", "pipeline_state.json")
PIPELINE_LOGS_FILE = os.path.join("data", "pipeline_logs.json")

PIPELINE_STEPS = [
    {"id": "nightly", "name": "Nightly Refresh", "category": "data",
     "description": "Append new OHLCV bars, rebuild caches and matrix.",
     "prerequisites": [], "result_files": []},
    {"id": "optimal_samples", "name": "1. Optimal Samples", "category": "pipeline",
     "description": "Current validated optimal samples for this setup.",
     "prerequisites": [], "result_files": [], "is_manual": True},
    {"id": "signal_brute", "name": "2. Signal Brute Forcing", "category": "pipeline",
     "description": "Pyramid grinder + signal exit grinder (runs back-to-back).",
     "prerequisites": [], "result_files": ["data/pyramid_results_dtss.json", "data/signal_exit_grind/signal_exit_dtss.json"]},
    {"id": "sample_expansion", "name": "3. Sample Expansion", "category": "pipeline",
     "description": "Signal filter + chart vetting. Review signals, YES = new optimal sample, NO = reject.",
     "prerequisites": ["signal_brute"],
     "result_files": ["data/signal_filter/filtered_dtss.json"], "is_manual": True},
    {"id": "mfe_capture", "name": "4. MFE Capture", "category": "pipeline",
     "description": "Find best exit conditions for max MFE capture. Single or multi-stage.",
     "prerequisites": ["signal_brute"],
     "result_files": []},
    {"id": "market_grind", "name": "5. Market Grinder", "category": "pipeline",
     "description": "Cluster outcomes vs market regime. Find optimal conditions.",
     "prerequisites": ["signal_brute"],
     "result_files": []},
]


def _load_pipeline_state():
    return _load_grinder_json(PIPELINE_FILE, {"steps": {}, "jobs": []})


def _save_pipeline_state(state):
    _save_grinder_json(PIPELINE_FILE, state)


def _load_pipeline_logs():
    return _load_grinder_json(PIPELINE_LOGS_FILE, {})


def _save_pipeline_logs(logs):
    _save_grinder_json(PIPELINE_LOGS_FILE, logs)


@app.get("/api/pipeline/steps")
async def get_pipeline_steps():
    """Get all pipeline steps with current state."""
    state = _load_pipeline_state()

    # Clean up jobs for step IDs that no longer exist
    valid_ids = {s["id"] for s in PIPELINE_STEPS}
    if state.get("jobs"):
        state["jobs"] = [j for j in state["jobs"] if j.get("step_id") in valid_ids]
        _save_pipeline_state(state)
    agent = _load_grinder_json(GRINDER_AGENT_FILE, {})

    agent_status = "unknown"
    last_hb = agent.get("last_heartbeat", "")
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00', '').replace('Z', ''))
            age = (datetime.utcnow() - hb_time).total_seconds()
            agent_status = "online" if age < 20 else "offline"
        except:
            agent_status = "unknown"

    steps_out = []
    for step_def in PIPELINE_STEPS:
        step_state = state.get("steps", {}).get(step_def["id"], {
            "status": "pending", "started_at": None, "finished_at": None,
            "duration_s": None, "exit_code": None, "error": None,
            "result_summary": None,
        })

        can_run = True
        if any(j.get("status") in ("queued", "running", "claimed") for j in state.get("jobs", [])):
            can_run = False
        else:
            for prereq in step_def["prerequisites"]:
                prereq_state = state.get("steps", {}).get(prereq, {})
                if prereq_state.get("status") != "done":
                    can_run = False
                    break

        # Optimal Samples + Sample Expansion: compute live stats
        if step_def["id"] in ("optimal_samples", "sample_expansion"):
            try:
                vetting_path = VETTING_DATA_DIR / "vetting" / "vetting_dtss.json"
                filtered_path = VETTING_DATA_DIR / "signal_filter" / "filtered_dtss.json"
                decisions = {}
                if vetting_path.exists():
                    with open(vetting_path) as f:
                        decisions = json.load(f)
                n_total = 0
                if filtered_path.exists():
                    with open(filtered_path) as f:
                        n_total = len(json.load(f).get("signals", []))
                counts = {"yes": 0, "maybe": 0, "no": 0}
                for v in decisions.values():
                    vd = v.get("verdict", "")
                    if vd in counts:
                        counts[vd] += 1
                n_vetted = sum(counts.values())
                with get_db() as db:
                    n_examples = db.execute(
                        "SELECT COUNT(*) FROM examples WHERE setup_type='dtss'"
                    ).fetchone()[0]
                    n_rejected = db.execute(
                        "SELECT COUNT(*) FROM rejected_signals WHERE setup_type='dtss'"
                    ).fetchone()[0]
                step_state["vetting_stats"] = {
                    "n_total": n_total, "n_vetted": n_vetted,
                    "n_yes": counts["yes"], "n_maybe": counts["maybe"], "n_no": counts["no"],
                    "n_examples": n_examples, "n_rejected": n_rejected,
                }
                if step_def["id"] == "sample_expansion" and n_vetted > 0:
                    # Show vetting progress, but don't override to 'running' — that
                    # conflicts with the Reload button which checks isFilterRunning.
                    # Only mark 'done' when all signals are vetted.
                    if n_vetted >= n_total:
                        step_state["status"] = "done"
                    step_state["result_summary"] = f"{n_vetted}/{n_total} vetted · {counts['yes']} yes · {counts['no']} no · {n_examples} total optimal samples"
            except:
                pass

        steps_out.append({**step_def, "state": step_state, "can_run": can_run})

    running = None
    for j in state.get("jobs", []):
        if j.get("status") in ("queued", "running", "claimed"):
            running = j.get("step_id")
            break

    return {
        "steps": steps_out, "running": running,
        "agent_status": agent_status, "agent_last_heartbeat": last_hb,
    }


@app.post("/api/pipeline/run/{step_id}")
async def pipeline_run_step(step_id: str, request: Request = None):
    """Queue a pipeline step for the desktop agent."""
    # Read optional params from request body (e.g. beam, depth, peak_target)
    step_params = {}
    if request:
        try:
            body = await request.json()
            if isinstance(body, dict):
                step_params = body.get("params", {})
        except:
            pass  # No body or not JSON

    step_def = next((s for s in PIPELINE_STEPS if s["id"] == step_id), None)
    if not step_def:
        return {"error": f"Unknown step: {step_id}"}

    state = _load_pipeline_state()

    # Remove ALL stale/dead jobs — if agent hasn't heartbeated in 30s, any
    # "running" job is dead. Just nuke everything that isn't actively alive.
    now = datetime.utcnow()
    agent = _load_grinder_json(GRINDER_AGENT_FILE, {})
    last_hb = agent.get("last_heartbeat", "")
    agent_alive = False
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00', '').replace('Z', ''))
            age = (now - hb_time).total_seconds()
            agent_alive = age < 30
        except:
            pass

    if not agent_alive:
        # Agent is dead — no job can be running. Clear everything.
        state["jobs"] = []
    else:
        # Agent is alive — only block if there's a job for a DIFFERENT step
        # that's actively running. If it's the SAME step, kill the old job
        # and let the new one take over.
        active_other = [j for j in state.get("jobs", [])
                        if j.get("status") in ("queued", "running", "claimed")
                        and j.get("step_id") != step_id]
        if active_other:
            return {"error": f"Already running: {active_other[0].get('step_id')}"}
        # Remove any old jobs for THIS step
        state["jobs"] = [j for j in state.get("jobs", [])
                         if j.get("step_id") != step_id]

    for prereq in step_def["prerequisites"]:
        prereq_state = state.get("steps", {}).get(prereq, {})
        if prereq_state.get("status") != "done":
            return {"error": f"Prerequisite not met: {prereq}"}

    job = {
        "job_id": f"pipe_{step_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "step_id": step_id, "status": "queued", "params": step_params,
        "created_at": datetime.now().isoformat(),
    }
    state.setdefault("jobs", []).append(job)
    state.setdefault("steps", {})[step_id] = {
        "status": "queued", "started_at": None, "finished_at": None,
        "duration_s": None, "exit_code": None, "error": None, "result_summary": None,
    }
    _save_pipeline_state(state)

    logs = _load_pipeline_logs()
    logs[step_id] = []
    _save_pipeline_logs(logs)

    return {"status": "queued", "job_id": job["job_id"], "step_id": step_id}


@app.get("/api/pipeline/jobs/pending")
async def pipeline_pending_jobs():
    """Agent polls this to pick up queued pipeline jobs."""
    state = _load_pipeline_state()
    pending = []
    for j in state.get("jobs", []):
        if j.get("status") == "queued":
            j["status"] = "claimed"
            pending.append(j)
    if pending:
        _save_pipeline_state(state)
    return {"jobs": pending}


@app.post("/api/pipeline/status")
async def pipeline_update_status(request: Request):
    """Agent reports step status updates."""
    body = await request.json()
    step_id = body.get("step_id")
    status = body.get("status")

    state = _load_pipeline_state()
    step_state = state.setdefault("steps", {}).setdefault(step_id, {})
    step_state["status"] = status

    if status == "running":
        step_state["started_at"] = body.get("timestamp", datetime.now().isoformat())
    elif status in ("done", "error", "stopped"):
        step_state["finished_at"] = body.get("timestamp", datetime.now().isoformat())
        step_state["duration_s"] = body.get("duration_s")
        step_state["exit_code"] = body.get("exit_code")
        step_state["error"] = body.get("error")
        step_state["result_summary"] = body.get("result_summary")
        for j in state.get("jobs", []):
            if j.get("step_id") == step_id and j.get("status") in ("claimed", "running"):
                j["status"] = status

    _save_pipeline_state(state)
    return {"ok": True}


@app.post("/api/pipeline/logs")
async def pipeline_append_logs(request: Request):
    """Agent streams log lines back."""
    body = await request.json()
    step_id = body.get("step_id")
    lines = body.get("lines", [])

    logs = _load_pipeline_logs()
    existing = logs.get(step_id, [])
    existing.extend(lines)
    if len(existing) > 5000:
        existing = existing[-4000:]
    logs[step_id] = existing
    _save_pipeline_logs(logs)
    return {"ok": True, "total_lines": len(existing)}


@app.get("/api/pipeline/logs/{step_id}")
async def pipeline_get_logs(step_id: str, after: int = 0):
    """Get log lines for a step. Use 'after' for polling (line index)."""
    logs = _load_pipeline_logs()
    all_lines = logs.get(step_id, [])
    return {"step_id": step_id, "lines": all_lines[after:], "total": len(all_lines), "after": after}


@app.post("/api/pipeline/reset/{step_id}")
async def pipeline_reset_step(step_id: str):
    """Reset a step to pending and clean up any associated jobs."""
    state = _load_pipeline_state()
    state.setdefault("steps", {})[step_id] = {
        "status": "pending", "started_at": None, "finished_at": None,
        "duration_s": None, "exit_code": None, "error": None, "result_summary": None,
    }
    # Remove ALL jobs for this step (prevents zombie jobs blocking future runs)
    state["jobs"] = [j for j in state.get("jobs", []) if j.get("step_id") != step_id]
    _save_pipeline_state(state)
    return {"ok": True, "step_id": step_id}


@app.post("/api/pipeline/stop")
async def pipeline_stop():
    """Request the agent to stop the current job."""
    state = _load_pipeline_state()
    for j in state.get("jobs", []):
        if j.get("status") in ("queued", "claimed", "running"):
            j["status"] = "stop_requested"
    _save_pipeline_state(state)
    return {"ok": True}


@app.get("/api/pipeline/stop-check/{step_id}")
async def pipeline_stop_check(step_id: str):
    """Agent polls this to check if stop was requested."""
    state = _load_pipeline_state()
    for j in state.get("jobs", []):
        if j.get("step_id") == step_id and j.get("status") == "stop_requested":
            return {"stop": True}
    return {"stop": False}


# ---------------------------------------------------------------------------
# VETTING — Signal filter results + chart vetting workflow
# ---------------------------------------------------------------------------

VETTING_DATA_DIR = Path("data")  # repo-local data dir (signal_filter output lives here)

@app.post("/api/vetting/{setup_type}/upload-signals")
async def upload_vetting_signals(setup_type: str, request: Request):
    """Upload filtered signals JSON from desktop. No git required."""
    body = await request.json()
    out_dir = VETTING_DATA_DIR / "signal_filter"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"filtered_{setup_type}.json"
    with open(path, "w") as f:
        json.dump(body, f, indent=2, default=str)
    n = len(body.get("signals", []))
    return {"status": "ok", "path": str(path), "n_signals": n}

@app.post("/api/vetting/{setup_type}/upload-exit")
async def upload_vetting_exit(setup_type: str, request: Request):
    """Upload signal exit grind JSON from desktop. No git required."""
    body = await request.json()
    out_dir = VETTING_DATA_DIR / "signal_exit_grind"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"signal_exit_{setup_type}.json"
    with open(path, "w") as f:
        json.dump(body, f, indent=2, default=str)
    return {"status": "ok", "path": str(path)}

@app.get("/api/vetting/{setup_type}/signals")
async def get_vetting_signals(setup_type: str):
    """Load filtered signals for chart vetting. Ranked by move_adr descending."""
    path = VETTING_DATA_DIR / "signal_filter" / f"filtered_{setup_type}.json"
    if not path.exists():
        raise HTTPException(404, f"No filtered signals for {setup_type}. Run signal_filter.py first.")
    with open(path) as f:
        data = json.load(f)
    # Load existing vetting decisions
    vetting_path = VETTING_DATA_DIR / "vetting" / f"vetting_{setup_type}.json"
    decisions = {}
    if vetting_path.exists():
        with open(vetting_path) as f:
            decisions = json.load(f)
    signals = data.get("signals", [])

    # Load existing examples and exclude signals that duplicate them
    with get_db() as db:
        examples = db.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=?",
            (setup_type,)
        ).fetchall()
    example_dates = {}  # ticker -> set of entry dates as datetime
    for ex in examples:
        t, d = ex["ticker"], ex["entry_date"]
        if t not in example_dates:
            example_dates[t] = []
        try:
            example_dates[t].append(datetime.strptime(d, "%Y-%m-%d"))
        except:
            pass

    def is_example_dup(sig):
        t = sig.get("ticker", "")
        if t not in example_dates:
            return False
        try:
            sig_dt = datetime.strptime(sig["date"], "%Y-%m-%d")
        except:
            return False
        for ex_dt in example_dates[t]:
            if abs((sig_dt - ex_dt).days) <= 5:
                return True
        return False

    signals = [s for s in signals if not is_example_dup(s)]

    # Attach existing decisions
    for sig in signals:
        key = f"{sig['ticker']}_{sig['date']}"
        sig["verdict"] = decisions.get(key, {}).get("verdict")
        sig["entry_date"] = decisions.get(key, {}).get("entry_date")
    return {
        "setup_type": setup_type,
        "n_signals": len(signals),
        "exit_condition": data.get("exit_condition", ""),
        "min_adr_threshold": data.get("min_adr_threshold", 0),
        "signals": signals,
    }


@app.get("/api/vetting/{setup_type}/ohlcv/{ticker}")
async def get_vetting_ohlcv(setup_type: str, ticker: str,
                             signal_date: str = Query(...),
                             lookback: int = Query(120),
                             forward: int = Query(80)):
    """Fetch OHLCV centered on signal date for chart vetting."""
    with get_db() as db:
        rows = db.execute(
            "SELECT date, open, high, low, close, volume FROM universe_ohlcv "
            "WHERE ticker=? ORDER BY date",
            (ticker,)
        ).fetchall()
    if not rows:
        raise HTTPException(404, f"No OHLCV for {ticker}")
    all_data = [dict(r) for r in rows]
    dates = [r["date"] for r in all_data]
    try:
        sig_idx = dates.index(signal_date)
    except ValueError:
        # Find nearest date
        sig_idx = min(range(len(dates)), key=lambda i: abs(
            (datetime.strptime(dates[i], "%Y-%m-%d") - datetime.strptime(signal_date, "%Y-%m-%d")).days))
    start = max(0, sig_idx - lookback)
    end = min(len(all_data), sig_idx + forward)
    return {"ticker": ticker, "signal_date": signal_date, "data": all_data[start:end]}


class VettingDecision(BaseModel):
    ticker: str
    signal_date: str
    verdict: str  # "yes", "maybe", "no"
    entry_date: str = None  # required for "yes"


@app.post("/api/vetting/{setup_type}/decide")
async def save_vetting_decision(setup_type: str, req: VettingDecision):
    """Save a vetting decision for a signal."""
    if req.verdict not in ("yes", "maybe", "no"):
        raise HTTPException(400, "verdict must be yes/maybe/no")
    if req.verdict == "yes" and not req.entry_date:
        raise HTTPException(400, "entry_date required for yes verdict")

    vetting_dir = VETTING_DATA_DIR / "vetting"
    vetting_dir.mkdir(exist_ok=True)
    vetting_path = vetting_dir / f"vetting_{setup_type}.json"

    decisions = {}
    if vetting_path.exists():
        with open(vetting_path) as f:
            decisions = json.load(f)

    key = f"{req.ticker}_{req.signal_date}"
    decisions[key] = {
        "ticker": req.ticker,
        "signal_date": req.signal_date,
        "verdict": req.verdict,
        "entry_date": req.entry_date,
        "timestamp": datetime.now().isoformat(),
    }

    with open(vetting_path, "w") as f:
        json.dump(decisions, f, indent=2)

    # If yes, also create the example
    result = {"status": "saved", "verdict": req.verdict}
    if req.verdict == "yes":
        try:
            ohlcv_df = fetch_ohlcv(req.ticker, req.entry_date)
            if ohlcv_df is not None:
                with get_db() as db:
                    existing = db.execute(
                        "SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                        (setup_type, req.ticker, req.entry_date)
                    ).fetchone()
                    if not existing:
                        eid = db.execute(
                            "INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                            (setup_type, req.ticker, req.entry_date, req.entry_date)
                        ).lastrowid
                        store_ohlcv(db, eid, ohlcv_df)
                        result["example_id"] = eid
                        result["message"] = f"Example created: {req.ticker} {req.entry_date}"
                    else:
                        result["message"] = f"Example already exists: {req.ticker} {req.entry_date}"
        except Exception as e:
            result["example_error"] = str(e)

    elif req.verdict == "no":
        try:
            with get_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO rejected_signals (setup_type, ticker, signal_date) VALUES (?,?,?)",
                    (setup_type, req.ticker, req.signal_date)
                )
            result["message"] = f"Rejected: {req.ticker} {req.signal_date}"
        except Exception as e:
            result["reject_error"] = str(e)

    return result


@app.get("/api/vetting/earnings/{ticker}")
async def get_earnings_dates(ticker: str):
    """Fetch earnings report dates from Yahoo Finance for chart overlay."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = t.get_earnings_dates(limit=40)
        if cal is None or cal.empty:
            return {"ticker": ticker, "earnings_dates": []}
        dates = [d.strftime("%Y-%m-%d") for d in cal.index]
        return {"ticker": ticker, "earnings_dates": dates}
    except Exception as e:
        return {"ticker": ticker, "earnings_dates": [], "error": str(e)}


@app.get("/api/vetting/{setup_type}/rejected")
async def get_rejected_signals(setup_type: str):
    """Get all rejected signals for a setup type."""
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, signal_date, created_at FROM rejected_signals WHERE setup_type=? ORDER BY created_at DESC",
            (setup_type,)
        ).fetchall()
    return {"setup_type": setup_type, "count": len(rows), "rejected": [dict(r) for r in rows]}


# Serve frontend (MUST be last - catches all routes)
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
