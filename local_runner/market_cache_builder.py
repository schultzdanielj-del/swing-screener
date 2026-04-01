"""
Market Context Cache Builder — Two-phase: fetch then compute.

PHASE 1 — FETCH  (network, threaded I/O parallelism)
  Downloads OHLCV for all market instruments from local pickle / EODHD / yfinance / FRED.
  Stores everything in a single pickle: local_runner/cache/market_ohlcv.pkl
  Zero computation in this phase (except derived breadth instruments).

  Data source priority:
    1. Local daily pickle (universe_ohlcv_daily.pkl) — ~227 US ETFs/stocks
    2. EODHD INDX/CC exchanges — indices (VIX, TNX, ...), crypto (BTC-USD),
       breadth internals (ADVN, DECN, TRIN, ...)
    3. yfinance — futures only (ES=F, NQ=F, CL=F, ...)
    4. FRED — macro series (yield spreads, credit spreads, NFCI, ...)

  After all fetches, derived instruments are computed from raw components:
    NYMO_CALC  = McClellan Oscillator = 19-EMA(ADVN-DECN) - 39-EMA(ADVN-DECN)
    NYUD_CALC  = NYSE up/down volume ratio = AVVN / DVCN
    NDXADP_CALC = NASDAQ A/D percent = (ADVQ-DECQ)/(ADVQ+DECQ+UNCQ)*100

PHASE 2 — COMPUTE  (CPU, ProcessPoolExecutor, EXPR_CACHE_WORKERS=8)
  Reads market_ohlcv.pkl — no network calls ever.
  Applies full 15,805-expression library to each instrument.
  Writes one .npz per instrument to local_runner/cache/market_series/
  Identical parallelism pattern to expr_cache_builder.py.

After initial build, nightly append:
  --append fetches only new bars (updates pickle), then recomputes affected instruments.
  The grinder always reads from local cache. Zero network at grind time.

Usage:
    python local_runner/market_cache_builder.py --fetch          # Phase 1 only
    python local_runner/market_cache_builder.py --compute        # Phase 2 only
    python local_runner/market_cache_builder.py --build          # Both phases
    python local_runner/market_cache_builder.py --build --force  # Force full rebuild
    python local_runner/market_cache_builder.py --append         # Nightly: new bars only
    python local_runner/market_cache_builder.py --status         # Cache status
"""

import os
import sys
import time
import json
import pickle
import hashlib
import argparse
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count

warnings.filterwarnings("ignore")

LOCAL_DIR     = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT     = os.path.dirname(LOCAL_DIR)
CACHE_DIR     = os.path.join(LOCAL_DIR, "cache")
OHLCV_PATH    = os.path.join(CACHE_DIR, "market_ohlcv.pkl")
DAILY_PKL     = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
MKT_DIR       = os.path.join(CACHE_DIR, "market_series")
MANIFEST_PATH = os.path.join(MKT_DIR, "_manifest.json")

HISTORY_START = "2016-01-01"

EODHD_API_TOKEN = os.environ.get("EODHD_API_TOKEN", "")
EODHD_BASE      = "https://eodhd.com/api"

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# INSTRUMENT REGISTRY
# ══════════════════════════════════════════════════════════════

# PRICE_ONLY instruments have no meaningful volume data.
# Phase 2 skips volume-based expressions for these.
# This also prevents them from being read from the daily pickle
# (even if the ticker exists there) — we want the index/futures
# close series, not the equity OHLCV.
PRICE_ONLY = {
    # Indices (EODHD INDX)
    "VIX.INDX", "VIX3M.INDX", "VVIX.INDX", "SKEW.INDX",
    "TNX.INDX", "TYX.INDX", "FVX.INDX", "IRX.INDX",
    # Futures (yfinance)
    "ES=F", "NQ=F", "RTY=F", "YM=F",
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    "CL=F", "GC=F", "SI=F", "HG=F",
    # Breadth internals — direct EODHD (close-only meaningful)
    "ADVN.INDX", "DECN.INDX", "HIGN.INDX", "LOWN.INDX", "TRIN.INDX",
    # Breadth internals — raw components for derived instruments
    "AVVN.INDX", "DVCN.INDX",
    "ADVQ.INDX", "DECQ.INDX", "UNCQ.INDX",
    # Breadth internals — computed
    "NYMO_CALC", "NYUD_CALC", "NDXADP_CALC",
    # FRED macro
    "FRED:BAMLH0A0HYM2", "FRED:BAMLC0A0CM",
    "FRED:T10Y2Y", "FRED:T10Y3M",
    "FRED:NFCI", "FRED:ANFCI",
    "FRED:DTWEXBGS", "FRED:DCOILWTICO",
}

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
        "VIX.INDX", "VIX3M.INDX", "VVIX.INDX", "SKEW.INDX",
        "VIXM", "VXZ", "VXX", "UVXY", "VIXY", "SVXY", "SVOL",
    ],
    "index_futures": [
        "ES=F", "NQ=F", "RTY=F", "YM=F",
    ],
    "rates_yields": [
        "TNX.INDX", "TYX.INDX", "FVX.INDX", "IRX.INDX",
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
        "BTC-USD.CC",
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
    "breadth_internals": [
        # Direct EODHD replacements
        "ADVN.INDX", "DECN.INDX", "HIGN.INDX", "LOWN.INDX", "TRIN.INDX",
        # Raw components (fetched, used to compute derived instruments)
        "AVVN.INDX", "DVCN.INDX",
        "ADVQ.INDX", "DECQ.INDX", "UNCQ.INDX",
        # Computed from raw components after fetch
        "NYMO_CALC", "NYUD_CALC", "NDXADP_CALC",
    ],
    "macro_fred": [
        "FRED:BAMLH0A0HYM2",
        "FRED:BAMLC0A0CM",
        "FRED:T10Y2Y",
        "FRED:T10Y3M",
        "FRED:NFCI",
        "FRED:ANFCI",
        "FRED:DTWEXBGS",
        "FRED:DCOILWTICO",
    ],
}

