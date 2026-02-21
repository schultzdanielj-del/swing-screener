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

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
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
    """
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
    """, (now_iso,))

    count = db.execute("SELECT COUNT(*) FROM tradable_universe").fetchone()[0]
    db.commit()

    return count


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


# Serve frontend
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
