"""Swing Screener — FastAPI backend."""

import os
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Swing Screener API")

DATA_DIR = Path("data/ohlcv")
SETUP_LIBRARY_DIR = Path("setup_library")


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate EMA manually for consistency."""
    k = 2 / (period + 1)
    ema = [series.iloc[0]]
    for i in range(1, len(series)):
        ema.append(series.iloc[i] * k + ema[-1] * (1 - k))
    return pd.Series(ema, index=series.index)


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add MAs, ATR, volume avg to dataframe."""
    df = df.copy()
    df["EMA8"] = calc_ema(df["Close"], 8)
    df["EMA21"] = calc_ema(df["Close"], 21)
    df["SMA50"] = calc_sma(df["Close"], 50)
    df["SMA200"] = calc_sma(df["Close"], 200)
    df["ATR14"] = calc_atr(df, 14)
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    return df


def clean_val(v):
    """Convert numpy/pandas types to JSON-safe Python types."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


@app.get("/api/ohlcv")
async def get_ohlcv(
    ticker: str = Query(..., description="Stock ticker symbol"),
    date: str = Query(None, description="Chart date (YYYY-MM-DD), defaults to today"),
    lookback: int = Query(150, description="Trading days of history to fetch"),
):
    """Fetch OHLCV data with indicators for a ticker."""
    ticker = ticker.upper().strip()

    if date:
        try:
            chart_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    else:
        chart_date = datetime.now()

    # Fetch extra days to warm up indicators (need ~200 for SMA200)
    start = chart_date - timedelta(days=lookback + 250)
    end = chart_date + timedelta(days=20)

    try:
        raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"), progress=False)
    except Exception as e:
        raise HTTPException(500, f"yfinance error: {e}")

    if raw.empty:
        raise HTTPException(404, f"No data found for {ticker}")

    # Handle MultiIndex columns from yfinance
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = raw.reset_index()
    raw["Date"] = pd.to_datetime(raw["Date"])
    raw = raw.sort_values("Date").reset_index(drop=True)

    # Add indicators on full history
    raw = add_indicators(raw)

    # Trim to lookback window for response
    chart_dt = pd.Timestamp(chart_date)
    mask = raw["Date"] <= chart_dt + timedelta(days=20)
    df = raw[mask].tail(lookback + 20).copy()

    # Build response
    candles = []
    for _, row in df.iterrows():
        candles.append({
            "date": row["Date"].strftime("%Y-%m-%d"),
            "open": clean_val(row["Open"]),
            "high": clean_val(row["High"]),
            "low": clean_val(row["Low"]),
            "close": clean_val(row["Close"]),
            "volume": clean_val(row["Volume"]),
            "ema8": clean_val(row.get("EMA8")),
            "ema21": clean_val(row.get("EMA21")),
            "sma50": clean_val(row.get("SMA50")),
            "sma200": clean_val(row.get("SMA200")),
            "atr14": clean_val(row.get("ATR14")),
            "volAvg20": clean_val(row.get("VolAvg20")),
        })

    return {"ticker": ticker, "chartDate": date, "candles": candles}


@app.get("/api/setups")
async def get_setups():
    """Return available setup types and their metadata."""
    setups = {}
    if SETUP_LIBRARY_DIR.exists():
        for d in SETUP_LIBRARY_DIR.iterdir():
            if d.is_dir():
                desc_file = d / "description.md"
                cond_file = d / "conditions.json"
                setups[d.name] = {
                    "name": d.name.upper(),
                    "hasDescription": desc_file.exists(),
                    "hasConditions": cond_file.exists(),
                }
    return setups


@app.get("/api/examples/{setup_type}")
async def get_examples(setup_type: str):
    """Return saved examples for a setup type."""
    data_dir = DATA_DIR / setup_type
    if not data_dir.exists():
        return {"examples": []}

    # Load entry dates if available
    entry_file = data_dir / "entry_dates.json"
    entries = {}
    if entry_file.exists():
        for e in json.loads(entry_file.read_text()):
            entries[e["ticker"]] = e

    # Load signal analysis if available
    analysis_file = data_dir / "signal_day_analysis.json"
    analyses = {}
    if analysis_file.exists():
        for a in json.loads(analysis_file.read_text()):
            analyses[a["ticker"]] = a

    # List CSV files
    examples = []
    for f in sorted(data_dir.glob("*.csv")):
        ticker = f.stem.split("_")[0]
        chart_date = f.stem.split("_")[1] if "_" in f.stem else None
        entry = entries.get(ticker, {})
        examples.append({
            "ticker": ticker,
            "chartDate": chart_date,
            "entryDate": entry.get("entry_date"),
            "hasAnalysis": ticker in analyses,
            "csvFile": f.name,
        })

    return {"setupType": setup_type, "examples": examples}


@app.get("/api/conditions/{setup_type}")
async def get_conditions(setup_type: str):
    """Return PCF conditions for a setup type."""
    cond_file = SETUP_LIBRARY_DIR / setup_type / "conditions.json"
    if not cond_file.exists():
        raise HTTPException(404, f"No conditions found for {setup_type}")
    return json.loads(cond_file.read_text())


# Serve frontend
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