# Instruments that are computed from other fetched instruments (not fetched directly)
DERIVED_INSTRUMENTS = {"NYMO_CALC", "NYUD_CALC", "NDXADP_CALC"}


def all_instruments():
    result = []
    for syms in INSTRUMENTS.values():
        result.extend(syms)
    return result


def _is_pickle_instrument(symbol):
    """True if this instrument should be read from the daily OHLCV pickle.

    Pickle instruments are plain US ETFs/stocks that exist in
    universe_ohlcv_daily.pkl. Everything else uses a network source.
    """
    if symbol in PRICE_ONLY:
        return False
    if symbol in DERIVED_INSTRUMENTS:
        return False
    if symbol.endswith(".INDX") or symbol.endswith(".CC"):
        return False
    if symbol.startswith("FRED:"):
        return False
    if "=" in symbol:    # futures like ES=F
        return False
    # Plain ticker — should be in daily pickle
    return True


# ══════════════════════════════════════════════════════════════
# PHASE 1 — FETCHERS
# ══════════════════════════════════════════════════════════════

def _standard_df(df, min_bars=50):
    """Ensure standard columns, trim to HISTORY_START, validate min bars."""
    if df is None or len(df) == 0:
        return None
    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    cutoff = pd.Timestamp(HISTORY_START)
    df = df[df["date"] >= cutoff].sort_values("date").reset_index(drop=True)
    return df if len(df) >= min_bars else None


def _read_from_daily_pickle(symbol, daily_cache):
    """Read one ticker from the pre-loaded daily OHLCV pickle."""
    if daily_cache is None or symbol not in daily_cache:
        return None
    df = daily_cache[symbol].copy()
    # Daily pickle already has date/open/high/low/close/volume columns
    return _standard_df(df)


def _fetch_eodhd(eodhd_symbol):
    """Fetch OHLCV from EODHD for non-US-equity instruments.

    eodhd_symbol: full EODHD symbol like 'VIX.INDX' or 'BTC-USD.CC'
    The exchange suffix is already in the symbol name.
    """
    import urllib.request

    # EODHD API expects the symbol as the path, e.g. /eod/VIX.INDX
    end_date = datetime.now().strftime("%Y-%m-%d")
    url = (f"{EODHD_BASE}/eod/{eodhd_symbol}"
           f"?from={HISTORY_START}&to={end_date}"
           f"&period=d"
           f"&api_token={EODHD_API_TOKEN}&fmt=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ScanPerfect/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        if not raw.startswith("["):
            return None
        data = json.loads(raw)
        if not data:
            return None

        df = pd.DataFrame(data)
        # EODHD INDX instruments: adjusted_close == close (no splits),
        # but apply the adjustment pattern for consistency
        close_raw = pd.to_numeric(df["close"], errors="coerce")
        adj_close = pd.to_numeric(df["adjusted_close"], errors="coerce")
        ratio = np.where(close_raw > 0, adj_close / close_raw, 1.0)

        df["open"]   = pd.to_numeric(df["open"], errors="coerce") * ratio
        df["high"]   = pd.to_numeric(df["high"], errors="coerce") * ratio
        df["low"]    = pd.to_numeric(df["low"], errors="coerce") * ratio
        df["close"]  = adj_close
        df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0.0)
        df["date"]   = pd.to_datetime(df["date"])

        return _standard_df(df)
    except Exception:
        return None


def _fetch_yfinance(symbol):
    """Fetch OHLCV from yfinance. Used only for futures (ES=F, etc.)."""
    import yfinance as yf

    try:
        df = yf.download(
            symbol, start=HISTORY_START, progress=False, auto_adjust=True
        )
        if df is None or len(df) == 0:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0].lower() for col in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df = df.reset_index()
        df = df.rename(columns={"Date": "date"})
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                return None
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return _standard_df(df)
    except Exception:
        return None


