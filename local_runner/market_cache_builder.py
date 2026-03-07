"""
Market Context Cache Builder — Pre-compute expression series for all market instruments.

Fetches OHLCV for ~247 market instruments (indices, ETFs, futures, crypto, macro)
from yfinance, Stooq, and FRED, then applies the full expression library to each.

Output: local_runner/cache/market_series/
  - One .npz per instrument: {"data": float32 (n_bars, n_exprs), "dates": str array}
  - _manifest.json: instrument list, expression names, build timestamp

Price-only instruments (no volume): volume expressions are skipped, columns stay NaN.

Usage:
    # Full build (fetch + compute, ~15-30 min):
    python local_runner/market_cache_builder.py --build

    # Force rebuild:
    python local_runner/market_cache_builder.py --build --force

    # Nightly append (new bars only):
    python local_runner/market_cache_builder.py --append

    # Status check:
    python local_runner/market_cache_builder.py --status

Requires: expression library (local_runner/brute_expressions.py)
"""

import os
import sys
import time
import json
import hashlib
import argparse
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

warnings.filterwarnings("ignore")

LOCAL_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(LOCAL_DIR)
CACHE_DIR  = os.path.join(LOCAL_DIR, "cache")
MKT_DIR    = os.path.join(CACHE_DIR, "market_series")
MANIFEST   = os.path.join(MKT_DIR, "_manifest.json")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# INSTRUMENT REGISTRY
# ══════════════════════════════════════════════════════════════

# Instruments with no volume (price-only) — skip volume expressions
PRICE_ONLY = {
    "^VIX", "^VIX3M", "^VVIX", "^SKEW",
    "^TNX", "^TYX", "^FVX", "^IRX",
    "ES=F", "NQ=F", "RTY=F", "YM=F",
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    "CL=F", "GC=F", "SI=F", "HG=F",
    # Stooq breadth (values only, no volume)
    "$nymo", "$tick", "$nyadv", "$nydec", "$trin", "$nyhl", "$nahl", "$nyud",
    "$spxadp", "$ndxadp",
    # FRED series (values only)
    "FRED:BAMLH0A0HYM2", "FRED:BAMLC0A0CM",
    "FRED:T10Y2Y", "FRED:T10Y3M",
    "FRED:NFCI", "FRED:ANFCI",
    "FRED:DTWEXBGS", "FRED:DCOILWTICO",
}

