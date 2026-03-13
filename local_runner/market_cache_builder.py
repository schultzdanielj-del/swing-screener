"""
Market Context Cache Builder — Two-phase: fetch then compute.

PHASE 1 — FETCH  (network, threaded I/O parallelism)
  Downloads OHLCV for all 266 market instruments from yfinance / Stooq / FRED.
  Stores everything in a single pickle: local_runner/cache/market_ohlcv.pkl
  Zero computation in this phase. Pure data collection.

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
MKT_DIR       = os.path.join(CACHE_DIR, "market_series")
MANIFEST_PATH = os.path.join(MKT_DIR, "_manifest.json")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# INSTRUMENT REGISTRY
# ══════════════════════════════════════════════════════════════

PRICE_ONLY = {
    "^VIX", "^VIX3M", "^VVIX", "^SKEW",
    "^TNX", "^TYX", "^FVX", "^IRX",
    "ES=F", "NQ=F", "RTY=F", "YM=F",
    "ZB=F", "ZN=F", "ZF=F", "ZT=F",
    "CL=F", "GC=F", "SI=F", "HG=F",
    "$nymo", "$tick", "$nyadv", "$nydec",
    "$trin", "$nyhl", "$nahl", "$nyud",
    "$spxadp", "$ndxadp",
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
    "breadth_internals_stooq": [
        "$nymo", "$tick", "$nyadv", "$nydec",
        "$trin", "$nyhl", "$nahl", "$nyud",
        "$spxadp", "$ndxadp",
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


def all_instruments():
    result = []
    for syms in INSTRUMENTS.values():
        result.extend(syms)
    return result


# ══════════════════════════════════════════════════════════════
# PHASE 1 — FETCHERS  (called from threads)
# ══════════════════════════════════════════════════════════════

def _fetch_yfinance(symbol, period="10y"):
    import yfinance as yf
    df = yf.download(symbol, period=period, progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df = df.reset_index()
    df = df.rename(columns={"Date": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=8)
    df = df[(df["close"] > 0) & (df["date"] >= cutoff)].sort_values("date").reset_index(drop=True)
    return df if len(df) >= 50 else None


def _fetch_stooq(symbol):
    import urllib.request, io
    sym_clean = symbol.lower().lstrip("$")
    url = f"https://stooq.com/q/d/l/?s=${sym_clean}&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
        if "No data" in raw or len(raw.strip().splitlines()) < 5:
            return None
        df = pd.read_csv(io.StringIO(raw))
        df.columns = [c.lower() for c in df.columns]
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                num_cols = [c for c in df.columns
                            if c != "date" and pd.api.types.is_numeric_dtype(df[c])]
                if num_cols:
                    df[col] = df[num_cols[0]]
                else:
                    return None
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = 0.0
        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=8)
        df = df[df["date"] >= cutoff].sort_values("date").reset_index(drop=True)
        return df if len(df) >= 50 else None
    except Exception:
        return None


def _fetch_fred(series_id):
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
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=8)
        df = df[df["date"] >= cutoff].sort_values("date").reset_index(drop=True)
        return df if len(df) >= 50 else None
    except Exception:
        return None


def _fetch_one(instrument_id):
    try:
        if instrument_id.startswith("FRED:"):
            df = _fetch_fred(instrument_id[5:])
        elif instrument_id.startswith("$"):
            df = _fetch_stooq(instrument_id)
        else:
            df = _fetch_yfinance(instrument_id)
        return instrument_id, df
    except Exception:
        return instrument_id, None


# ══════════════════════════════════════════════════════════════
# PHASE 1 — FETCH ALL
# ══════════════════════════════════════════════════════════════

def fetch_all(force=False, n_threads=16):
    print("\n" + "=" * 70)
    print("  MARKET CACHE — PHASE 1: FETCH")
    print("=" * 70)

    instruments = all_instruments()
    print(f"\n  {len(instruments)} instruments  |  {n_threads} threads")

    if not force and os.path.exists(OHLCV_PATH):
        print(f"\n  market_ohlcv.pkl already exists. Use --force to re-fetch.")
        with open(OHLCV_PATH, "rb") as f:
            existing = pickle.load(f)
        print(f"  {len(existing)} instruments in cache.")
        return existing

    os.makedirs(CACHE_DIR, exist_ok=True)
    t0        = time.time()
    results   = {}
    failed    = []
    completed = 0

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {pool.submit(_fetch_one, inst): inst for inst in instruments}
        for future in as_completed(futures):
            inst_id, df = future.result()
            completed += 1
            if df is not None:
                results[inst_id] = df
                print(f"  [{completed:3d}/{len(instruments)}] OK   {inst_id:25s} "
                      f"{len(df)} bars  "
                      f"({df['date'].iloc[0].date()} – {df['date'].iloc[-1].date()})")
            else:
                failed.append(inst_id)
                print(f"  [{completed:3d}/{len(instruments)}] FAIL {inst_id}")

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
            .replace("-", "dash_"))
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

    instruments = all_instruments()
    updated     = []
    today       = pd.Timestamp.now().normalize()

    def _append_one(inst_id):
        existing = ohlcv_cache.get(inst_id)
        if existing is not None:
            last = pd.Timestamp(existing["date"].iloc[-1])
            if last >= today:
                return inst_id, None
        try:
            if inst_id.startswith("FRED:"):
                df_new = _fetch_fred(inst_id[5:])
            elif inst_id.startswith("$"):
                df_new = _fetch_stooq(inst_id)
            else:
                df_new = _fetch_yfinance(inst_id, period="1mo")
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
        except Exception:
            return inst_id, None

    print(f"\n  Fetching new bars ({n_threads} threads)...")
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {pool.submit(_append_one, inst): inst for inst in instruments}
        for future in as_completed(futures):
            inst_id, df_merged = future.result()
            if df_merged is not None:
                ohlcv_cache[inst_id] = df_merged
                updated.append(inst_id)
                n_new = len(df_merged) - len(ohlcv_cache.get(inst_id, df_merged))
                print(f"  {inst_id:25s} → {len(df_merged)} bars")

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