def _fetch_fred(series_id):
    """Fetch macro series from FRED CSV endpoint."""
    import urllib.request, io

    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(raw))
        if df.empty:
            return None
        df.columns = ["date", "value"]
        df["date"]  = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        df["open"] = df["high"] = df["low"] = df["close"] = df["value"]
        df["volume"] = 0.0
        df = df[["date", "open", "high", "low", "close", "volume"]]
        return _standard_df(df)
    except Exception:
        return None


def _fetch_one(instrument_id, daily_cache=None):
    """Route an instrument to the correct fetcher.

    Returns (instrument_id, DataFrame_or_None).
    """
    try:
        if instrument_id in DERIVED_INSTRUMENTS:
            return instrument_id, None  # computed later, not fetched
        elif instrument_id.startswith("FRED:"):
            df = _fetch_fred(instrument_id[5:])
        elif instrument_id.endswith(".INDX") or instrument_id.endswith(".CC"):
            df = _fetch_eodhd(instrument_id)
        elif _is_pickle_instrument(instrument_id):
            df = _read_from_daily_pickle(instrument_id, daily_cache)
        else:
            # Futures and anything else — yfinance
            df = _fetch_yfinance(instrument_id)
        return instrument_id, df
    except Exception:
        return instrument_id, None


# ══════════════════════════════════════════════════════════════
# DERIVED INSTRUMENTS (computed from fetched components)
# ══════════════════════════════════════════════════════════════

def _compute_derived_instruments(results):
    """Compute derived breadth instruments from raw components.

    NYMO_CALC:   McClellan Oscillator = EMA19(A-D) - EMA39(A-D)
    NYUD_CALC:   NYSE up/down volume  = AVVN / DVCN
    NDXADP_CALC: NASDAQ A/D percent   = (ADVQ-DECQ)/(ADVQ+DECQ+UNCQ)*100

    Modifies results dict in place.
    """
    computed = 0

    # --- NYMO_CALC: McClellan Oscillator ---
    advn = results.get("ADVN.INDX")
    decn = results.get("DECN.INDX")
    if advn is not None and decn is not None:
        # Align on date
        merged = pd.merge(
            advn[["date", "close"]].rename(columns={"close": "adv"}),
            decn[["date", "close"]].rename(columns={"close": "dec"}),
            on="date", how="inner"
        ).sort_values("date").reset_index(drop=True)
        if len(merged) >= 50:
            ad_diff = merged["adv"] - merged["dec"]
            ema19 = ad_diff.ewm(span=19, adjust=False).mean()
            ema39 = ad_diff.ewm(span=39, adjust=False).mean()
            nymo = ema19 - ema39
            df = pd.DataFrame({
                "date": merged["date"],
                "open": nymo, "high": nymo, "low": nymo, "close": nymo,
                "volume": 0.0,
            })
            results["NYMO_CALC"] = df
            computed += 1

    # --- NYUD_CALC: NYSE up/down volume ratio ---
    avvn = results.get("AVVN.INDX")
    dvcn = results.get("DVCN.INDX")
    if avvn is not None and dvcn is not None:
        merged = pd.merge(
            avvn[["date", "close"]].rename(columns={"close": "up_vol"}),
            dvcn[["date", "close"]].rename(columns={"close": "dn_vol"}),
            on="date", how="inner"
        ).sort_values("date").reset_index(drop=True)
        if len(merged) >= 50:
            ratio = merged["up_vol"] / merged["dn_vol"].replace(0, np.nan)
            df = pd.DataFrame({
                "date": merged["date"],
                "open": ratio, "high": ratio, "low": ratio, "close": ratio,
                "volume": 0.0,
            })
            results["NYUD_CALC"] = df
            computed += 1

    # --- NDXADP_CALC: NASDAQ advance-decline percent ---
    advq = results.get("ADVQ.INDX")
    decq = results.get("DECQ.INDX")
    uncq = results.get("UNCQ.INDX")
    if advq is not None and decq is not None and uncq is not None:
        merged = advq[["date", "close"]].rename(columns={"close": "adv"})
        merged = pd.merge(merged,
                          decq[["date", "close"]].rename(columns={"close": "dec"}),
                          on="date", how="inner")
        merged = pd.merge(merged,
                          uncq[["date", "close"]].rename(columns={"close": "unc"}),
                          on="date", how="inner")
        merged = merged.sort_values("date").reset_index(drop=True)
        if len(merged) >= 50:
            total = merged["adv"] + merged["dec"] + merged["unc"]
            pct = ((merged["adv"] - merged["dec"]) / total.replace(0, np.nan)) * 100
            df = pd.DataFrame({
                "date": merged["date"],
                "open": pct, "high": pct, "low": pct, "close": pct,
                "volume": 0.0,
            })
            results["NDXADP_CALC"] = df
            computed += 1

    return computed


