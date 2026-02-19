"""ScanPerfect — FastAPI backend."""

import os
import json
import math
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="ScanPerfect API")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "schultzdanielj-del/swing-screener"


def git_push_data(message: str):
    """Commit and push data changes to GitHub so they persist across deploys."""
    if not GITHUB_TOKEN:
        print("GIT PUSH SKIP: No GITHUB_TOKEN set")
        return
    try:
        repo_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

        # Config
        subprocess.run(["git", "config", "user.email", "scanperfect@auto.dev"],
                       capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.name", "ScanPerfect"],
                       capture_output=True, timeout=10)

        # Stage all changes
        result = subprocess.run(["git", "add", "-A"], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"GIT ADD FAILED: {result.stderr}")
            return

        # Check if there's anything to commit
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if not status.stdout.strip():
            print(f"GIT PUSH SKIP: Nothing to commit for '{message}'")
            return

        # Commit
        result = subprocess.run(["git", "commit", "-m", message],
                               capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"GIT COMMIT FAILED: {result.stderr}")
            return

        # Push
        result = subprocess.run(["git", "push", repo_url, "main"],
                               capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"GIT PUSH FAILED: {result.stderr}")
            return

        print(f"GIT PUSH OK: {message}")
    except subprocess.TimeoutExpired:
        print(f"GIT PUSH TIMEOUT: {message}")
    except Exception as e:
        print(f"GIT PUSH ERROR: {e}")


class SaveExampleRequest(BaseModel):
    ticker: str
    chart_date: str  # YYYY-MM-DD
    entry_date: str  # YYYY-MM-DD

DATA_DIR = Path("data/ohlcv")
CHARTS_DIR = Path("data/charts")
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