# Full instrument manifest — grouped by category
INSTRUMENTS = {
    "broad_market": [
        "SPY", "QQQ", "IWM", "DIA",
    ],
    "breadth_participation": [
        "RSP", "QQEW", "QQQE", "EQAL", "MDY", "IJH", "IJR", "OEF", "QQQJ",
    ],
    "style_factor": [
        "IWF", "IWD", "IWO", "IWN", "RPV", "RFG",
        "MTUM", "QUAL", "VLUE", "SIZE", "USMV", "SPLV", "SPHB", "XMLV", "XSLV",
    ],
    "volatility": [
        "^VIX", "^VIX3M", "^VVIX", "^SKEW",
        "VIXM", "VXZ", "VXX", "UVXY", "VIXY", "SVXY", "SVOL",
    ],
    "index_futures": [
        "ES=F", "NQ=F", "RTY=F", "YM=F",
    ],
    "rates_yields": [
        "^TNX", "^TYX", "^FVX", "^IRX",
    ],
    "treasury_etfs": [
        "SHY", "IEF", "TLT", "TLH", "EDV", "ZROZ",
        "GOVT", "VGSH", "VGIT", "VGLT", "SCHO", "SCHR", "SCHQ",
        "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    ],
    "rates_vol_curve": [
        "IVOL", "PFIX", "TMF", "TMV", "TTT", "TBF",
    ],
    "tips_inflation": [
        "TIP", "VTIP", "STIP", "TIPX", "LTPZ", "RINF",
    ],
    "credit": [
        "HYG", "JNK", "LQD", "AGG", "BND", "MBB",
        "BKLN", "SRLN", "ANGL", "FALN", "SHYG", "HYXF", "HYDB",
        "EMB", "BNDX", "CWB", "ICVT",
    ],
    "risk_off": [
        "GLD", "SLV", "GC=F", "SI=F", "GDX", "GDXJ",
    ],
    "dollar": [
        "UUP",
    ],
    "oil_energy": [
        "USO", "BNO", "CL=F", "OIH", "XES", "XOP", "FCG", "GUSH", "DRIP",
        "AMLP", "MLPA",
    ],
    "commodities_broad": [
        "DBC", "PDBC", "COMT", "UNG", "BOIL",
    ],
    "metals_materials": [
        "CPER", "HG=F", "PALL", "PPLT", "LIT", "REMX", "XME", "PICK",
    ],
    "agriculture": [
        "WEAT", "CORN", "SOYB", "CANE", "MOO",
    ],
    "energy_transition": [
        "ICLN", "TAN", "FAN", "URA", "NLR",
    ],
    "sectors_spdr": [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
        "XLP", "XLB", "XLRE", "XLU", "XLC",
    ],
    "industry_groups": [
        "SMH", "SOXX", "KRE", "KBE", "IAI", "KBWB", "KBWR",
        "XBI", "IBB", "XHB", "ITB", "PKB",
        "IYT", "JETS", "XRT", "XPH", "IHI", "IHF", "PJP",
    ],
    "tech_thematic": [
        "IGV", "SKYY", "WCLD", "CLOU", "FINX", "IPAY",
        "ROBO", "BOTZ", "AIQ", "HACK", "CIBR",
    ],
    "real_estate": [
        "VNQ", "IYR", "REZ", "HOMZ", "MORT", "PFF", "PGX",
    ],
    "dividend_defensive": [
        "VYM", "DVY", "NOBL", "DGRO", "SDY", "SCHD",
    ],
    "global_macro": [
        "EEM", "EFA", "VWO", "IEMG",
        "EWJ", "EWZ", "EWG", "EWU", "EWY", "EWT",
        "EWA", "EWC", "EWH", "EWS", "EWL",
        "INDA", "INDY", "EPI", "FXI", "ASHR", "KWEB",
    ],
    "speculative_risk": [
        "ARKK", "BITO", "MARA", "RIOT", "WGMI", "GME", "BUZZ", "IPO", "FPX",
    ],
    "bitcoin": [
        "BTC-USD",
    ],
    "leveraged_sentiment": [
        "TQQQ", "UPRO", "TNA", "SPXU", "TZA", "SQQQ",
        "TECL", "LABU", "LABD", "NAIL", "FNGD",
    ],
    "managed_futures_trend": [
        "DBMF", "KMLM", "CTA", "WTMF",
    ],
    "risk_parity_balanced": [
        "AOR", "AOA", "AOK", "AOM",
    ],
    "tail_hedge": [
        "TAIL", "HDGE", "PUTW", "QYLD", "XYLD", "RYLD",
    ],
    "merger_arb": [
        "MNA",
    ],
    "closed_end_other": [
        "PCEF", "FTSD", "GURU",
    ],
    # Stooq — NYSE/NASDAQ breadth internals
    "breadth_internals_stooq": [
        "$nymo", "$tick", "$nyadv", "$nydec",
        "$trin", "$nyhl", "$nahl", "$nyud",
        "$spxadp", "$ndxadp",
    ],
    # FRED — macro series
    "macro_fred": [
        "FRED:BAMLH0A0HYM2",   # HY OAS spread
        "FRED:BAMLC0A0CM",     # IG OAS spread
        "FRED:T10Y2Y",         # 10yr-2yr yield spread
        "FRED:T10Y3M",         # 10yr-3mo yield spread
        "FRED:NFCI",           # National Financial Conditions Index
        "FRED:ANFCI",          # Adjusted NFCI
        "FRED:DTWEXBGS",       # Trade-weighted dollar broad
        "FRED:DCOILWTICO",     # WTI crude oil
    ],
}

def all_instruments():
    """Flat list of all instrument IDs."""
    result = []
    for syms in INSTRUMENTS.values():
        result.extend(syms)
    return result


# ══════════════════════════════════════════════════════════════
# FETCHERS
# ══════════════════════════════════════════════════════════════

def fetch_yfinance(symbol, period="5y"):
    """Fetch OHLCV via yfinance. Returns DataFrame with [date,open,high,low,close,volume]."""
    import yfinance as yf
    df = yf.download(symbol, period=period, progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        return None

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    df = df.reset_index()
    df = df.rename(columns={"Date": "date", "index": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

    df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    df = df[df["close"] > 0].reset_index(drop=True)
    df = df.sort_values("date").reset_index(drop=True)
    return df if len(df) >= 50 else None


def fetch_stooq(symbol):
    """Fetch daily data from Stooq. Symbol format: $nymo, $tick, etc."""
    import urllib.request
    import io
    # Stooq uses symbol as-is, lowercase
    sym = symbol.lower().lstrip("$")
    sym_stooq = f"${sym}"
    url = f"https://stooq.com/q/d/l/?s={sym_stooq}&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(raw))
        if df.empty or "No data" in raw:
            return None
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"date": "date"})
        df["date"] = pd.to_datetime(df["date"])
        # Stooq breadth series: open/high/low/close may all be same value (it's a single series)
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                # Use whatever numeric column is available
                num_cols = df.select_dtypes(include=[np.number]).columns
                if len(num_cols) > 0:
                    df[col] = df[num_cols[0]]
                else:
                    return None
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = 0.0
        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        df = df.sort_values("date").reset_index(drop=True)
        # Filter to last 5yr
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=5)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if len(df) >= 50 else None
    except Exception as e:
        return None


def fetch_fred(series_id):
    """Fetch daily data from FRED (no API key required for public series)."""
    import urllib.request
    import io
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(raw))
        if df.empty:
            return None
        df.columns = ["date", "value"]
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        # Treat as price-only OHLC = value for all
        df["open"] = df["value"]
        df["high"] = df["value"]
        df["low"] = df["value"]
        df["close"] = df["value"]
        df["volume"] = 0.0
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("date").reset_index(drop=True)
        # Filter to last 5yr
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=5)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if len(df) >= 50 else None
    except Exception as e:
        return None