# ══════════════════════════════════════════════════════════════
# PHASE 1 — FETCH ALL
# ══════════════════════════════════════════════════════════════

def fetch_all(force=False, n_threads=16):
    print("\n" + "=" * 70)
    print("  MARKET CACHE — PHASE 1: FETCH")
    print("=" * 70)

    instruments = all_instruments()
    print(f"\n  {len(instruments)} instruments")

    if not force and os.path.exists(OHLCV_PATH):
        print(f"\n  market_ohlcv.pkl already exists. Use --force to re-fetch.")
        with open(OHLCV_PATH, "rb") as f:
            existing = pickle.load(f)
        print(f"  {len(existing)} instruments in cache.")
        return existing

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Load daily pickle for ETF/stock instruments
    daily_cache = None
    if os.path.exists(DAILY_PKL):
        print(f"\n  Loading daily pickle for ETF reads...")
        with open(DAILY_PKL, "rb") as f:
            daily_cache = pickle.load(f)
        print(f"  {len(daily_cache)} tickers in daily pickle")
    else:
        print(f"\n  WARNING: {DAILY_PKL} not found — ETF instruments will fail")

    # Split instruments into fetch paths
    pickle_insts  = []
    network_insts = []
    derived_insts = []

    for inst in instruments:
        if inst in DERIVED_INSTRUMENTS:
            derived_insts.append(inst)
        elif _is_pickle_instrument(inst):
            pickle_insts.append(inst)
        else:
            network_insts.append(inst)

    print(f"\n  Pickle reads:  {len(pickle_insts)}")
    print(f"  Network fetch: {len(network_insts)} "
          f"(EODHD + yfinance + FRED)")
    print(f"  Derived:       {len(derived_insts)} "
          f"(computed after fetch)")

    t0      = time.time()
    results = {}
    failed  = []

    # 1. Pickle reads (instant, no threading needed)
    print(f"\n  Reading {len(pickle_insts)} instruments from daily pickle...")
    for inst in pickle_insts:
        df = _read_from_daily_pickle(inst, daily_cache)
        if df is not None:
            results[inst] = df
        else:
            failed.append(inst)
            print(f"    MISS {inst} (not in daily pickle)")

    pickle_time = time.time() - t0
    print(f"  Pickle: {len(results)} OK, {len(failed)} missing  [{pickle_time:.1f}s]")

    # 2. Network fetches (threaded)
    print(f"\n  Fetching {len(network_insts)} instruments from network "
          f"({n_threads} threads)...")
    t1        = time.time()
    completed = 0

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {
            pool.submit(_fetch_one, inst, None): inst
            for inst in network_insts
        }
        for future in as_completed(futures):
            inst_id, df = future.result()
            completed += 1
            if df is not None:
                results[inst_id] = df
                print(f"    [{completed:3d}/{len(network_insts)}] OK   "
                      f"{inst_id:25s} {len(df)} bars  "
                      f"({df['date'].iloc[0].date()} – "
                      f"{df['date'].iloc[-1].date()})")
            else:
                failed.append(inst_id)
                print(f"    [{completed:3d}/{len(network_insts)}] FAIL "
                      f"{inst_id}")

    net_time = time.time() - t1
    print(f"  Network: {completed - len([f for f in failed if f in network_insts])} OK  "
          f"[{net_time:.1f}s]")

    # 3. Compute derived instruments
    print(f"\n  Computing derived instruments...")
    n_derived = _compute_derived_instruments(results)
    print(f"  Computed: {n_derived} derived instruments")

    elapsed = time.time() - t0

    with open(OHLCV_PATH, "wb") as f:
        pickle.dump(results, f, protocol=4)

    print(f"\n  {'=' * 50}")
    print(f"  FETCH COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  Fetched: {len(results)}   Failed: {len(failed)}")
    if failed:
        print(f"  Failed: {failed}")
    print(f"  Saved: {OHLCV_PATH}")
    print(f"  Time:  {elapsed:.0f}s")
    return results


# ══════════════════════════════════════════════════════════════
# PHASE 2 — EXPRESSION LIBRARY + SKIP RULES
# ══════════════════════════════════════════════════════════════

VOLUME_OPS = {
    "high_volume_bar_pct", "cmf_slope", "cmf", "obv_slope",
    "rvol_continuous", "volume_price_divergence", "up_volume_ratio",
    "cumulative_rvol", "volume_ratio",
}
SKIP_CATEGORIES_PRICE_ONLY = {"volume_character", "volume_continuous"}
SKIP_CATEGORIES_ALL        = {"lsp", "algo_lines"}
SKIP_OPS_ALL               = {"rs_vs_spy", "rs_vs_spy_slope"}


def _load_expressions():
    from local_runner.brute_expressions import generate_all
    return generate_all()