def generate_chart_image(df: pd.DataFrame, ticker: str, entry_date: str,
                         setup_type: str, at_entry: bool = False) -> str:
    """Generate a D1 candlestick chart PNG using pure matplotlib."""
    charts_dir = CHARTS_DIR / setup_type
    charts_dir.mkdir(parents=True, exist_ok=True)

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    # Center on entry date: aim for 30 before, 30 after
    # If not enough data after, show what we have but keep total ~60
    entry_dt = pd.Timestamp(entry_date)
    entry_rows = df[df["Date"] == entry_dt]
    if entry_rows.empty:
        before = df[df["Date"] <= entry_dt]
        if before.empty:
            return None
        entry_idx = before.index[-1]
    else:
        entry_idx = entry_rows.index[0]

    if at_entry:
        # Show 50 candles before entry, nothing after, with padding
        want_before = min(50, entry_idx)
        start_idx = entry_idx - want_before
        chart_df = df.iloc[start_idx:entry_idx + 1].copy().reset_index(drop=True)
        entry_pos = want_before
        # Add 15% empty space to the right
        n = len(chart_df)
        empty_right = max(int(n * 0.18), 5)
        total_width = n + empty_right
    else:
        avail_after = len(df) - entry_idx - 1
        avail_before = entry_idx
        want_after = min(30, avail_after)
        want_before = min(30, avail_before)
        total = want_before + 1 + want_after
        if total < 60:
            extra = 60 - total
            if want_before < 30:
                want_after = min(want_after + extra, avail_after)
            else:
                want_before = min(want_before + extra, avail_before)
        start_idx = entry_idx - want_before
        end_idx = entry_idx + want_after + 1
        chart_df = df.iloc[start_idx:end_idx].copy().reset_index(drop=True)
        entry_pos = want_before
        n = len(chart_df)
        empty_right = 0
        total_width = n
    entry_pos = want_before  # position in chart_df

    if chart_df.empty:
        return None

    fig, (ax, ax_vol) = plt.subplots(2, 1, figsize=(8, 4), dpi=120,
                                      gridspec_kw={"height_ratios": [3, 1]},
                                      facecolor="#0a0e17")
    ax.set_facecolor("#0a0e17")
    ax_vol.set_facecolor("#0a0e17")

    n = len(chart_df)
    w = 0.6  # candle body width

    for i, row in chart_df.iterrows():
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        color = "#26A69A" if c >= o else "#EF5350"

        # Wick
        ax.plot([i, i], [l, h], color=color, linewidth=0.8)
        # Body
        body_bottom = min(o, c)
        body_height = max(abs(c - o), 0.001)
        ax.add_patch(Rectangle((i - w/2, body_bottom), w, body_height,
                                facecolor=color, edgecolor=color, linewidth=0.5))

        # Volume
        ax_vol.bar(i, row["Volume"], width=w, color=color, alpha=0.7)

    # MAs
    for period, ma_type, color, lw in [
        (8, "ema", "#ADD8E6", 1.0), (21, "ema", "#D2B48C", 1.0),
        (50, "sma", "#FFD700", 1.2), (200, "sma", "#FF0000", 1.5),
    ]:
        if n >= period:
            if ma_type == "ema":
                s = chart_df["Close"].ewm(span=period, adjust=False).mean()
            else:
                s = chart_df["Close"].rolling(window=period).mean()
            ax.plot(range(n), s.values, color=color, linewidth=lw, alpha=0.8)

    # Entry date crosshair at open price
    entry_open = float(chart_df.iloc[entry_pos]["Open"])
    ax.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax.axhline(y=entry_open, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax_vol.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")

    # Styling
    ax.set_title(f"{ticker}  •  {entry_date}", color="#e2e8f0", fontsize=11,
                 fontweight="bold", pad=8)
    ax.tick_params(colors="#64748b", labelsize=8)
    ax_vol.tick_params(colors="#64748b", labelsize=7)
    ax.spines[:].set_color("#2a3550")
    ax_vol.spines[:].set_color("#2a3550")
    ax.set_xlim(-1, total_width)
    ax_vol.set_xlim(-1, total_width)
    ax.set_xticks([])
    ax_vol.set_xticks([])
    ax_vol.yaxis.set_visible(False)
    ax.grid(True, alpha=0.1, color="#64748b")

    suffix = "_at_entry" if at_entry else ""
    filepath = str(charts_dir / f"{ticker}{suffix}.png")
    fig.tight_layout(pad=0.5)
    fig.savefig(filepath, facecolor="#0a0e17", bbox_inches="tight")
    plt.close(fig)
    return filepath


def clean_val(v):
    """Convert numpy/pandas types to JSON-safe Python types."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


@app.get("/api/debug/git-status")
async def debug_git_status():
    """Check git status and GITHUB_TOKEN availability on Railway."""
    has_token = bool(GITHUB_TOKEN)
    results = {"has_github_token": has_token}
    try:
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        results["git_status"] = status.stdout.strip() or "(clean)"
        results["git_status_rc"] = status.returncode
        results["git_status_err"] = status.stderr.strip() or None
    except Exception as e:
        results["git_status_error"] = str(e)
    try:
        remote = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True, timeout=10)
        # Mask the token in remote URL
        remote_out = remote.stdout.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else remote.stdout
        results["git_remote"] = remote_out.strip()
    except Exception as e:
        results["git_remote_error"] = str(e)
    try:
        log = subprocess.run(["git", "log", "--oneline", "-5"], capture_output=True, text=True, timeout=10)
        results["recent_commits"] = log.stdout.strip().split("\n") if log.stdout.strip() else []
    except Exception as e:
        results["git_log_error"] = str(e)
    # Check working directory
    results["cwd"] = os.getcwd()
    results["data_dir_exists"] = DATA_DIR.exists()
    return results


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

    # Load entry dates — this is the source of truth
    entry_file = data_dir / "entry_dates.json"
    if not entry_file.exists():
        return {"examples": []}
    entry_list = json.loads(entry_file.read_text())

    # Load signal analysis if available
    analysis_file = data_dir / "signal_day_analysis.json"
    analyses = {}
    if analysis_file.exists():
        for a in json.loads(analysis_file.read_text()):
            analyses[a["ticker"]] = a

    # Only show tickers with entry dates
    examples = []
    for e in sorted(entry_list, key=lambda x: x["ticker"]):
        ticker = e["ticker"]
        # Find matching CSV
        matches = list(data_dir.glob(f"{ticker}_*.csv"))
        csv_name = matches[0].name if matches else None
        chart_date = csv_name.split("_")[1].replace(".csv", "") if csv_name and "_" in csv_name else None
        examples.append({
            "ticker": ticker,
            "chartDate": chart_date,
            "entryDate": e.get("entry_date"),
            "hasAnalysis": ticker in analyses,
            "csvFile": csv_name,
        })

    return {"setupType": setup_type, "examples": examples}


@app.get("/api/ohlcv/local/{setup_type}/{ticker}")
async def get_local_ohlcv(setup_type: str, ticker: str):
    """Load OHLCV from saved CSV with indicators — no yfinance call."""
    ticker = ticker.upper().strip()
    data_dir = DATA_DIR / setup_type

    # Find CSV for this ticker
    matches = list(data_dir.glob(f"{ticker}_*.csv"))
    if not matches:
        raise HTTPException(404, f"No CSV for {ticker} in {setup_type}")

    raw = pd.read_csv(matches[0])
    raw["Date"] = pd.to_datetime(raw["Date"])
    raw = raw.sort_values("Date").reset_index(drop=True)
    raw = add_indicators(raw)

    # Return last 150 rows
    df = raw.tail(150)

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

    return {"ticker": ticker, "candles": candles}


@app.get("/api/chart-image/{setup_type}/{ticker}")
async def get_chart_image(
    setup_type: str, ticker: str,
    at_entry: int = Query(0, description="1 to show chart at entry time only"),
):
    """Serve a pre-generated chart image PNG."""
    ticker = ticker.upper().strip()
    if at_entry:
        img_path = CHARTS_DIR / setup_type / f"{ticker}_at_entry.png"
    else:
        img_path = CHARTS_DIR / setup_type / f"{ticker}.png"
    if not img_path.exists():
        raise HTTPException(404, f"No chart image for {ticker}")
    return FileResponse(str(img_path), media_type="image/png")


@app.get("/api/extension-chart/{setup_type}/{ticker}")
async def get_extension_chart(setup_type: str, ticker: str):
    """Serve a pre-generated extension analysis chart PNG."""
    ticker = ticker.upper().strip()
    img_path = Path("data/charts/extension") / setup_type / f"{ticker}.png"
    if not img_path.exists():
        raise HTTPException(404, f"No extension chart for {ticker}")
    return FileResponse(str(img_path), media_type="image/png")


@app.get("/api/extension-data/{setup_type}/{ticker}")
async def get_extension_data(
    setup_type: str, ticker: str,
    entry_date: str = Query(None, description="Entry date to center view around"),
):
    """Return extension CSV data as JSON, trimmed so entry is ~2/3 through."""
    ticker = ticker.upper().strip()
    csv_path = Path("data/extension") / setup_type / f"{ticker}.csv"
    if not csv_path.exists():
        raise HTTPException(404, f"No extension data for {ticker}")

    df = pd.read_csv(csv_path)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.dropna(subset=["ext_sma200_pct"]).reset_index(drop=True)

    # Trim so entry date sits at ~2/3 from left
    if entry_date:
        entry_dt = pd.Timestamp(entry_date)
        entry_rows = df[df["Date"] <= entry_dt]
        if not entry_rows.empty:
            entry_idx = entry_rows.index[-1]
            after_count = len(df) - entry_idx - 1
            # Want entry at 2/3: before = 2 * after
            before_count = min(after_count * 2, entry_idx)
            # But ensure minimum context: at least 200 rows before
            before_count = max(before_count, min(200, entry_idx))
            start_idx = entry_idx - before_count
            df = df.iloc[start_idx:].reset_index(drop=True)

    result = []
    for _, row in df.iterrows():
        result.append({
            "date": row["Date"].strftime("%Y-%m-%d"),
            "ext_sma50_pct": clean_val(row.get("ext_sma50_pct")),
            "ext_sma200_pct": clean_val(row.get("ext_sma200_pct")),
        })
    return result


@app.get("/api/conditions/{setup_type}")
async def get_conditions(setup_type: str):
    """Return PCF conditions for a setup type."""
    cond_file = SETUP_LIBRARY_DIR / setup_type / "conditions.json"
    if not cond_file.exists():
        raise HTTPException(404, f"No conditions found for {setup_type}")
    return json.loads(cond_file.read_text())


@app.delete("/api/examples/{setup_type}/{ticker}")
async def delete_example(setup_type: str, ticker: str):
    """Delete an example: remove CSV, charts, extension data, entry date, and analysis."""
    ticker = ticker.upper().strip()
    data_dir = DATA_DIR / setup_type
    if not data_dir.exists():
        raise HTTPException(404, f"No data directory for {setup_type}")

    # Delete CSV file(s) matching this ticker
    deleted_files = []
    for f in data_dir.glob(f"{ticker}_*.csv"):
        f.unlink()
        deleted_files.append(f.name)

    if not deleted_files:
        raise HTTPException(404, f"No CSV found for {ticker} in {setup_type}")

    # Remove from entry_dates.json
    entry_file = data_dir / "entry_dates.json"
    if entry_file.exists():
        entries = json.loads(entry_file.read_text())
        entries = [e for e in entries if e["ticker"] != ticker]
        entry_file.write_text(json.dumps(entries, indent=2))

    # Remove from signal_day_analysis.json
    analysis_file = data_dir / "signal_day_analysis.json"
    if analysis_file.exists():
        analyses = json.loads(analysis_file.read_text())
        analyses = [a for a in analyses if a["ticker"] != ticker]
        analysis_file.write_text(json.dumps(analyses, indent=2))

    # Remove D1 chart images
    for suffix in ["", "_at_entry"]:
        chart_img = CHARTS_DIR / setup_type / f"{ticker}{suffix}.png"
        if chart_img.exists():
            chart_img.unlink()
            deleted_files.append(f"charts/{ticker}{suffix}.png")

    # Remove extension chart image
    ext_chart = CHARTS_DIR / "extension" / setup_type / f"{ticker}.png"
    if ext_chart.exists():
        ext_chart.unlink()
        deleted_files.append(f"charts/extension/{ticker}.png")

    # Remove extension CSV data
    ext_dir = Path("data/extension") / setup_type
    ext_csv = ext_dir / f"{ticker}.csv"
    if ext_csv.exists():
        ext_csv.unlink()
        deleted_files.append(f"extension/{ticker}.csv")

    # Persist to git
    git_push_data(f"Delete {ticker} from {setup_type}")

    return {"status": "deleted", "ticker": ticker, "files": deleted_files}


class UpdateEntryRequest(BaseModel):
    entry_date: str  # YYYY-MM-DD


@app.patch("/api/examples/{setup_type}/{ticker}")
async def update_entry_date(setup_type: str, ticker: str, req: UpdateEntryRequest):
    """Update entry date for an example: re-run analysis and regenerate charts."""
    ticker = ticker.upper().strip()
    entry_date = req.entry_date
    data_dir = DATA_DIR / setup_type

    # Verify CSV exists
    matches = list(data_dir.glob(f"{ticker}_*.csv"))
    if not matches:
        raise HTTPException(404, f"No CSV for {ticker} in {setup_type}")

    # Update entry_dates.json
    entry_file = data_dir / "entry_dates.json"
    entries = []
    if entry_file.exists():
        entries = json.loads(entry_file.read_text())
    entries = [e for e in entries if e["ticker"] != ticker]
    entries.append({"ticker": ticker, "entry_date": entry_date})
    entries.sort(key=lambda x: x["ticker"])
    entry_file.write_text(json.dumps(entries, indent=2))

    # Re-run signal analysis
    raw = pd.read_csv(matches[0])
    analysis = None
    analysis_file = data_dir / "signal_day_analysis.json"
    analyses = []
    if analysis_file.exists():
        analyses = json.loads(analysis_file.read_text())
    try:
        analysis = run_signal_analysis(raw, ticker, entry_date)
        analyses = [a for a in analyses if a["ticker"] != ticker]
        analyses.append(analysis)
        analyses.sort(key=lambda x: x["ticker"])
        analysis_file.write_text(json.dumps(analyses, indent=2))
    except Exception as e:
        analysis = {"error": str(e)}

    # Regenerate chart images
    try:
        generate_chart_image(raw, ticker, entry_date, setup_type, at_entry=False)
        generate_chart_image(raw, ticker, entry_date, setup_type, at_entry=True)
    except Exception as e:
        print(f"Chart regen failed for {ticker}: {e}")

    # Persist to git
    git_push_data(f"Update {ticker} entry date to {entry_date}")

    return {
        "status": "updated",
        "ticker": ticker,
        "entryDate": entry_date,
        "analysis": analysis,
    }


def calc_sma_series(series: pd.Series, period: int) -> pd.Series:
    """SMA for analysis (same as calc_sma but named distinctly)."""
    return series.rolling(window=period, min_periods=period).mean()


def run_signal_analysis(df: pd.DataFrame, ticker: str, entry_date: str) -> dict:
    """
    Compute signal day analysis metrics for a given entry date.
    Signal date = trading day before entry date.
    Analysis is done on the signal date's data.
    """
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    # Add indicators
    df["EMA8"] = calc_ema(df["Close"], 8)
    df["EMA21"] = calc_ema(df["Close"], 21)
    df["SMA10"] = calc_sma_series(df["Close"], 10)
    df["SMA50"] = calc_sma_series(df["Close"], 50)
    df["SMA200"] = calc_sma_series(df["Close"], 200)
    df["ATR14"] = calc_atr(df, 14)
    df["VolAvg20"] = df["Volume"].rolling(20).mean()

    entry_dt = pd.Timestamp(entry_date)

    # Find entry index
    entry_idx = df.index[df["Date"] == entry_dt]
    if len(entry_idx) == 0:
        # Try closest date before
        before = df[df["Date"] <= entry_dt]
        if before.empty:
            raise ValueError(f"No data at or before entry date {entry_date}")
        entry_idx = [before.index[-1]]

    eidx = entry_idx[0]
    if eidx == 0:
        raise ValueError("Entry date is the first row, no signal date available")

    sig_idx = eidx - 1  # signal day = day before entry
    sig = df.iloc[sig_idx]

    c = float(sig["Close"])
    o = float(sig["Open"])
    h = float(sig["High"])
    l = float(sig["Low"])
    atr = float(sig["ATR14"]) if pd.notna(sig["ATR14"]) else None
    vol = float(sig["Volume"])
    vol_avg = float(sig["VolAvg20"]) if pd.notna(sig["VolAvg20"]) else None

    ema8 = float(sig["EMA8"]) if pd.notna(sig["EMA8"]) else None
    ema21 = float(sig["EMA21"]) if pd.notna(sig["EMA21"]) else None
    sma10 = float(sig["SMA10"]) if pd.notna(sig["SMA10"]) else None
    sma50 = float(sig["SMA50"]) if pd.notna(sig["SMA50"]) else None
    sma200 = float(sig["SMA200"]) if pd.notna(sig["SMA200"]) else None

    # Swing high in last 30 trading days
    lookback_30 = df.iloc[max(0, sig_idx - 30):sig_idx + 1]
    swing_high = float(lookback_30["High"].max())
    high_idx = lookback_30["High"].idxmax()
    days_from_high = sig_idx - high_idx

    # Pullback from swing high
    pullback_pct = round((swing_high - c) / swing_high * 100, 2)
    pullback_atr = round((swing_high - c) / atr, 2) if atr else None

    # Find pullback low between swing high and signal
    pullback_range = df.iloc[high_idx:sig_idx + 1]
    pullback_low = float(pullback_range["Low"].min())
    low_idx = pullback_range["Low"].idxmin()
    days_since_low = sig_idx - low_idx

    # Bounce from pullback low
    bounce_pct = round((c - pullback_low) / pullback_low * 100, 2)
    bounce_atr = round((c - pullback_low) / atr, 2) if atr else None

    # Green/up candle counts
    recent_5 = df.iloc[max(0, sig_idx - 4):sig_idx + 1]
    recent_3 = df.iloc[max(0, sig_idx - 2):sig_idx + 1]
    green_3 = int((recent_3["Close"] > recent_3["Open"]).sum())
    green_5 = int((recent_5["Close"] > recent_5["Open"]).sum())
    up_close_3 = int((recent_3["Close"] > recent_3["Close"].shift(1)).sum())
    up_close_5 = int((recent_5["Close"] > recent_5["Close"].shift(1)).sum())

    # Signal candle properties
    sig_is_green = c > o
    sig_body = abs(c - o)
    sig_range = h - l
    sig_close_position = round((c - l) / sig_range, 2) if sig_range > 0 else 0.5

    # SMA50 slope (5 day)
    sma50_slope = None
    if sig_idx >= 5 and pd.notna(sig["SMA50"]):
        sma50_5ago = df.iloc[sig_idx - 5]["SMA50"]
        if pd.notna(sma50_5ago) and sma50_5ago > 0:
            sma50_slope = round((float(sig["SMA50"]) - float(sma50_5ago)) / float(sma50_5ago) * 100, 3)

    # Pullback structure
    total_pullback_days = days_from_high
    down_days = int((pullback_range["Close"] < pullback_range["Open"]).sum())
    pct_down = round(down_days / max(total_pullback_days, 1) * 100, 1)

    # 20-day range %
    range_20 = df.iloc[max(0, sig_idx - 19):sig_idx + 1]
    r20_high = float(range_20["High"].max())
    r20_low = float(range_20["Low"].min())
    range_20d_pct = round((r20_high - r20_low) / r20_low * 100, 1) if r20_low > 0 else None

    # Up days in last 14
    recent_14 = df.iloc[max(0, sig_idx - 13):sig_idx + 1]
    up_days_14 = int((recent_14["Close"] > recent_14["Close"].shift(1)).sum())

    return {
        "ticker": ticker,
        "entry_date": entry_date,
        "signal_date": sig["Date"].strftime("%Y-%m-%d"),
        "close": round(c, 2),
        "atr14": round(atr, 4) if atr else None,
        "atr_pct": round(atr / c * 100, 2) if atr else None,
        "above_sma50": c > sma50 if sma50 else None,
        "above_sma200": c > sma200 if sma200 else None,
        "ema8_above_ema21": ema8 > ema21 if (ema8 and ema21) else None,
        "c_vs_ema8_pct": round((c - ema8) / ema8 * 100, 2) if ema8 else None,
        "c_vs_ema21_pct": round((c - ema21) / ema21 * 100, 2) if ema21 else None,
        "c_vs_sma10_pct": round((c - sma10) / sma10 * 100, 2) if sma10 else None,
        "c_vs_sma50_pct": round((c - sma50) / sma50 * 100, 2) if sma50 else None,
        "c_vs_ema8_atr": round((c - ema8) / atr, 2) if (ema8 and atr) else None,
        "c_vs_ema21_atr": round((c - ema21) / atr, 2) if (ema21 and atr) else None,
        "c_vs_sma50_atr": round((c - sma50) / atr, 2) if (sma50 and atr) else None,
        "sma50_slope_5d": sma50_slope,
        "swing_high_30d": round(swing_high, 2),
        "days_from_high": int(days_from_high),
        "pullback_pct": pullback_pct,
        "pullback_atr": pullback_atr,
        "pullback_low": round(pullback_low, 2),
        "days_since_low": int(days_since_low),
        "bounce_from_low_pct": bounce_pct,
        "bounce_from_low_atr": bounce_atr,
        "green_candles_3d": green_3,
        "green_candles_5d": green_5,
        "up_close_3d": up_close_3,
        "up_close_5d": up_close_5,
        "sig_is_green": sig_is_green,
        "sig_close_position": sig_close_position,
        "sig_body_atr": round(sig_body / atr, 2) if atr else None,
        "sig_range_atr": round(sig_range / atr, 2) if atr else None,
        "sig_vol_vs_20avg": round(vol / vol_avg, 2) if vol_avg else None,
        "total_pullback_days": int(total_pullback_days),
        "down_days_in_pullback": down_days,
        "pct_down_in_pullback": pct_down,
        "range_20d_pct": range_20d_pct,
        "up_days_14": up_days_14,
    }


@app.post("/api/examples/{setup_type}")
async def save_example(setup_type: str, req: SaveExampleRequest):
    """Save a new example: fetch OHLCV, store CSV, entry date, and run analysis."""
    ticker = req.ticker.upper().strip()
    chart_date = req.chart_date
    entry_date = req.entry_date

    # Validate dates
    try:
        chart_dt = datetime.strptime(chart_date, "%Y-%m-%d")
        entry_dt = datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")

    data_dir = DATA_DIR / setup_type
    data_dir.mkdir(parents=True, exist_ok=True)

    # Fetch OHLCV (6 months before + 15 days after chart date)
    start = chart_dt - timedelta(days=250)
    end = chart_dt + timedelta(days=60)

    try:
        raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"), progress=False)
    except Exception as e:
        raise HTTPException(500, f"yfinance error: {e}")

    if raw.empty:
        raise HTTPException(404, f"No data found for {ticker}")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = raw.reset_index()
    raw["Date"] = pd.to_datetime(raw["Date"])
    raw = raw.sort_values("Date").reset_index(drop=True)

    # Save CSV
    csv_name = f"{ticker}_{chart_date}.csv"
    csv_path = data_dir / csv_name
    raw.to_csv(csv_path, index=False)

    # Update entry_dates.json
    entry_file = data_dir / "entry_dates.json"
    entries = []
    if entry_file.exists():
        entries = json.loads(entry_file.read_text())

    # Remove existing entry for this ticker if present
    entries = [e for e in entries if e["ticker"] != ticker]
    entries.append({
        "ticker": ticker,
        "chart_date": chart_date,
        "entry_date": entry_date,
    })
    entries.sort(key=lambda x: x["ticker"])
    entry_file.write_text(json.dumps(entries, indent=2))

    # Run signal analysis
    analysis = None
    analysis_file = data_dir / "signal_day_analysis.json"
    analyses = []
    if analysis_file.exists():
        analyses = json.loads(analysis_file.read_text())

    try:
        analysis = run_signal_analysis(raw, ticker, entry_date)
        # Remove existing analysis for this ticker
        analyses = [a for a in analyses if a["ticker"] != ticker]
        analyses.append(analysis)
        analyses.sort(key=lambda x: x["ticker"])
        analysis_file.write_text(json.dumps(analyses, indent=2))
    except Exception as e:
        # Save entry date even if analysis fails
        analysis = {"error": str(e)}

    # Generate chart images (both views)
    chart_path = None
    try:
        chart_path = generate_chart_image(raw, ticker, entry_date, setup_type, at_entry=False)
        generate_chart_image(raw, ticker, entry_date, setup_type, at_entry=True)
    except Exception as e:
        print(f"Chart image generation failed for {ticker}: {e}")

    # Persist to git
    git_push_data(f"Add {ticker} to {setup_type}")

    return {
        "status": "saved",
        "ticker": ticker,
        "csvFile": csv_name,
        "entryDate": entry_date,
        "analysis": analysis,
        "hasChart": chart_path is not None,
    }


# Serve frontend
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