def fetch_instrument(instrument_id):
    """Route to correct fetcher based on instrument_id prefix."""
    if instrument_id.startswith("FRED:"):
        return fetch_fred(instrument_id[5:])
    elif instrument_id.startswith("$"):
        return fetch_stooq(instrument_id)
    else:
        return fetch_yfinance(instrument_id)


# ══════════════════════════════════════════════════════════════
# EXPRESSION LIBRARY
# ══════════════════════════════════════════════════════════════

def load_expressions():
    """Load the expression library from brute_expressions.py."""
    from local_runner.brute_expressions import generate_all
    return generate_all()


def expr_fingerprint(expressions):
    raw = json.dumps([e["name"] for e in expressions], sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# Volume and LSP/algo ops that can't run on price-only or market instruments
VOLUME_OPS = {
    "high_volume_bar_pct", "cmf_slope", "cmf", "obv_slope",
    "rvol_continuous", "volume_price_divergence", "up_volume_ratio",
    "cumulative_rvol", "volume_ratio",
}
SKIP_CATEGORIES_PRICE_ONLY = {"volume_character", "volume_continuous"}
# LSP and algo lines are ticker-specific technical patterns — skip for all market instruments
SKIP_CATEGORIES_ALL = {"lsp", "algo_lines"}
# rs_vs_spy is relative to SPY as reference ticker — skip to avoid circular reference
SKIP_OPS_ALL = {"rs_vs_spy", "rs_vs_spy_slope"}


def filter_expressions(expressions, is_price_only):
    """Return (filtered_exprs, skip_mask) for this instrument type."""
    keep = []
    skip_mask = []
    for e in expressions:
        cat = e["category"]
        op  = e["compute"].get("op", "")
        if cat in SKIP_CATEGORIES_ALL or op in SKIP_OPS_ALL:
            skip_mask.append(True)
            continue
        if is_price_only and (cat in SKIP_CATEGORIES_PRICE_ONLY or op in VOLUME_OPS):
            skip_mask.append(True)
            continue
        skip_mask.append(False)
        keep.append(e)
    return keep, skip_mask


# ══════════════════════════════════════════════════════════════
# CACHE I/O
# ══════════════════════════════════════════════════════════════

def instrument_filename(instrument_id):
    """Safe filename for an instrument ID."""
    safe = instrument_id.replace("^", "caret_").replace("=", "eq_").replace(
        ":", "col_").replace("$", "dol_").replace("-", "dash_")
    return f"{safe}.npz"


def save_instrument_cache(instrument_id, dates, data):
    os.makedirs(MKT_DIR, exist_ok=True)
    path = os.path.join(MKT_DIR, instrument_filename(instrument_id))
    np.savez_compressed(path, data=data, dates=dates)


def load_instrument_cache(instrument_id):
    """Load cached data for one instrument. Returns (dates, data) or (None, None)."""
    path = os.path.join(MKT_DIR, instrument_filename(instrument_id))
    if not os.path.exists(path):
        return None, None
    with np.load(path, allow_pickle=True) as f:
        return f["dates"], f["data"]


def load_manifest():
    if not os.path.exists(MANIFEST):
        return None
    with open(MANIFEST) as f:
        return json.load(f)


def save_manifest(manifest):
    os.makedirs(MKT_DIR, exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)


# ══════════════════════════════════════════════════════════════
# COMPUTATION (reuses expr_cache_builder logic)
# ══════════════════════════════════════════════════════════════

def compute_instrument(instrument_id, df, expressions, is_price_only):
    """
    Compute all applicable expression series for one market instrument.
    Returns (dates_array, data_array) with shape (n_bars, n_all_exprs).
    Skipped expressions remain NaN.
    """
    from scripts.expression_engine import ExpressionEngine
    from scripts.backtest_conditions import compute_series
    from local_runner.expr_cache_builder import (
        resample_ohlcv, build_htf_to_daily_map, map_htf_series_to_daily
    )

    n_bars  = len(df)
    n_exprs = len(expressions)
    data    = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # Classify expression indices
    daily_indices      = []
    htf_weekly_indices = []
    htf_monthly_indices = []
    htf_weekly_base    = []
    htf_monthly_base   = []
    ext_struct_indices = []
    ext_series_name_to_idx = {}

    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    skip_cats = SKIP_CATEGORIES_ALL.copy()
    skip_ops  = SKIP_OPS_ALL.copy()
    if is_price_only:
        skip_cats |= SKIP_CATEGORIES_PRICE_ONLY
        skip_ops  |= VOLUME_OPS

    for j, expr in enumerate(expressions):
        cat    = expr["category"]
        compute = expr["compute"]
        op     = compute.get("op", "")

        if cat in skip_cats or op in skip_ops:
            continue  # stays NaN

        if op == "precomputed":
            source = compute.get("source")
            if source in ("lsp", "algo"):
                continue  # skip — market instruments don't have LSP/algo
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    htf_weekly_indices.append(j)
                    htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    htf_monthly_indices.append(j)
                    htf_monthly_base.append(compute.get("base_compute"))
        elif op in _ON_SERIES_OPS:
            ext_struct_indices.append(j)
        else:
            daily_indices.append(j)

    # Build name→idx for extension structure
    for j, expr in enumerate(expressions):
        if expr["name"] in ("ext_avgc50_adr14", "ext_avgc200_adr14"):
            ext_series_name_to_idx[expr["name"]] = j

    engine = ExpressionEngine(df)

    # 1. Daily expressions
    for j in daily_indices:
        try:
            series = compute_series(engine, expressions[j]["compute"])
            if series is not None:
                arr = np.asarray(series, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
                elif len(arr) < n_bars:
                    data[n_bars - len(arr):, j] = arr
        except:
            pass

    # 2. HTF weekly + monthly
    for tf_freq, tf_indices, tf_base in [
        ("W",  htf_weekly_indices,  htf_weekly_base),
        ("ME", htf_monthly_indices, htf_monthly_base),
    ]:
        if not tf_indices:
            continue
        htf_df = resample_ohlcv(df, tf_freq)
        if htf_df is None or len(htf_df) < 5:
            continue
        htf_map    = build_htf_to_daily_map(df["date"], htf_df, tf_freq)
        htf_engine = ExpressionEngine(htf_df)
        for k, j in enumerate(tf_indices):
            try:
                htf_series = compute_series(htf_engine, tf_base[k])
                if htf_series is not None:
                    htf_arr   = np.asarray(htf_series, dtype=np.float32)
                    daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                    data[:, j] = daily_arr
            except:
                pass

    # 3. Extension structure (second pass — needs ext_avgc50/200 already computed)
    if ext_struct_indices and ext_series_name_to_idx:
        from scripts.backtest_conditions import compute_on_series
        series_registry = {}
        for sname, sidx in ext_series_name_to_idx.items():
            col = data[:, sidx]
            if not np.all(np.isnan(col)):
                series_registry[sname] = col.astype(np.float64)

        if series_registry:
            for j in ext_struct_indices:
                try:
                    series = compute_series(
                        engine, expressions[j]["compute"],
                        series_registry=series_registry
                    )
                    if series is not None:
                        arr = np.asarray(series, dtype=np.float32)
                        if len(arr) == n_bars:
                            data[:, j] = arr
                except:
                    pass

    dates = df["date"].dt.strftime("%Y-%m-%d").values
    return dates, data


# ══════════════════════════════════════════════════════════════
# BUILD
# ══════════════════════════════════════════════════════════════

def build_full(force=False):
    print("\n" + "=" * 70)
    print("  MARKET CONTEXT CACHE — FULL BUILD")
    print("=" * 70)

    expressions = load_expressions()
    fingerprint = expr_fingerprint(expressions)
    print(f"\n  {len(expressions)} expressions (fingerprint: {fingerprint})")

    if not force:
        manifest = load_manifest()
        if manifest and manifest.get("fingerprint") == fingerprint:
            n = len(manifest.get("instruments", {}))
            print(f"\n  Cache is fresh ({n} instruments). Use --force to rebuild.")
            return manifest

    instruments = all_instruments()
    print(f"\n  {len(instruments)} instruments to fetch and compute")
    os.makedirs(MKT_DIR, exist_ok=True)

    t0 = time.time()
    instrument_info = {}
    failed = []

    for i, inst_id in enumerate(instruments):
        t_inst = time.time()
        is_price_only = inst_id in PRICE_ONLY

        # Fetch
        try:
            df = fetch_instrument(inst_id)
        except Exception as e:
            print(f"  [{i+1:3d}/{len(instruments)}] FETCH FAIL  {inst_id}: {e}")
            failed.append((inst_id, f"fetch: {e}"))
            continue

        if df is None:
            print(f"  [{i+1:3d}/{len(instruments)}] NO DATA     {inst_id}")
            failed.append((inst_id, "no data"))
            continue

        # Compute
        try:
            dates, data = compute_instrument(inst_id, df, expressions, is_price_only)
            save_instrument_cache(inst_id, dates, data)
            elapsed = time.time() - t_inst
            n_valid = int(np.sum(~np.isnan(data[-1])))  # non-NaN on last bar
            instrument_info[inst_id] = {
                "n_bars":    len(dates),
                "last_date": str(dates[-1]),
                "n_exprs_valid": n_valid,
                "price_only": is_price_only,
            }
            print(f"  [{i+1:3d}/{len(instruments)}] OK  {inst_id:20s} "
                  f"{len(dates)} bars  {n_valid}/{len(expressions)} exprs  {elapsed:.1f}s")
        except Exception as e:
            print(f"  [{i+1:3d}/{len(instruments)}] COMPUTE FAIL {inst_id}: {e}")
            failed.append((inst_id, f"compute: {e}"))

    total_time = time.time() - t0

    manifest = {
        "fingerprint":  fingerprint,
        "n_expressions": len(expressions),
        "expr_names":   [e["name"] for e in expressions],
        "n_instruments": len(instrument_info),
        "instruments":  instrument_info,
        "failed":       failed,
        "built_at":     datetime.now(timezone.utc).isoformat(),
        "build_time_s": round(total_time, 1),
    }
    save_manifest(manifest)

    # Disk usage
    total_bytes = sum(
        os.path.getsize(os.path.join(MKT_DIR, f))
        for f in os.listdir(MKT_DIR) if f.endswith(".npz")
    )

    print(f"\n  {'=' * 50}")
    print(f"  BUILD COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  Instruments cached: {len(instrument_info)}")
    print(f"  Failed: {len(failed)}")
    if failed:
        for inst_id, reason in failed[:10]:
            print(f"    ✗ {inst_id}: {reason}")
    print(f"  Disk: {total_bytes / 1024**3:.2f} GB")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Manifest: {MANIFEST}")
    return manifest


# ══════════════════════════════════════════════════════════════
# NIGHTLY APPEND
# ══════════════════════════════════════════════════════════════

def append_new_bars():
    """Fetch only new bars since last build and append to each instrument's cache."""
    print("\n" + "=" * 70)
    print("  MARKET CONTEXT CACHE — NIGHTLY APPEND")
    print("=" * 70)

    manifest = load_manifest()
    if not manifest:
        print("  No manifest found. Run --build first.")
        return

    expressions = load_expressions()
    fingerprint = expr_fingerprint(expressions)
    if manifest.get("fingerprint") != fingerprint:
        print("  Expression library changed. Run --build --force.")
        return

    instruments     = all_instruments()
    cached_info     = manifest.get("instruments", {})
    updated         = 0
    failed          = []

    for inst_id in instruments:
        is_price_only = inst_id in PRICE_ONLY
        existing_dates, existing_data = load_instrument_cache(inst_id)

        if existing_dates is None:
            # Not cached yet — do a full fetch for this instrument
            try:
                df = fetch_instrument(inst_id)
                if df is None:
                    continue
                dates, data = compute_instrument(inst_id, df, expressions, is_price_only)
                save_instrument_cache(inst_id, dates, data)
                cached_info[inst_id] = {
                    "n_bars": len(dates), "last_date": str(dates[-1]),
                    "n_exprs_valid": int(np.sum(~np.isnan(data[-1]))),
                    "price_only": is_price_only,
                }
                print(f"  NEW   {inst_id:20s} {len(dates)} bars")
                updated += 1
            except Exception as e:
                failed.append((inst_id, str(e)))
            continue

        last_cached = pd.Timestamp(existing_dates[-1])
        today       = pd.Timestamp.now().normalize()

        if last_cached >= today:
            continue  # already up to date

        # Fetch with a window that overlaps a bit for clean append
        try:
            df_new = fetch_yfinance(inst_id, period="1mo") if not inst_id.startswith(
                ("$", "FRED:")) else fetch_instrument(inst_id)
            if df_new is None:
                continue

            # Keep only truly new bars
            new_bars = df_new[df_new["date"] > last_cached].reset_index(drop=True)
            if len(new_bars) == 0:
                continue

            # We need some lookback for indicator computation — use last 500 bars
            lookback_date = pd.Timestamp(existing_dates[max(0, len(existing_dates)-500)])
            df_lookback = pd.DataFrame({
                "date":   pd.to_datetime(existing_dates[max(0, len(existing_dates)-500):]),
            })
            # Re-fetch full recent window for clean compute context
            df_full = fetch_instrument(inst_id)
            if df_full is None:
                continue

            dates_new, data_new = compute_instrument(inst_id, df_full, expressions, is_price_only)

            # Align with existing: keep existing up to last_cached, append new
            existing_date_set = set(existing_dates)
            new_mask = np.array([d not in existing_date_set for d in dates_new])

            if not np.any(new_mask):
                continue

            merged_dates = np.concatenate([existing_dates, dates_new[new_mask]])
            merged_data  = np.concatenate([existing_data,  data_new[new_mask]])

            save_instrument_cache(inst_id, merged_dates, merged_data)
            cached_info[inst_id]["n_bars"]    = len(merged_dates)
            cached_info[inst_id]["last_date"] = str(merged_dates[-1])
            n_added = int(np.sum(new_mask))
            print(f"  +{n_added:2d}   {inst_id:20s} → {len(merged_dates)} bars")
            updated += 1

        except Exception as e:
            failed.append((inst_id, str(e)))

    manifest["instruments"] = cached_info
    manifest["appended_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest)

    print(f"\n  Updated: {updated}  Failed: {len(failed)}")
    if failed:
        for inst_id, reason in failed[:5]:
            print(f"    ✗ {inst_id}: {reason}")


# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════

def print_status():
    manifest = load_manifest()
    if not manifest:
        print("No market cache found. Run --build first.")
        return

    print(f"\nMarket Context Cache Status")
    print(f"  Built:       {manifest.get('built_at', 'unknown')}")
    print(f"  Instruments: {manifest.get('n_instruments', 0)}")
    print(f"  Expressions: {manifest.get('n_expressions', 0)}")
    print(f"  Fingerprint: {manifest.get('fingerprint', 'unknown')}")

    instruments = manifest.get("instruments", {})
    failed      = manifest.get("failed", [])

    if instruments:
        last_dates = [v["last_date"] for v in instruments.values()]
        print(f"  Last bar:    {max(last_dates)}")

    if failed:
        print(f"\n  Failed ({len(failed)}):")
        for inst_id, reason in failed:
            print(f"    ✗ {inst_id}: {reason}")

    # Disk usage
    if os.path.exists(MKT_DIR):
        total = sum(
            os.path.getsize(os.path.join(MKT_DIR, f))
            for f in os.listdir(MKT_DIR) if f.endswith(".npz")
        )
        print(f"  Disk:        {total / 1024**2:.0f} MB")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Context Cache Builder")
    parser.add_argument("--build",  action="store_true", help="Build/rebuild cache")
    parser.add_argument("--append", action="store_true", help="Append new bars only")
    parser.add_argument("--force",  action="store_true", help="Force full rebuild")
    parser.add_argument("--status", action="store_true", help="Print cache status")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.build:
        build_full(force=args.force)
    elif args.append:
        append_new_bars()
    else:
        parser.print_help()