def _expr_fingerprint(expressions):
    raw = json.dumps([e["name"] for e in expressions], sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def instrument_filename(instrument_id):
    safe = (instrument_id
            .replace("^", "caret_")
            .replace("=", "eq_")
            .replace(":", "col_")
            .replace("$", "dol_")
            .replace("-", "dash_")
            .replace(".", "dot_"))
    return f"{safe}.npz"


# ══════════════════════════════════════════════════════════════
# PHASE 2 — WORKER  (subprocess, zero network)
# ══════════════════════════════════════════════════════════════

_w_expressions         = None
_w_daily_indices       = None
_w_htf_weekly_indices  = None
_w_htf_monthly_indices = None
_w_htf_weekly_base     = None
_w_htf_monthly_base    = None
_w_ext_struct_indices  = None
_w_ext_series_idx_map  = None


def _init_compute_worker(expressions):
    global _w_expressions, _w_daily_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base
    global _w_ext_struct_indices, _w_ext_series_idx_map

    _w_expressions         = expressions
    _w_daily_indices       = []
    _w_htf_weekly_indices  = []
    _w_htf_monthly_indices = []
    _w_htf_weekly_base     = []
    _w_htf_monthly_base    = []
    _w_ext_struct_indices  = []
    _w_ext_series_idx_map  = {}

    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    # Map extension series names to column indices
    for j, expr in enumerate(expressions):
        if expr["name"] in ("ext_avgc50_adr14", "ext_avgc200_adr14"):
            _w_ext_series_idx_map[expr["name"]] = j

    for j, expr in enumerate(expressions):
        cat     = expr["category"]
        compute = expr["compute"]
        op      = compute.get("op", "")

        if cat in SKIP_CATEGORIES_ALL or op in SKIP_OPS_ALL:
            continue

        if op == "precomputed":
            source = compute.get("source")
            if source in ("lsp", "algo"):
                continue
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    _w_htf_weekly_indices.append(j)
                    _w_htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    _w_htf_monthly_indices.append(j)
                    _w_htf_monthly_base.append(compute.get("base_compute"))
        elif op in _ON_SERIES_OPS:
            _w_ext_struct_indices.append(j)
        else:
            _w_daily_indices.append(j)


def _compute_one(args):
    """
    Compute all expressions for one instrument. No network calls.
    args: (instrument_id, df_dict, is_price_only)
    Returns: (instrument_id, dates_array, data_array) or (instrument_id, None, None)
    """
    instrument_id, df_dict, is_price_only = args

    try:
        from scripts.expression_engine import ExpressionEngine
        from scripts.backtest_conditions import compute_series
        from local_runner.expr_cache_builder import (
            resample_ohlcv, build_htf_to_daily_map, map_htf_series_to_daily
        )

        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).reset_index(drop=True)

        n_bars  = len(df)
        n_exprs = len(_w_expressions)
        if n_bars < 50:
            return (instrument_id, None, None)

        data   = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)
        engine = ExpressionEngine(df)

        # 1. Daily expressions
        for j in _w_daily_indices:
            expr = _w_expressions[j]
            if is_price_only:
                cat = expr["category"]
                op  = expr["compute"].get("op", "")
                if cat in SKIP_CATEGORIES_PRICE_ONLY or op in VOLUME_OPS:
                    continue
            try:
                series = compute_series(engine, expr["compute"])
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
            ("W",  _w_htf_weekly_indices,  _w_htf_weekly_base),
            ("ME", _w_htf_monthly_indices, _w_htf_monthly_base),
        ]:
            if not tf_indices:
                continue
            htf_df = resample_ohlcv(df, tf_freq)
            if htf_df is None or len(htf_df) < 5:
                continue
            htf_map    = build_htf_to_daily_map(df["date"], htf_df, tf_freq)
            htf_engine = ExpressionEngine(htf_df)
            for k, j in enumerate(tf_indices):
                expr = _w_expressions[j]
                if is_price_only:
                    base_op = tf_base[k].get("op", "") if tf_base[k] else ""
                    if (expr["category"] in SKIP_CATEGORIES_PRICE_ONLY
                            or base_op in VOLUME_OPS):
                        continue
                try:
                    htf_series = compute_series(htf_engine, tf_base[k])
                    if htf_series is not None:
                        htf_arr   = np.asarray(htf_series, dtype=np.float32)
                        daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                        data[:, j] = daily_arr
                except:
                    pass

        # 3. Extension structure (second pass — needs ext_avgc50/200 from step 1)
        if _w_ext_struct_indices and _w_ext_series_idx_map:
            from scripts.backtest_conditions import compute_series as cs
            series_registry = {}
            for sname, sidx in _w_ext_series_idx_map.items():
                col = data[:, sidx]
                if not np.all(np.isnan(col)):
                    series_registry[sname] = col.astype(np.float64)
            if series_registry:
                for j in _w_ext_struct_indices:
                    try:
                        series = cs(
                            engine, _w_expressions[j]["compute"],
                            series_registry=series_registry
                        )
                        if series is not None:
                            arr = np.asarray(series, dtype=np.float32)
                            if len(arr) == n_bars:
                                data[:, j] = arr
                    except:
                        pass

        dates = df["date"].dt.strftime("%Y-%m-%d").values
        return (instrument_id, dates, data)

    except Exception:
        return (instrument_id, None, None)


# ══════════════════════════════════════════════════════════════
# PHASE 2 — COMPUTE ALL  (parallel, CPU bound, zero network)
# ══════════════════════════════════════════════════════════════

def compute_all(ohlcv_cache=None, force=False):
    print("\n" + "=" * 70)
    print("  MARKET CACHE — PHASE 2: COMPUTE")
    print("=" * 70)

    if ohlcv_cache is None:
        if not os.path.exists(OHLCV_PATH):
            raise FileNotFoundError(
                f"market_ohlcv.pkl not found at {OHLCV_PATH}\n"
                "Run: python local_runner/market_cache_builder.py --fetch"
            )
        print(f"\n  Loading market_ohlcv.pkl...")
        with open(OHLCV_PATH, "rb") as f:
            ohlcv_cache = pickle.load(f)
    print(f"  {len(ohlcv_cache)} instruments in OHLCV cache")

    print("\n  Loading expressions...")
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)
    print(f"  {len(expressions)} expressions (fingerprint: {fingerprint})")

    if not force:
        manifest = load_manifest()
        if manifest and manifest.get("fingerprint") == fingerprint:
            n = len(manifest.get("instruments", {}))
            print(f"\n  Compute cache is fresh ({n} instruments). Use --force to recompute.")
            return manifest

    os.makedirs(MKT_DIR, exist_ok=True)

    # Build work items — DataFrame → dict avoids multiprocessing serialization overhead
    work_items = []
    for inst_id, df in ohlcv_cache.items():
        df_dict = {
            "date":   df["date"].values,
            "open":   df["open"].values,
            "high":   df["high"].values,
            "low":    df["low"].values,
            "close":  df["close"].values,
            "volume": df["volume"].values,
        }
        work_items.append((inst_id, df_dict, inst_id in PRICE_ONLY))

    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", max(cpu_count() - 1, 1)))
    print(f"\n  Computing {len(work_items)} instruments × {len(expressions)} expressions")
    print(f"  Workers: {n_workers}  (set EXPR_CACHE_WORKERS to override)")

    t0              = time.time()
    instrument_info = {}
    completed       = 0
    failed          = 0
    first_errors    = []

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_compute_worker,
        initargs=(expressions,)
    ) as pool:
        max_in_flight = n_workers * 2
        pending  = {}
        work_idx = 0

        def _submit_next():
            nonlocal work_idx
            if work_idx < len(work_items):
                item   = work_items[work_idx]
                future = pool.submit(_compute_one, item)
                pending[future] = item[0]
                work_idx += 1
                return True
            return False

        def _collect_one(future):
            nonlocal completed, failed
            inst_id = pending.pop(future)
            try:
                inst_out, dates, data = future.result()
                if dates is not None and data is not None:
                    path = os.path.join(MKT_DIR, instrument_filename(inst_out))
                    np.savez_compressed(path, data=data, dates=dates)
                    n_valid = int(np.sum(~np.isnan(data[-1])))
                    instrument_info[inst_out] = {
                        "n_bars":        len(dates),
                        "last_date":     str(dates[-1]),
                        "n_exprs_valid": n_valid,
                        "price_only":    inst_out in PRICE_ONLY,
                    }
                else:
                    failed += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"{inst_id}: returned None")
            except Exception as e:
                failed += 1
                if len(first_errors) < 5:
                    first_errors.append(f"{inst_id}: {e}")
            del future
            completed += 1
            if completed % 20 == 0 or completed == len(work_items):
                elapsed = time.time() - t0
                rate    = completed / elapsed if elapsed > 0 else 1
                eta     = (len(work_items) - completed) / rate if rate > 0 else 0
                pct     = completed / len(work_items) * 100
                print(f"    {completed}/{len(work_items)} ({pct:.0f}%)  "
                      f"[{elapsed:.0f}s, ~{eta:.0f}s left]  "
                      f"({len(instrument_info)} ok, {failed} failed)")

        for _ in range(min(max_in_flight, len(work_items))):
            _submit_next()

        while pending:
            future = next(as_completed(pending))
            _collect_one(future)
            _submit_next()

    total_time = time.time() - t0

    if first_errors:
        print(f"\n  First errors:")
        for e in first_errors:
            print(f"    ✗ {e}")

    manifest = {
        "fingerprint":   fingerprint,
        "n_expressions": len(expressions),
        "expr_names":    [e["name"] for e in expressions],
        "n_instruments": len(instrument_info),
        "instruments":   instrument_info,
        "built_at":      datetime.now(timezone.utc).isoformat(),
        "build_time_s":  round(total_time, 1),
    }
    save_manifest(manifest)

    total_bytes = sum(
        os.path.getsize(os.path.join(MKT_DIR, f))
        for f in os.listdir(MKT_DIR) if f.endswith(".npz")
    )

    print(f"\n  {'=' * 50}")
    print(f"  COMPUTE COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  Instruments: {len(instrument_info)}   Failed: {failed}")
    print(f"  Disk: {total_bytes / 1024**3:.2f} GB")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")
    return manifest


# ══════════════════════════════════════════════════════════════
# MANIFEST I/O
# ══════════════════════════════════════════════════════════════

def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return None
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(manifest):
    os.makedirs(MKT_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


# ══════════════════════════════════════════════════════════════
# NIGHTLY APPEND
# ══════════════════════════════════════════════════════════════

def append_new_bars(n_threads=16):
    print("\n" + "=" * 70)
    print("  MARKET CACHE — NIGHTLY APPEND")
    print("=" * 70)

    manifest = load_manifest()
    if not manifest:
        print("  No manifest. Run --build first.")
        return

    if not os.path.exists(OHLCV_PATH):
        print("  market_ohlcv.pkl not found. Run --build first.")
        return

    with open(OHLCV_PATH, "rb") as f:
        ohlcv_cache = pickle.load(f)

    # Load daily pickle for ETF reads (already refreshed by nightly steps 1-4)
    daily_cache = None
    if os.path.exists(DAILY_PKL):
        with open(DAILY_PKL, "rb") as f:
            daily_cache = pickle.load(f)

    instruments = all_instruments()
    updated     = []
    today       = pd.Timestamp.now().normalize()

    # Split: pickle instruments vs network instruments vs derived
    pickle_insts  = [i for i in instruments
                     if _is_pickle_instrument(i)]
    network_insts = [i for i in instruments
                     if i not in DERIVED_INSTRUMENTS
                     and not _is_pickle_instrument(i)]

    # 1. Pickle instruments — compare dates, merge if newer bars exist
    print(f"\n  Checking {len(pickle_insts)} pickle instruments...")
    for inst_id in pickle_insts:
        existing = ohlcv_cache.get(inst_id)
        if daily_cache is None or inst_id not in daily_cache:
            continue
        fresh_df = _standard_df(daily_cache[inst_id].copy())
        if fresh_df is None:
            continue
        if existing is not None:
            last_existing = pd.Timestamp(existing["date"].iloc[-1])
            last_fresh    = pd.Timestamp(fresh_df["date"].iloc[-1])
            if last_fresh <= last_existing:
                continue
            # Merge: keep existing + append new bars from pickle
            new_bars = fresh_df[fresh_df["date"] > last_existing]
            if len(new_bars) == 0:
                continue
            merged = pd.concat([existing, new_bars], ignore_index=True)
            merged = (merged.drop_duplicates("date")
                      .sort_values("date").reset_index(drop=True))
            ohlcv_cache[inst_id] = merged
        else:
            ohlcv_cache[inst_id] = fresh_df
        updated.append(inst_id)
        print(f"    {inst_id:25s} → {len(ohlcv_cache[inst_id])} bars")

    # 2. Network instruments — threaded fetch
    def _append_one_network(inst_id):
        existing = ohlcv_cache.get(inst_id)
        if existing is not None:
            last = pd.Timestamp(existing["date"].iloc[-1])
            if last >= today:
                return inst_id, None

        _, df_new = _fetch_one(inst_id, daily_cache=None)
        if df_new is None:
            return inst_id, None

        if existing is not None:
            last  = pd.Timestamp(existing["date"].iloc[-1])
            new   = df_new[df_new["date"] > last]
            if len(new) == 0:
                return inst_id, None
            merged = pd.concat([existing, new], ignore_index=True)
            merged = (merged.drop_duplicates("date")
                      .sort_values("date").reset_index(drop=True))
            return inst_id, merged
        return inst_id, df_new

    print(f"\n  Fetching {len(network_insts)} network instruments "
          f"({n_threads} threads)...")
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {pool.submit(_append_one_network, inst): inst
                   for inst in network_insts}
        for future in as_completed(futures):
            inst_id, df_merged = future.result()
            if df_merged is not None:
                ohlcv_cache[inst_id] = df_merged
                updated.append(inst_id)
                print(f"    {inst_id:25s} → {len(df_merged)} bars")

    # 3. Recompute derived instruments (always — cheap operation)
    n_derived = _compute_derived_instruments(ohlcv_cache)
    # Mark derived as updated if any component was updated
    derived_components = {
        "NYMO_CALC":   {"ADVN.INDX", "DECN.INDX"},
        "NYUD_CALC":   {"AVVN.INDX", "DVCN.INDX"},
        "NDXADP_CALC": {"ADVQ.INDX", "DECQ.INDX", "UNCQ.INDX"},
    }
    updated_set = set(updated)
    for derived, components in derived_components.items():
        if updated_set & components and derived in ohlcv_cache:
            updated.append(derived)

    with open(OHLCV_PATH, "wb") as f:
        pickle.dump(ohlcv_cache, f, protocol=4)
    print(f"\n  {len(updated)} instruments updated in market_ohlcv.pkl")

    if not updated:
        print("  Nothing to recompute.")
        return

    # Recompute only updated instruments — zero network
    print(f"\n  Recomputing {len(updated)} instruments...")
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)
    cached_info = manifest.get("instruments", {})
    n_workers   = int(os.environ.get("EXPR_CACHE_WORKERS", max(cpu_count() - 1, 1)))

    work_items = []
    for inst_id in updated:
        df = ohlcv_cache[inst_id]
        df_dict = {
            "date":   df["date"].values,
            "open":   df["open"].values,
            "high":   df["high"].values,
            "low":    df["low"].values,
            "close":  df["close"].values,
            "volume": df["volume"].values,
        }
        work_items.append((inst_id, df_dict, inst_id in PRICE_ONLY))

    recomputed = 0
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_compute_worker,
        initargs=(expressions,)
    ) as pool:
        futures = {pool.submit(_compute_one, item): item[0] for item in work_items}
        for future in as_completed(futures):
            inst_id, dates, data = future.result()
            if dates is not None:
                path = os.path.join(MKT_DIR, instrument_filename(inst_id))
                np.savez_compressed(path, data=data, dates=dates)
                cached_info[inst_id] = {
                    "n_bars":        len(dates),
                    "last_date":     str(dates[-1]),
                    "n_exprs_valid": int(np.sum(~np.isnan(data[-1]))),
                    "price_only":    inst_id in PRICE_ONLY,
                }
                recomputed += 1
                print(f"  ✓ {inst_id}")

    manifest["instruments"] = cached_info
    manifest["fingerprint"] = fingerprint
    manifest["appended_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest)
    print(f"\n  Recomputed: {recomputed}  Done.")


# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════

def print_status():
    print("\nMarket Context Cache Status")
    print("-" * 50)
    if os.path.exists(OHLCV_PATH):
        size_mb = os.path.getsize(OHLCV_PATH) / 1024**2
        with open(OHLCV_PATH, "rb") as f:
            cache = pickle.load(f)
        last = max(df["date"].iloc[-1] for df in cache.values())
        print(f"  market_ohlcv.pkl   {len(cache)} instruments  "
              f"{size_mb:.0f} MB  last bar: {last.date()}")

        # Show source breakdown
        pickle_count = sum(1 for k in cache if _is_pickle_instrument(k))
        eodhd_count  = sum(1 for k in cache
                          if k.endswith(".INDX") or k.endswith(".CC"))
        yf_count     = sum(1 for k in cache if "=" in k)
        fred_count   = sum(1 for k in cache if k.startswith("FRED:"))
        derived_count = sum(1 for k in cache if k in DERIVED_INSTRUMENTS)
        print(f"  Sources: {pickle_count} pickle, {eodhd_count} EODHD, "
              f"{yf_count} yfinance, {fred_count} FRED, "
              f"{derived_count} derived")
    else:
        print("  market_ohlcv.pkl   NOT FOUND — run --fetch")

    manifest = load_manifest()
    if manifest:
        print(f"  market_series/     {manifest.get('n_instruments', 0)} computed  "
              f"{manifest.get('n_expressions', 0)} expressions  "
              f"built {manifest.get('built_at', '?')[:10]}")
        if os.path.exists(MKT_DIR):
            total = sum(
                os.path.getsize(os.path.join(MKT_DIR, f))
                for f in os.listdir(MKT_DIR) if f.endswith(".npz")
            )
            print(f"  Disk: {total / 1024**3:.2f} GB")
    else:
        print("  market_series/     NOT FOUND — run --compute")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Context Cache Builder")
    parser.add_argument("--fetch",   action="store_true", help="Phase 1: fetch OHLCV only")
    parser.add_argument("--compute", action="store_true", help="Phase 2: compute expressions only (reads local pickle)")
    parser.add_argument("--build",   action="store_true", help="Both phases in sequence")
    parser.add_argument("--append",  action="store_true", help="Nightly: fetch new bars + recompute changed instruments")
    parser.add_argument("--force",   action="store_true", help="Force re-fetch and/or recompute even if cache is fresh")
    parser.add_argument("--status",  action="store_true", help="Print cache status")
    parser.add_argument("--threads", type=int, default=16, help="Fetch thread count (default: 16)")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.fetch:
        fetch_all(force=args.force, n_threads=args.threads)
    elif args.compute:
        compute_all(force=args.force)
    elif args.build:
        ohlcv = fetch_all(force=args.force, n_threads=args.threads)
        compute_all(ohlcv_cache=ohlcv, force=args.force)
    elif args.append:
        append_new_bars(n_threads=args.threads)
    else:
        parser.print_help()
