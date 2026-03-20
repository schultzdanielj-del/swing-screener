"""
EV Grinder — Phase 3 Correlative Scoring Engine.

See EV_GRINDER.md for full spec.

Usage:
    python scripts/ev_grinder.py --setup dtss

Increment 1: Data loaders + refinement depth replay.
Increment 2: Setup feature computation (6 OHLCV + 4 fundamentals).
Increment 3: Market feature screening (parallel) + setup feature screening.
Increment 4: Cross-instrument dedup (greedy correlation-based).
"""

import os
import sys
import glob
import json
import time
import pickle
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from scipy.stats import rankdata

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
MKT_DIR = os.path.join(CACHE_DIR, "market_series")
MKT_MANIFEST = os.path.join(MKT_DIR, "_manifest.json")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# FILE FINDERS
# ══════════════════════════════════════════════════════════════

def find_latest_refinement(setup_type):
    pattern = os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None, None
    def _extract_ts(path):
        bn = os.path.basename(path).replace(".json", "")
        parts = bn.split("_")
        if len(parts) >= 2:
            ts = parts[-2] + parts[-1]
            if len(ts) == 14 and ts.isdigit():
                return ts
        return "0"
    candidates.sort(key=_extract_ts, reverse=True)
    return candidates[0], os.path.basename(candidates[0])


def find_raw_clusters(setup_type):
    latest = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if os.path.exists(latest):
        return latest, os.path.basename(latest)
    pattern = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}_*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None, None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0], os.path.basename(candidates[0])


# ══════════════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════════════

def load_clusters(path):
    with open(path) as f:
        data = json.load(f)
    clusters = data.get("clusters", [])
    signals = []
    for c in clusters:
        leftward_idxs = [b["bar_idx"] for b in c.get("leftward", [])]
        signals.append({
            "ticker": c["ticker"], "date": c["rightmost"]["date"],
            "bar_idx": c["rightmost"]["bar_idx"],
            "close": c["rightmost"].get("close"),
            "classification": c.get("classification", "UNKNOWN"),
            "move_adr": c.get("move_adr"), "adr_at_signal": c.get("adr_at_signal"),
            "entry_high": c.get("entry_high"),
            "is_example": bool(c.get("is_example", 0)),
            "cluster_id": c["cluster_id"], "leftward_bar_idxs": leftward_idxs,
        })
    return signals, data


def load_refinement(path):
    with open(path) as f:
        data = json.load(f)
    refinement_conditions = data.get("refinement_conditions_only", [])
    winners = data.get("winner_signals", [])
    losers = data.get("loser_signals", [])
    eliminated = data.get("eliminated_signals", [])
    return refinement_conditions, winners + losers, eliminated, data


def load_5yr_cache():
    for name in ("universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    raise FileNotFoundError("No 5yr OHLCV cache found")


def load_fundamentals_cache():
    path = os.path.join(CACHE_DIR, "fundamentals_cache.json")
    if not os.path.exists(path):
        print("  WARNING: fundamentals_cache.json not found")
        return {}
    with open(path) as f:
        return json.load(f).get("tickers", {})


# ══════════════════════════════════════════════════════════════
# SIGNAL STATS
# ══════════════════════════════════════════════════════════════

def compute_signal_stats(signals):
    total = len(signals)
    if total == 0:
        return {"total": 0, "winners": 0, "losers": 0, "wr": 0.0,
                "peak_day": 0, "avg_day": 0.0, "avg_week": 0.0,
                "avg_month": 0.0, "avg_year": 0.0}
    winners = sum(1 for s in signals if "WIN" in s.get("classification", ""))
    losers = total - winners
    dates = [s["date"] for s in signals]
    date_counts = Counter(dates)
    peak_day = max(date_counts.values()) if date_counts else 0
    avg_day = total / len(date_counts) if date_counts else 0.0
    if dates:
        from datetime import date as dt_date
        sd = sorted(dates)
        span = (dt_date.fromisoformat(sd[-1]) - dt_date.fromisoformat(sd[0])).days + 1
        avg_week = total / max(span / 7, 1)
        avg_month = total / max(span / 30.44, 1)
        avg_year = total / max(span / 365.25, 1)
    else:
        avg_week = avg_month = avg_year = 0.0
    return {"total": total, "winners": winners, "losers": losers,
            "wr": round(winners / total, 4), "peak_day": peak_day,
            "avg_day": round(avg_day, 2), "avg_week": round(avg_week, 2),
            "avg_month": round(avg_month, 2), "avg_year": round(avg_year, 2)}


# ══════════════════════════════════════════════════════════════
# SETUP FEATURES (INCREMENT 2)
# ══════════════════════════════════════════════════════════════

def _build_date_index(df):
    dates = df["date"].values
    return {str(dates[i])[:10]: i for i in range(len(dates))}


def _compute_rs_series(df):
    """Compute RS raw value series for a ticker.

    TC2000 PCF formula:
    ((((C4/O4)-1)*100) + ... + (((C/O)-1)*100)) / 5 * (((C+C50)/2) / ATR50)

    5-day rolling average of intraday % move, multiplied by
    (average of current close and close 50 bars ago) divided by ATR50.
    """
    closes = df["close"].values.astype(np.float64)
    opens = df["open"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    dates = df["date"].values
    n = len(df)
    if n < 55:
        return {}

    # Intraday % move: ((C/O) - 1) * 100
    with np.errstate(divide='ignore', invalid='ignore'):
        intraday_pct = (closes / opens - 1.0) * 100.0
    intraday_pct = np.where(np.isfinite(intraday_pct), intraday_pct, 0.0)

    # 5-day rolling average (vectorized via cumsum)
    cs_intra = np.cumsum(intraday_pct)
    avg_intraday = np.full(n, np.nan)
    avg_intraday[4:] = (cs_intra[4:] - np.concatenate([[0], cs_intra[:-5]])) / 5.0

    # ATR50: 50-bar simple moving average of true range (vectorized via cumsum)
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - np.roll(closes, 1)),
                               np.abs(lows - np.roll(closes, 1))))
    tr[0] = highs[0] - lows[0]
    cs_tr = np.cumsum(tr)
    atr50 = np.full(n, np.nan)
    atr50[49:] = (cs_tr[49:] - np.concatenate([[0], cs_tr[:-50]])) / 50.0

    # Price component: (C + C50) / 2 — average of current close and close 50 bars ago
    # This matches the TC2000 PCF: (((C+C50)/2)/ATR50)
    c50 = np.full(n, np.nan)
    c50[50:] = closes[:-50]
    avg_price = (closes + c50) / 2.0

    # RS = avg_intraday * (avg_price / atr50)
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = avg_intraday * (avg_price / atr50)
    rs = np.where(np.isfinite(rs), rs, np.nan)

    return {str(dates[i])[:10]: float(rs[i]) for i in range(n) if not np.isnan(rs[i])}


def _resample_to_weekly(df):
    if len(df) < 10:
        return None
    wdf = df.copy().set_index("date")
    r = wdf.resample("W").agg({"open": "first", "high": "max", "low": "min",
                                "close": "last", "volume": "sum"}).dropna(subset=["close"])
    if len(r) < 10:
        return None
    r = r.reset_index()
    r.columns = ["date", "open", "high", "low", "close", "volume"]
    return r


def _compute_rs_weekly_series(df):
    wdf = _resample_to_weekly(df)
    if wdf is None:
        return {}
    weekly_rs = _compute_rs_series(wdf)
    if not weekly_rs:
        return {}
    daily_dates = df["date"].values
    wd_sorted = sorted(weekly_rs.keys())
    if not wd_sorted:
        return {}
    result = {}
    wi = 0
    for i in range(len(daily_dates)):
        d = str(daily_dates[i])[:10]
        while wi < len(wd_sorted) - 1 and wd_sorted[wi + 1] <= d:
            wi += 1
        if wd_sorted[wi] <= d:
            result[d] = weekly_rs[wd_sorted[wi]]
    return result


def compute_setup_features(all_signals):
    """Compute 6 OHLCV + 4 fundamentals features using DATE-BASED lookups."""
    print("\n  ── SETUP FEATURE COMPUTATION ──")
    t0 = time.time()

    print("  Loading 5yr OHLCV cache...")
    ohlcv = load_5yr_cache()
    print(f"  {len(ohlcv)} tickers")

    print("  Loading fundamentals cache...")
    fund = load_fundamentals_cache()
    print(f"  {sum(1 for v in fund.values() if 'error' not in v)} tickers with data")

    for tk, df in ohlcv.items():
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            ohlcv[tk] = df.copy()
            ohlcv[tk]["date"] = pd.to_datetime(df["date"])

    print("  Building date indexes...")
    tickers = set(s["ticker"] for s in all_signals)
    date_idx = {tk: _build_date_index(ohlcv[tk]) for tk in tickers if tk in ohlcv}

    print("  Computing SPY RS...")
    spy_df = ohlcv.get("SPY")
    if spy_df is None:
        raise RuntimeError("SPY not in OHLCV cache")
    spy_d1 = _compute_rs_series(spy_df)
    spy_w1 = _compute_rs_weekly_series(spy_df)
    print(f"  SPY RS: {len(spy_d1)} daily, {len(spy_w1)} weekly")

    print("  Computing RS for signal tickers...")
    tk_d1 = {}
    tk_w1 = {}
    for tk in tickers:
        df = ohlcv.get(tk)
        if df is None:
            continue
        tk_d1[tk] = _compute_rs_series(df)
        tk_w1[tk] = _compute_rs_weekly_series(df)
    print(f"  RS done for {len(tk_d1)} tickers")

    print("  Computing per-signal features...")
    cov = defaultdict(int)
    n_miss = 0

    for sig in all_signals:
        tk = sig["ticker"]
        sd = sig["date"]
        df = ohlcv.get(tk)
        di = date_idx.get(tk, {})
        ri = di.get(sd)

        if ri is None:
            n_miss += 1
            for f in ["feat_price", "feat_adr", "feat_dollar_volume_20d",
                       "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
                       "feat_market_cap", "feat_volume_float_ratio",
                       "feat_rs_vs_sector", "feat_sector_rs_vs_spy"]:
                sig[f] = None
            sig["_sector"] = None
            continue

        sig["feat_price"] = float(df["close"].values[ri])
        cov["price"] += 1

        if ri >= 13:
            h = df["high"].values[ri-13:ri+1].astype(np.float64)
            l = df["low"].values[ri-13:ri+1].astype(np.float64)
            a = float(np.mean(h - l))
            sig["feat_adr"] = a if (a > 0 and np.isfinite(a)) else None
        else:
            sig["feat_adr"] = None
        if sig["feat_adr"] is not None:
            cov["adr"] += 1

        if ri >= 19:
            c = df["close"].values[ri-19:ri+1].astype(np.float64)
            v = df["volume"].values[ri-19:ri+1].astype(np.float64)
            dv = float(np.mean(c * v))
            sig["feat_dollar_volume_20d"] = dv if np.isfinite(dv) else None
        else:
            sig["feat_dollar_volume_20d"] = None
        if sig["feat_dollar_volume_20d"] is not None:
            cov["dollar_volume_20d"] += 1

        sig["feat_days_since_ipo"] = ri
        cov["days_since_ipo"] += 1

        tr = tk_d1.get(tk, {}).get(sd)
        sr = spy_d1.get(sd)
        sig["feat_rs_d1"] = (tr - sr) if (tr is not None and sr is not None) else None
        if sig["feat_rs_d1"] is not None:
            cov["rs_d1"] += 1

        tw = tk_w1.get(tk, {}).get(sd)
        sw = spy_w1.get(sd)
        sig["feat_rs_w1"] = (tw - sw) if (tw is not None and sw is not None) else None
        if sig["feat_rs_w1"] is not None:
            cov["rs_w1"] += 1

        fi = fund.get(tk, {})
        sh = fi.get("shares_outstanding")
        sig["feat_market_cap"] = (sh * sig["feat_price"]) if (sh and sig["feat_price"]) else None
        if sig["feat_market_cap"] is not None:
            cov["market_cap"] += 1

        fl = fi.get("float_shares")
        if fl and fl > 0:
            vol = float(df["volume"].values[ri])
            sig["feat_volume_float_ratio"] = (vol / fl) if (vol > 0 and np.isfinite(vol)) else None
        else:
            sig["feat_volume_float_ratio"] = None
        if sig["feat_volume_float_ratio"] is not None:
            cov["volume_float_ratio"] += 1

        sig["_sector"] = fi.get("sector")

    if n_miss:
        print(f"  WARNING: {n_miss} signals had dates not in cache")

    # Sector RS
    print("  Computing sector RS...")
    dsr = defaultdict(list)
    for s in all_signals:
        sec = s.get("_sector")
        if sec and s["feat_rs_d1"] is not None:
            dsr[(s["date"], sec)].append(s["feat_rs_d1"])
    savg = {k: float(np.mean(v)) for k, v in dsr.items()}

    for s in all_signals:
        sec = s.get("_sector")
        sd = s["date"]
        if sec and s["feat_rs_d1"] is not None and (sd, sec) in savg:
            s["feat_rs_vs_sector"] = s["feat_rs_d1"] - savg[(sd, sec)]
            cov["rs_vs_sector"] += 1
        else:
            s["feat_rs_vs_sector"] = None
        if sec and (sd, sec) in savg:
            s["feat_sector_rs_vs_spy"] = savg[(sd, sec)]
            cov["sector_rs_vs_spy"] += 1
        else:
            s["feat_sector_rs_vs_spy"] = None
        del s["_sector"]

    elapsed = time.time() - t0
    n = len(all_signals)
    print(f"\n  Coverage ({n} signals):")
    for f in ["price", "adr", "dollar_volume_20d", "days_since_ipo",
              "rs_d1", "rs_w1", "market_cap", "volume_float_ratio",
              "rs_vs_sector", "sector_rs_vs_spy"]:
        print(f"    {f:25s} {cov[f]:>5}/{n} ({cov[f]/n*100:.1f}%)")
    print(f"\n  Setup features complete ({elapsed:.1f}s)")
    del ohlcv
    return dict(cov)


def validate_setup_features(all_signals):
    """Validate against preserved setup_grinder output."""
    print("\n  ── VALIDATING SETUP FEATURES ──")
    import requests
    url = "https://web-production-e3025.up.railway.app/api/v2/files/local_runner/cache/setup_dtss_20260313_135931.json"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"  WARNING: Could not load reference (HTTP {resp.status_code})")
            return True, {}
        ref_data = resp.json()
    except Exception as e:
        print(f"  WARNING: Could not load reference: {e}")
        return True, {}

    ref_sigs = ref_data.get("signal_features", [])
    if not ref_sigs:
        return True, {}
    print(f"  Reference: {len(ref_sigs)} signals")

    ref_lk = {(s["ticker"], s["signal_date"]): s for s in ref_sigs}
    fmap = {"price": "feat_price", "adr": "feat_adr",
            "dollar_volume_20d": "feat_dollar_volume_20d",
            "days_since_ipo": "feat_days_since_ipo",
            "rs_d1": "feat_rs_d1", "rs_w1": "feat_rs_w1"}

    diffs = {f: [] for f in fmap}
    matched = 0
    for s in all_signals:
        ref = ref_lk.get((s["ticker"], s["date"]))
        if not ref:
            continue
        matched += 1
        for rn, sn in fmap.items():
            rv, sv = ref.get(rn), s.get(sn)
            if rv is not None and sv is not None:
                diffs[rn].append(abs(float(rv) - float(sv)))

    print(f"  Matched: {matched}/{len(all_signals)}")

    ok = True
    comp = {}
    for feat, dl in diffs.items():
        if not dl:
            comp[feat] = {"n": 0, "status": "no_data"}
            continue
        mx, mn = max(dl), sum(dl) / len(dl)
        # Thresholds: tight for exact values, loose for derived values
        # days_since_ipo drifts as cache grows (expected ~13 bars per day of nightly rebuilds)
        # RS can have small formula-level float diffs
        th = {"price": 0.01, "adr": 0.01, "dollar_volume_20d": 1.0,
              "days_since_ipo": 20.0, "rs_d1": 1.0, "rs_w1": 1.0}[feat]
        passed = mx <= th
        if not passed:
            ok = False
        comp[feat] = {"n": len(dl), "max_diff": round(mx, 6),
                      "mean_diff": round(mn, 6), "threshold": th,
                      "status": "PASS" if passed else "FAIL"}
        print(f"    {feat:25s} max={mx:.6f}  mean={mn:.6f}  [{'PASS' if passed else 'FAIL'}]")

    print(f"\n  {'✓ All passed' if ok else '✗ FAILED'}")
    return ok, comp


# ══════════════════════════════════════════════════════════════
# REFINEMENT DEPTH REPLAY (INCREMENT 1)
# ══════════════════════════════════════════════════════════════

def replay_refinement_depth(all_signals, refinement_conditions, expr_cache):
    print("\n  ── REFINEMENT DEPTH REPLAY ──")
    t0 = time.time()
    n_cond = len(refinement_conditions)
    if n_cond == 0:
        return {}, [compute_signal_stats(all_signals)], []

    losing = {}
    for s in all_signals:
        if "LOSS" in s.get("classification", ""):
            bars = [(s["ticker"], s["bar_idx"])]
            for li in s.get("leftward_bar_idxs", []):
                bars.append((s["ticker"], li))
            losing[s["cluster_id"]] = bars
    n_los = len(losing)
    print(f"  Losing clusters: {n_los}, Conditions: {n_cond}")

    cols = []
    for c in refinement_conditions:
        ci = expr_cache.expr_index(c["name"])
        if ci is None:
            raise RuntimeError(f"'{c['name']}' not in expr cache")
        cols.append(ci)

    print(f"  Loading expr data...")
    tc = {}
    for bars in losing.values():
        for tk, _ in bars:
            if tk not in tc:
                _, d = expr_cache.get_ticker(tk)
                if d is None:
                    raise RuntimeError(f"'{tk}' not in expr cache")
                tc[tk] = d
    print(f"  Loaded {len(tc)} tickers")

    print(f"  Computing pass/fail...")
    cbp = {}
    for cid, bars in losing.items():
        p = np.ones((len(bars), n_cond), dtype=bool)
        for bi, (tk, idx) in enumerate(bars):
            d = tc[tk]
            if idx >= d.shape[0]:
                raise RuntimeError(f"bar_idx {idx} >= {d.shape[0]} for {tk}")
            for ci, (cond, col) in enumerate(zip(refinement_conditions, cols)):
                v = float(d[idx, col])
                p[bi, ci] = True if np.isnan(v) else (v >= cond["low"] and v <= cond["high"])
        cbp[cid] = p
    del tc

    def alive(mask):
        return sum(1 for p in cbp.values() if np.any(np.all(p[:, mask], axis=1)))

    print(f"\n  Verifying full depth...")
    aa = np.ones(n_cond, dtype=bool)
    surv = alive(aa)
    print(f"  Full: {surv} surviving, {n_los - surv} eliminated")

    print(f"\n  Greedy peel...")
    am = np.ones(n_cond, dtype=bool)
    po = []
    for ps in range(n_cond):
        bc, ba = None, -1
        ca = alive(am)
        for ci in range(n_cond):
            if not am[ci]:
                continue
            t = am.copy(); t[ci] = False
            a = alive(t)
            if bc is None or a < ba:
                ba, bc = a, ci
        am[bc] = False
        po.append(bc)
        if (ps+1) % 20 == 0 or ps == 0 or ps == n_cond - 1:
            print(f"    Peel {ps+1:3d}: {refinement_conditions[bc]['name']:40s} "
                  f"+{ba-ca:3d} ({ba} alive, depth={n_cond-ps-1})")

    ao = list(reversed(po))
    print(f"\n  Computing killed_at_depth...")
    kad = {}
    for cid, p in cbp.items():
        ba = np.ones(p.shape[0], dtype=bool)
        for di, ci in enumerate(ao):
            ba = ba & p[:, ci]
            if not np.any(ba):
                kad[cid] = di + 1
                break
    print(f"  Killed: {len(kad)}, Surviving: {n_los - len(kad)}")

    print(f"\n  Building depth_stats...")
    c2s = {s["cluster_id"]: s for s in all_signals if "LOSS" in s.get("classification", "")}
    ws = [s for s in all_signals if "WIN" in s.get("classification", "")]
    ds = []
    for d in range(n_cond + 1):
        al = [s for cid, s in c2s.items() if kad.get(cid) is None or d < kad[cid]]
        st = compute_signal_stats(ws + al)
        st["depth"] = d
        ds.append(st)

    cio = []
    for di, ci in enumerate(ao):
        d = di + 1
        c = refinement_conditions[ci]
        cio.append({"idx": di, "depth": d, "name": c["name"],
                     "low": c["low"], "high": c["high"],
                     "clusters_killed": [cid for cid, kd in kad.items() if kd == d],
                     "cumulative_losers_remaining": ds[d]["losers"],
                     "cumulative_winners": ds[d]["winners"],
                     "cumulative_total": ds[d]["total"],
                     "cumulative_wr": ds[d]["wr"],
                     "cumulative_peak": ds[d]["peak_day"],
                     "cumulative_avg": ds[d]["avg_day"]})

    print(f"\n  Replay complete ({time.time()-t0:.1f}s)")
    return kad, ds, cio


# ══════════════════════════════════════════════════════════════
# FEATURE SCREENING (INCREMENT 3)
# ══════════════════════════════════════════════════════════════

def screen_features(values, is_winner, move_adrs, feature_names,
                    min_per_bucket=8, wr_pp_threshold=10, mfe_adr_threshold=1.0):
    """Screen features for WR and MFE predictive power using DECILES.

    Spread is measured D10 minus D1 (top 10% vs bottom 10%). This is far
    more discriminating than quartiles — random noise rarely produces a
    large D10-D1 spread, while genuinely predictive features show bigger
    spreads because the extreme buckets are purer.

    Args:
        values: np.ndarray shape (n_signals, n_features), float. NaN = missing.
        is_winner: np.ndarray shape (n_signals,), bool.
        move_adrs: np.ndarray shape (n_signals,), float. NaN for losers is ok.
        feature_names: list of str, length n_features.
        min_per_bucket: minimum signals per decile (skip feature if any D < this).
        wr_pp_threshold: minimum D10-D1 WR spread in percentage points.
        mfe_adr_threshold: minimum D10-D1 MFE spread in ADR units.

    Returns:
        list of dicts, one per surviving feature.
    """
    n_signals, n_features = values.shape
    survivors = []
    wr_threshold = wr_pp_threshold / 100.0  # convert pp to fraction
    N_BUCKETS = 10
    pct_boundaries = np.linspace(0, 100, N_BUCKETS + 1)  # [0, 10, 20, ..., 100]

    for fi in range(n_features):
        col = values[:, fi]

        # Skip if >50% non-finite
        valid_mask = np.isfinite(col)
        n_valid = int(np.sum(valid_mask))
        if n_valid < n_signals * 0.5:
            continue

        # Get valid subset
        valid_vals = col[valid_mask]
        valid_winners = is_winner[valid_mask]
        valid_moves = move_adrs[valid_mask]

        # Compute decile boundaries (9 cutpoints: 10th, 20th, ..., 90th percentile)
        boundaries = np.percentile(valid_vals, pct_boundaries[1:-1])

        # If min == max (no spread), skip
        if boundaries[0] == boundaries[-1]:
            continue

        # Assign deciles (1-10) using digitize
        # digitize returns bucket index: values <= boundaries[0] → 0, etc.
        # We clip to 0..9 then add 1 to get 1..10
        bucket_idx = np.digitize(valid_vals, boundaries, right=False)
        bucket_idx = np.clip(bucket_idx, 0, N_BUCKETS - 1) + 1  # 1..10

        # Check min per bucket
        b_counts = [int(np.sum(bucket_idx == b)) for b in range(1, N_BUCKETS + 1)]
        if any(c < min_per_bucket for c in b_counts):
            continue

        # WR per decile
        b_wr = []
        for b in range(1, N_BUCKETS + 1):
            mask = bucket_idx == b
            b_wr.append(float(np.mean(valid_winners[mask])))

        # Spread: D10 minus D1
        wr_spread = abs(b_wr[N_BUCKETS - 1] - b_wr[0])
        wr_pass = wr_spread >= wr_threshold

        # MFE per decile (median move_adr among winners only)
        b_mfe = []
        for b in range(1, N_BUCKETS + 1):
            mask = (bucket_idx == b) & valid_winners
            winner_moves = valid_moves[mask]
            winner_moves = winner_moves[np.isfinite(winner_moves)]
            if len(winner_moves) >= 3:
                b_mfe.append(float(np.median(winner_moves)))
            else:
                b_mfe.append(np.nan)

        # MFE spread: D10 vs D1 (only if both non-NaN)
        d1_mfe = b_mfe[0]
        d10_mfe = b_mfe[N_BUCKETS - 1]
        if not np.isnan(d1_mfe) and not np.isnan(d10_mfe):
            mfe_spread = abs(d10_mfe - d1_mfe)
        else:
            mfe_spread = 0.0
        mfe_pass = mfe_spread >= mfe_adr_threshold

        if not wr_pass and not mfe_pass:
            continue

        # Determine direction: ascending (D10 best) or descending (D1 best)
        if wr_pass:
            direction = "ascending" if b_wr[N_BUCKETS - 1] > b_wr[0] else "descending"
        else:
            direction = "ascending" if d10_mfe > d1_mfe else "descending"

        screen_type = "both" if (wr_pass and mfe_pass) else ("wr_only" if wr_pass else "mfe_only")

        survivors.append({
            "name": feature_names[fi],
            "col_idx": fi,
            "screen_type": screen_type,
            "wr_spread": round(wr_spread, 4),
            "mfe_spread": round(mfe_spread, 4),
            "direction": direction,
            "decile_boundaries": [round(float(b), 6) for b in boundaries],
            "decile_wr": [round(w, 4) for w in b_wr],
            "decile_mfe": [round(m, 4) if not np.isnan(m) else None for m in b_mfe],
            "n_per_decile": b_counts,
            "values": col.copy(),  # full array including NaN, for dedup correlation later
        })

    return survivors


def _cap_survivors(survivors, max_per_instrument=200):
    """Keep only the top N survivors per instrument, ranked by screening strength.

    Full correlation-based dedup happens in increment 4 across all instruments.
    This just caps the output so no single instrument floods the results.
    """
    if len(survivors) <= max_per_instrument:
        return survivors

    # Rank by screening strength: max of WR spread and normalized MFE spread
    scored = sorted(survivors,
                    key=lambda s: max(s["wr_spread"], s["mfe_spread"] / 10.0),
                    reverse=True)
    return scored[:max_per_instrument]


def _instrument_filename(instrument_id):
    """Convert instrument ID to .npz filename. Must match market_cache_builder.py."""
    safe = (instrument_id
            .replace("^", "caret_")
            .replace("=", "eq_")
            .replace(":", "col_")
            .replace("$", "dol_")
            .replace("-", "dash_"))
    return f"{safe}.npz"


def _screen_one_instrument(args):
    """Worker: load one instrument's .npz and screen all expressions.

    Args tuple:
        (instrument_id, npz_path, signal_dates_pre, signal_dates_post,
         is_winner_pre, is_winner_post, move_adrs_pre, move_adrs_post,
         n_exprs, wr_pp, mfe_adr, min_per_bucket)

    Returns:
        (instrument_id, survivors_pre, survivors_post, n_tested, elapsed_s)
        survivors_pre/post are lists of dicts (same as screen_features output,
        but with 'values' replaced by signal-level value arrays for dedup later,
        and with name prefixed by instrument)
    """
    (inst_id, npz_path, sig_dates_pre, sig_dates_post,
     is_win_pre, is_win_post, moves_pre, moves_post,
     n_exprs, expr_names, wr_pp, mfe_adr, min_per_bucket) = args

    from datetime import date as dt_date, timedelta
    t0 = time.time()
    try:
        loaded = np.load(npz_path, allow_pickle=True)
        data = loaded["data"]    # shape (n_bars, n_exprs)
        dates = loaded["dates"]  # string array

        # Build date → row index
        date_to_row = {}
        for i, d in enumerate(dates):
            date_to_row[str(d)] = i

        n_pre = len(sig_dates_pre)
        n_post = len(sig_dates_post)

        # Look up values for pre-refinement signals
        vals_pre = np.full((n_pre, n_exprs), np.nan, dtype=np.float32)
        for si, sd in enumerate(sig_dates_pre):
            ri = date_to_row.get(sd)
            if ri is None:
                # Try most recent prior date (holiday/calendar mismatch)
                # Binary search: find largest date <= sd
                for offset in range(1, 6):
                    try:
                        d = dt_date.fromisoformat(sd) - timedelta(days=offset)
                        ri = date_to_row.get(d.isoformat())
                        if ri is not None:
                            break
                    except (ValueError, TypeError):
                        break
            if ri is not None and ri < data.shape[0]:
                vals_pre[si, :] = data[ri, :]

        # Look up values for post-refinement signals
        vals_post = np.full((n_post, n_exprs), np.nan, dtype=np.float32)
        for si, sd in enumerate(sig_dates_post):
            ri = date_to_row.get(sd)
            if ri is None:
                for offset in range(1, 6):
                    try:
                        d = dt_date.fromisoformat(sd) - timedelta(days=offset)
                        ri = date_to_row.get(d.isoformat())
                        if ri is not None:
                            break
                    except (ValueError, TypeError):
                        break
            if ri is not None and ri < data.shape[0]:
                vals_post[si, :] = data[ri, :]

        # Free the big array
        del data, loaded

        # Build feature names for this instrument
        feat_names = [f"{inst_id}__{expr_names[j]}" for j in range(n_exprs)]

        # Screen pre-refinement
        surv_pre = screen_features(
            vals_pre, is_win_pre, moves_pre, feat_names,
            min_per_bucket=min_per_bucket, wr_pp_threshold=wr_pp, mfe_adr_threshold=mfe_adr)

        # Screen post-refinement
        surv_post = screen_features(
            vals_post, is_win_post, moves_post, feat_names,
            min_per_bucket=min_per_bucket, wr_pp_threshold=wr_pp, mfe_adr_threshold=mfe_adr)

        # Per-instrument cap: keep top 200 by strength, full dedup in increment 4
        surv_pre = _cap_survivors(surv_pre)
        surv_post = _cap_survivors(surv_post)

        elapsed = time.time() - t0
        return (inst_id, surv_pre, surv_post, n_exprs, elapsed)

    except Exception as e:
        return (inst_id, [], [], 0, time.time() - t0, str(e))


def run_market_screening(all_signals, post_signals, n_workers=None,
                         wr_pp=10, mfe_adr=1.0, min_per_bucket=8):
    """Screen all market instruments in parallel.

    Returns:
        (all_survivors_pre, all_survivors_post, screening_stats)
    """
    if n_workers is None:
        n_workers = os.cpu_count() or 8
    print("\n  ── MARKET FEATURE SCREENING ──")
    t0 = time.time()

    # Load manifest
    if not os.path.exists(MKT_MANIFEST):
        print("  ERROR: Market series manifest not found")
        return [], [], {}
    with open(MKT_MANIFEST) as f:
        manifest = json.load(f)

    instruments = manifest.get("instruments", {})
    expr_names = manifest.get("expr_names", [])
    n_exprs = len(expr_names)
    print(f"  {len(instruments)} instruments × {n_exprs} expressions = "
          f"{len(instruments) * n_exprs:,} features to test")
    print(f"  Thresholds: WR ≥ {wr_pp}pp, MFE ≥ {mfe_adr} ADR, min/decile ≥ {min_per_bucket}")
    print(f"  Workers: {n_workers}")

    # Build signal arrays (serializable for multiprocessing)
    sig_dates_pre = [s["date"] for s in all_signals]
    is_win_pre = np.array(["WIN" in s.get("classification", "") for s in all_signals])
    moves_pre = np.array([s.get("move_adr") or np.nan for s in all_signals], dtype=np.float64)

    # Post-refinement: need to identify which of all_signals survived refinement
    post_dates_set = set((s.get("ticker"), s.get("signal_date", s.get("date")))
                         for s in post_signals)
    post_mask = []
    for s in all_signals:
        key = (s["ticker"], s["date"])
        post_mask.append(key in post_dates_set)
    post_mask = np.array(post_mask)

    # Extract post-refinement arrays from all_signals (preserving order)
    post_indices = np.where(post_mask)[0]
    sig_dates_post = [all_signals[i]["date"] for i in post_indices]
    is_win_post = is_win_pre[post_indices]
    moves_post = moves_pre[post_indices]

    print(f"  Pre-refinement: {len(sig_dates_pre)} signals ({int(is_win_pre.sum())}W)")
    print(f"  Post-refinement: {len(sig_dates_post)} signals ({int(is_win_post.sum())}W)")

    # Build work items
    work = []
    skipped = 0
    for inst_id, info in instruments.items():
        npz_path = os.path.join(MKT_DIR, _instrument_filename(inst_id))
        if not os.path.exists(npz_path):
            skipped += 1
            continue
        work.append((
            inst_id, npz_path,
            sig_dates_pre, sig_dates_post,
            is_win_pre, is_win_post,
            moves_pre, moves_post,
            n_exprs, expr_names,
            wr_pp, mfe_adr, min_per_bucket
        ))
    if skipped:
        print(f"  WARNING: {skipped} instruments missing .npz files")
    print(f"  Queued: {len(work)} instruments\n")

    # Run in parallel
    all_surv_pre = []
    all_surv_post = []
    completed = 0
    total_features = 0
    errors = []

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_screen_one_instrument, item): item[0] for item in work}

        for future in as_completed(futures):
            inst_id = futures[future]
            try:
                result = future.result()
                if len(result) == 6:
                    # Error case
                    _, sp, spo, nt, el, err = result
                    errors.append(f"{inst_id}: {err}")
                else:
                    _, sp, spo, nt, el = result
                    all_surv_pre.extend(sp)
                    all_surv_post.extend(spo)
                    total_features += nt
            except Exception as e:
                errors.append(f"{inst_id}: {e}")

            completed += 1
            if completed % 20 == 0 or completed == len(work):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 1
                eta = (len(work) - completed) / rate if rate > 0 else 0
                print(f"    {completed:3d}/{len(work)} instruments  "
                      f"[{elapsed:.0f}s, ~{eta:.0f}s left]  "
                      f"surv: {len(all_surv_pre)} pre / {len(all_surv_post)} post")

    elapsed = time.time() - t0

    if errors:
        print(f"\n  {len(errors)} errors (first 5):")
        for e in errors[:5]:
            print(f"    ✗ {e}")

    stats = {
        "n_instruments": len(work),
        "n_expressions_per_instrument": n_exprs,
        "n_features_tested": total_features * len(work),
        "n_survivors_pre": len(all_surv_pre),
        "n_survivors_post": len(all_surv_post),
        "thresholds": {"wr_pp": wr_pp, "mfe_adr": mfe_adr, "min_per_bucket": min_per_bucket},
        "elapsed_s": round(elapsed, 1),
        "n_errors": len(errors),
    }

    print(f"\n  Market screening complete ({elapsed:.1f}s)")
    print(f"  Features tested: ~{total_features * len(work):,}")
    print(f"  Survivors: {len(all_surv_pre)} pre-refinement, {len(all_surv_post)} post-refinement")
    return all_surv_pre, all_surv_post, stats


def screen_setup_features(all_signals, post_signals):
    """Screen the 10 setup features through the same WR/MFE logic.

    Returns:
        (survivors_pre, survivors_post, stats)
    """
    print("\n  ── SETUP FEATURE SCREENING ──")

    feat_keys = ["feat_price", "feat_adr", "feat_dollar_volume_20d",
                 "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
                 "feat_market_cap", "feat_volume_float_ratio",
                 "feat_rs_vs_sector", "feat_sector_rs_vs_spy"]
    feat_names = [k.replace("feat_", "") for k in feat_keys]

    n_pre = len(all_signals)

    # Build pre-refinement matrix
    vals_pre = np.full((n_pre, len(feat_keys)), np.nan, dtype=np.float64)
    is_win_pre = np.array(["WIN" in s.get("classification", "") for s in all_signals])
    moves_pre = np.array([s.get("move_adr") or np.nan for s in all_signals], dtype=np.float64)

    for si, s in enumerate(all_signals):
        for fi, fk in enumerate(feat_keys):
            v = s.get(fk)
            if v is not None:
                vals_pre[si, fi] = float(v)

    surv_pre = screen_features(vals_pre, is_win_pre, moves_pre, feat_names,
                               min_per_bucket=8, wr_pp_threshold=10, mfe_adr_threshold=1.0)
    # Tag source
    for s in surv_pre:
        fk = s["name"]
        s["source"] = "setup_fundamentals" if fk in (
            "market_cap", "volume_float_ratio", "rs_vs_sector", "sector_rs_vs_spy"
        ) else "setup_ohlcv"

    # Build post-refinement matrix
    post_dates_set = set((s.get("ticker"), s.get("signal_date", s.get("date")))
                         for s in post_signals)
    post_mask = np.array([
        (s["ticker"], s["date"]) in post_dates_set for s in all_signals
    ])
    post_indices = np.where(post_mask)[0]

    vals_post = vals_pre[post_indices, :]
    is_win_post = is_win_pre[post_indices]
    moves_post = moves_pre[post_indices]

    surv_post = screen_features(vals_post, is_win_post, moves_post, feat_names,
                                min_per_bucket=8, wr_pp_threshold=10, mfe_adr_threshold=1.0)
    for s in surv_post:
        fk = s["name"]
        s["source"] = "setup_fundamentals" if fk in (
            "market_cap", "volume_float_ratio", "rs_vs_sector", "sector_rs_vs_spy"
        ) else "setup_ohlcv"

    print(f"  Setup features: {len(surv_pre)} pre-refinement survivors, "
          f"{len(surv_post)} post-refinement survivors")
    for s in surv_pre:
        tag = s["screen_type"]
        print(f"    {s['name']:30s} {tag:10s} WR={s['wr_spread']:.1%}  "
              f"MFE={s['mfe_spread']:.1f}  dir={s['direction']}")

    stats = {
        "n_features": len(feat_keys),
        "n_survivors_pre": len(surv_pre),
        "n_survivors_post": len(surv_post),
    }
    return surv_pre, surv_post, stats




# ══════════════════════════════════════════════════════════════
# CROSS-INSTRUMENT DEDUP (INCREMENT 4)
# ══════════════════════════════════════════════════════════════

def _reload_instrument_values(args):
    """Worker: load one instrument's .npz and extract values for surviving expressions only.

    Args tuple:
        (instrument_id, npz_path, expr_col_indices, signal_dates)

    expr_col_indices: list of (survivor_global_idx, col_idx_in_npz) — only the
        expressions that survived screening for this instrument.

    Returns:
        (instrument_id, list_of (global_idx, values_array), elapsed_s)
        or (instrument_id, [], elapsed_s, error_str) on failure.
    """
    inst_id, npz_path, expr_col_indices, signal_dates = args
    from datetime import date as dt_date, timedelta
    t0 = time.time()
    try:
        loaded = np.load(npz_path, allow_pickle=True)
        data = loaded["data"]      # (n_bars, n_exprs)
        dates = loaded["dates"]    # string array

        # Build date → row index
        date_to_row = {}
        for i, d in enumerate(dates):
            date_to_row[str(d)] = i

        n_signals = len(signal_dates)
        n_bars = data.shape[0]

        # Pre-compute row indices for all signal dates (reused per expression)
        row_indices = np.full(n_signals, -1, dtype=np.int64)
        for si, sd in enumerate(signal_dates):
            ri = date_to_row.get(sd)
            if ri is None:
                for offset in range(1, 6):
                    try:
                        d = dt_date.fromisoformat(sd) - timedelta(days=offset)
                        ri = date_to_row.get(d.isoformat())
                        if ri is not None:
                            break
                    except (ValueError, TypeError):
                        break
            if ri is not None and ri < n_bars:
                row_indices[si] = ri

        # Vectorized extraction: gather all valid rows at once per column
        valid_mask = row_indices >= 0
        valid_rows = row_indices[valid_mask]

        results = []
        for global_idx, col_idx in expr_col_indices:
            vals = np.full(n_signals, np.nan, dtype=np.float64)
            vals[valid_mask] = data[valid_rows, col_idx].astype(np.float64)
            results.append((global_idx, vals))

        del data, loaded
        return (inst_id, results, time.time() - t0)
    except Exception as e:
        return (inst_id, [], time.time() - t0, str(e))


def _dedup_one_instrument_pass1(args):
    """Pass 1 worker: greedy dedup within one instrument's survivors.

    Returns (inst_id, kept_local_indices).
    """
    inst_id, values, strengths, corr_threshold, min_overlap = args
    n = values.shape[0]
    if n <= 1:
        return (inst_id, list(range(n)))

    order = np.argsort(-strengths)
    kept_idx = []
    kept_rows = []

    for idx in order:
        idx = int(idx)
        candidate = values[idx]
        cand_valid = np.isfinite(candidate)

        dominated = False
        for kr in kept_rows:
            both = cand_valid & np.isfinite(kr)
            nv = int(both.sum())
            if nv < min_overlap:
                continue
            cv = candidate[both]
            kv = kr[both]
            cs, ks = np.std(cv), np.std(kv)
            if cs < 1e-10 or ks < 1e-10:
                continue
            cm = cv - cv.mean()
            km = kv - kv.mean()
            r = np.dot(cm, km) / (cs * ks * nv)
            if abs(r) >= corr_threshold:
                dominated = True
                break

        if not dominated:
            kept_idx.append(idx)
            kept_rows.append(candidate)

    return (inst_id, kept_idx)


def _greedy_dedup_batched(values_matrix, strengths, corr_threshold=0.95, min_overlap=50):
    """Pass 2: greedy dedup with exact vectorized Pearson correlation.

    For each candidate, computes exact correlation against all kept features
    using pre-allocated arrays and vectorized math. No approximation.

    Key insight: with ~2-5K features after pass 1 and ~893 signal values,
    the exact approach is fast enough (each correlation is a dot product
    on ~893 floats). Pre-allocated arrays avoid per-iteration allocation.
    """
    n, n_sig = values_matrix.shape
    if n <= 1:
        return list(range(n))

    order = np.argsort(-np.array(strengths))

    # Replace non-finite values with 0 for dot products, track valid masks separately
    valid_masks = np.isfinite(values_matrix)
    filled = np.where(valid_masks, values_matrix, 0.0)
    row_valid_counts = valid_masks.sum(axis=1)

    kept_idx = []

    # Pre-allocate arrays for kept features
    cap = min(512, n)
    kept_filled = np.zeros((cap, n_sig), dtype=np.float64)
    kept_valid = np.zeros((cap, n_sig), dtype=bool)
    n_kept = 0

    def _grow():
        nonlocal cap, kept_filled, kept_valid
        old_cap = cap
        cap *= 2
        kept_filled = np.vstack([kept_filled, np.zeros((old_cap, n_sig), dtype=np.float64)])
        kept_valid = np.vstack([kept_valid, np.zeros((old_cap, n_sig), dtype=bool)])

    for rank, oi in enumerate(order):
        oi = int(oi)

        if n_kept == 0:
            kept_idx.append(oi)
            kept_filled[0] = filled[oi]
            kept_valid[0] = valid_masks[oi]
            n_kept = 1
            continue

        cand_f = filled[oi]       # (n_sig,) with 0 where NaN
        cand_v = valid_masks[oi]  # (n_sig,) bool
        n_cand = int(row_valid_counts[oi])

        if n_cand < min_overlap:
            # Too few valid values to correlate — keep it
            kept_idx.append(oi)
            if n_kept >= cap:
                _grow()
            kept_filled[n_kept] = cand_f
            kept_valid[n_kept] = cand_v
            n_kept += 1
            continue

        # Compute exact Pearson correlation against all kept features
        # For each kept feature k:
        #   overlap = cand_valid & kept_valid[k]
        #   n_overlap = sum(overlap)
        #   sum_x = sum(cand[overlap]), sum_y = sum(kept[overlap])
        #   sum_xy = sum(cand[overlap] * kept[overlap])
        #   sum_x2 = sum(cand[overlap]^2), sum_y2 = sum(kept[overlap]^2)
        #   r = (n*sum_xy - sum_x*sum_y) / sqrt((n*sum_x2 - sum_x^2)(n*sum_y2 - sum_y^2))
        #
        # Vectorized: use element-wise multiply with valid masks

        kv = kept_valid[:n_kept]   # (k, n_sig) bool
        kf = kept_filled[:n_kept]  # (k, n_sig) float

        # Overlap mask: both valid
        overlap = kv & cand_v  # (k, n_sig)
        n_overlap = overlap.sum(axis=1).astype(np.float64)  # (k,)

        # Masked values: zero where not overlapping
        cx = np.where(overlap, cand_f, 0.0)  # (k, n_sig) — candidate broadcast
        kx = np.where(overlap, kf, 0.0)      # (k, n_sig)

        sum_x = cx.sum(axis=1)       # (k,)
        sum_y = kx.sum(axis=1)       # (k,)
        sum_xy = (cx * kx).sum(axis=1)  # (k,)
        sum_x2 = (cx * cx).sum(axis=1)  # (k,)
        sum_y2 = (kx * kx).sum(axis=1)  # (k,)

        # Pearson formula
        with np.errstate(divide='ignore', invalid='ignore'):
            numerator = n_overlap * sum_xy - sum_x * sum_y
            denom_x = n_overlap * sum_x2 - sum_x * sum_x
            denom_y = n_overlap * sum_y2 - sum_y * sum_y
            denom = np.sqrt(denom_x * denom_y)
            corr = np.abs(numerator / denom)

        # Mask out invalid: insufficient overlap or zero denominator
        corr = np.where((n_overlap >= min_overlap) & np.isfinite(corr), corr, 0.0)

        max_corr = float(corr.max())
        if max_corr >= corr_threshold:
            continue  # dominated — skip

        # Not dominated — keep
        kept_idx.append(oi)
        if n_kept >= cap:
            _grow()
        kept_filled[n_kept] = cand_f
        kept_valid[n_kept] = cand_v
        n_kept += 1

        if (rank + 1) % 500 == 0:
            print(f"      Pass 2: {rank+1}/{n} checked, {n_kept} kept")

    return kept_idx


def run_cross_dedup(survivors, signal_dates, label, n_workers=None, corr_threshold=0.95):
    """Two-pass dedup: within-instrument (parallel) then cross-instrument (batched).

    Pass 1: For each instrument, dedup its survivors against each other.
             Catches SMA20/SMA21/EMA20 overlaps. Embarrassingly parallel.
             Reduces ~51K to ~2-5K.

    Pass 2: Dedup across all instruments using batched matrix-multiply correlation.
             Catches SPY_SMA20 ≈ QQQ_SMA20. Fast on the reduced set.

    Args:
        survivors: list of dicts from screening (setup + market combined).
        signal_dates: list of str (dates for the signal set).
        label: str for logging ("pre" or "post").
        n_workers: int or None (default: all CPU cores).
        corr_threshold: float, dedup threshold.

    Returns:
        (deduped_survivors, dedup_stats)
    """
    import os as _os
    if n_workers is None:
        n_workers = _os.cpu_count() or 8
    print(f"\n  ── CROSS-INSTRUMENT DEDUP ({label.upper()}) ──")
    t0 = time.time()
    n_total = len(survivors)
    n_signals = len(signal_dates)
    print(f"  Input: {n_total} survivors, {n_signals} signals, {n_workers} cores")
    if n_total == 0:
        return [], {"input": 0, "output": 0, "dropped": 0}

    # Separate setup vs market survivors
    setup_indices = [i for i, s in enumerate(survivors) if s.get("source") != "market"]
    market_indices = [i for i, s in enumerate(survivors) if s.get("source") == "market"]
    print(f"  Setup: {len(setup_indices)}, Market: {len(market_indices)}")

    # ── RELOAD VALUES ──
    # Setup survivors: values already in memory from screen_features
    all_values = [None] * n_total
    for gi in setup_indices:
        v = survivors[gi].get("values")
        if v is not None:
            all_values[gi] = np.array(v, dtype=np.float64) if not isinstance(v, np.ndarray) else v.astype(np.float64)
        else:
            print(f"    WARNING: setup survivor '{survivors[gi]['name']}' missing values")
            all_values[gi] = np.full(n_signals, np.nan, dtype=np.float64)

    # Market survivors: reload from .npz files in parallel
    inst_groups = defaultdict(list)
    for gi in market_indices:
        s = survivors[gi]
        inst = s.get("instrument")
        col = s.get("col_idx")
        if inst is not None and col is not None:
            inst_groups[inst].append((gi, col))

    n_instruments = len(inst_groups)
    if n_instruments > 0:
        print(f"  Reloading values from {n_instruments} instruments...")
        t_reload = time.time()
        work = []
        for inst_id, idx_cols in inst_groups.items():
            npz_path = os.path.join(MKT_DIR, _instrument_filename(inst_id))
            if not os.path.exists(npz_path):
                for gi, _ in idx_cols:
                    all_values[gi] = np.full(n_signals, np.nan, dtype=np.float64)
                continue
            work.append((inst_id, npz_path, idx_cols, signal_dates))

        errors = []
        loaded = 0
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_reload_instrument_values, item): item[0] for item in work}
            for future in as_completed(futures):
                inst_id = futures[future]
                try:
                    result = future.result()
                    if len(result) == 4:
                        _, _, _, err = result
                        errors.append(f"{inst_id}: {err}")
                        for gi, _ in inst_groups[inst_id]:
                            all_values[gi] = np.full(n_signals, np.nan, dtype=np.float64)
                    else:
                        _, pairs, _ = result
                        for gi, vals in pairs:
                            all_values[gi] = vals
                except Exception as e:
                    errors.append(f"{inst_id}: {e}")
                    for gi, _ in inst_groups[inst_id]:
                        all_values[gi] = np.full(n_signals, np.nan, dtype=np.float64)
                loaded += 1
                if loaded % 50 == 0 or loaded == len(work):
                    print(f"    Loaded {loaded}/{len(work)} instruments")

        if errors:
            print(f"  {len(errors)} load errors (first 3):")
            for e in errors[:3]:
                print(f"    ✗ {e}")
        print(f"  Reload: {time.time() - t_reload:.1f}s")

    # Fill any remaining Nones
    for gi in range(n_total):
        if all_values[gi] is None:
            all_values[gi] = np.full(n_signals, np.nan, dtype=np.float64)

    # Compute strengths for all survivors
    all_strengths = [max(s["wr_spread"], s["mfe_spread"] / 10.0) for s in survivors]

    # ── PASS 1: WITHIN-INSTRUMENT DEDUP (parallel) ──
    print(f"\n  Pass 1: Within-instrument dedup...")
    t_p1 = time.time()

    # Group survivors by instrument (setup features = instrument "setup")
    inst_survivor_map = defaultdict(list)  # inst_id → list of global indices
    for gi in setup_indices:
        inst_survivor_map["__setup__"].append(gi)
    for gi in market_indices:
        inst = survivors[gi].get("instrument", "__unknown__")
        inst_survivor_map[inst].append(gi)

    # Build work items for parallel pass 1
    pass1_work = []
    for inst_id, global_indices in inst_survivor_map.items():
        if len(global_indices) <= 1:
            continue  # nothing to dedup
        vals = np.array([all_values[gi] for gi in global_indices], dtype=np.float64)
        strs = np.array([all_strengths[gi] for gi in global_indices])
        pass1_work.append((inst_id, vals, strs, corr_threshold, 50))

    # Instruments with only 1 survivor pass through directly
    pass1_kept_global = []
    for inst_id, global_indices in inst_survivor_map.items():
        if len(global_indices) <= 1:
            pass1_kept_global.extend(global_indices)

    # Run pass 1 in parallel
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        for result in pool.map(_dedup_one_instrument_pass1, pass1_work):
            inst_id, local_kept = result
            global_indices = inst_survivor_map[inst_id]
            for ki in local_kept:
                pass1_kept_global.append(global_indices[ki])

    pass1_time = time.time() - t_p1
    n_after_p1 = len(pass1_kept_global)
    print(f"  Pass 1: {n_total} → {n_after_p1} ({pass1_time:.1f}s)")

    # ── PASS 1.5: SAME-EXPRESSION DEDUP (instant) ──
    # If the same expression survived on multiple instruments (e.g.,
    # m_slope_ratio_xavgc8_xavgc50_off10 on VYM and NOBL), keep only
    # the strongest one. These are almost always correlated >0.95 because
    # correlated instruments produce near-identical expression values.
    # This is O(n) grouping — instant — and crushes 24K down to ~2-3K.
    print(f"\n  Pass 1.5: Same-expression dedup...")
    t_p15 = time.time()

    expr_groups = defaultdict(list)  # expression_name → list of (global_idx, strength)
    for gi in pass1_kept_global:
        s = survivors[gi]
        # Setup features don't have an expression field — keep them all
        if s.get("source") != "market":
            expr_groups[f"__setup__{s['name']}"].append((gi, all_strengths[gi]))
        else:
            expr_name = s.get("expression", s["name"])
            expr_groups[expr_name].append((gi, all_strengths[gi]))

    pass15_kept_global = []
    for expr_name, candidates in expr_groups.items():
        # Keep only the strongest per expression
        best_gi, best_str = max(candidates, key=lambda x: x[1])
        pass15_kept_global.append(best_gi)

    pass15_time = time.time() - t_p15
    n_after_p15 = len(pass15_kept_global)
    n_expr_dupes = n_after_p1 - n_after_p15
    print(f"  Pass 1.5: {n_after_p1} → {n_after_p15} ({n_expr_dupes} same-expression dupes, {pass15_time:.1f}s)")

    # ── PASS 2: CROSS-INSTRUMENT DEDUP (batched) ──
    print(f"\n  Pass 2: Cross-instrument dedup (batched)...")
    t_p2 = time.time()

    cross_vals = np.array([all_values[gi] for gi in pass15_kept_global], dtype=np.float64)
    cross_strs = np.array([all_strengths[gi] for gi in pass15_kept_global])

    kept_cross_local = _greedy_dedup_batched(cross_vals, cross_strs, corr_threshold, 50)

    # Map back to global indices
    final_global = [pass15_kept_global[i] for i in kept_cross_local]

    pass2_time = time.time() - t_p2
    print(f"  Pass 2: {n_after_p15} → {len(final_global)} ({pass2_time:.1f}s)")

    # Build deduped survivor list with values attached
    deduped = []
    for gi in final_global:
        s = survivors[gi].copy()
        s["values"] = all_values[gi]
        deduped.append(s)

    # Stats
    n_kept_setup = sum(1 for s in deduped if s.get("source") != "market")
    n_kept_market = sum(1 for s in deduped if s.get("source") == "market")
    n_dropped = n_total - len(deduped)
    elapsed = time.time() - t0

    print(f"\n  Dedup complete ({elapsed:.1f}s)")
    print(f"  {n_total} → {len(deduped)} ({n_dropped} dropped, {n_dropped/max(n_total,1)*100:.1f}%)")
    print(f"  Kept: {n_kept_setup} setup + {n_kept_market} market")

    if n_kept_market:
        kept_insts = Counter(s.get("instrument") for s in deduped if s.get("source") == "market")
        print(f"  Unique instruments: {len(kept_insts)}")
        print(f"  Top 10 by count:")
        for inst, cnt in kept_insts.most_common(10):
            print(f"    {inst}: {cnt}")

    stats = {
        "input": n_total,
        "after_pass1": n_after_p1,
        "after_pass15": n_after_p15,
        "output": len(deduped),
        "dropped": n_dropped,
        "drop_pct": round(n_dropped / max(n_total, 1) * 100, 1),
        "corr_threshold": corr_threshold,
        "n_setup_kept": n_kept_setup,
        "n_market_kept": n_kept_market,
        "n_instruments_kept": len(set(s.get("instrument") for s in deduped if s.get("source") == "market")),
        "pass1_time_s": round(pass1_time, 1),
        "pass15_time_s": round(pass15_time, 1),
        "pass15_expr_dupes_removed": n_expr_dupes,
        "pass2_time_s": round(pass2_time, 1),
        "reload_time_s": round(elapsed - pass1_time - pass15_time - pass2_time, 1),
        "total_time_s": round(elapsed, 1),
    }
    return deduped, stats




# ══════════════════════════════════════════════════════════════
# SCORING CURVES + SIGNAL SCORING (INCREMENT 5)
# ══════════════════════════════════════════════════════════════

def build_scoring_curves(deduped_survivors, all_signals, label):
    """Build percentile-based scoring curves for each surviving feature.

    For each feature:
      1. Compute each signal's percentile rank (0-100) within the feature's distribution
      2. Flip descending features so higher = better
      3. Preserve decile curves (already computed in screening) for WR/MFE interpolation

    Args:
        deduped_survivors: list of dicts from dedup step (with 'values' arrays attached)
        all_signals: list of signal dicts
        label: str ("pre" or "post")

    Returns:
        (enriched_features, percentile_matrix)
        enriched_features: list of feature dicts with scoring curve data
        percentile_matrix: np.ndarray shape (n_signals, n_features) float 0-100
    """
    print(f"\n  ── SCORING CURVES ({label.upper()}) ──")
    t0 = time.time()
    n_signals = len(all_signals)
    n_features = len(deduped_survivors)
    print(f"  {n_signals} signals × {n_features} features")

    if n_features == 0:
        return [], np.zeros((n_signals, 0), dtype=np.float64)

    percentile_matrix = np.full((n_signals, n_features), 50.0, dtype=np.float64)
    enriched = []

    for fi, surv in enumerate(deduped_survivors):
        vals = surv.get("values")
        if vals is None:
            enriched.append(_build_feature_dict(surv, fi))
            continue

        vals = np.asarray(vals, dtype=np.float64)
        valid_mask = np.isfinite(vals)
        n_valid = int(valid_mask.sum())

        if n_valid < 20:
            enriched.append(_build_feature_dict(surv, fi))
            continue

        # Percentile rank among valid values: rankdata gives 1..n_valid, convert to 0..100
        valid_vals = vals[valid_mask]
        ranks = rankdata(valid_vals, method='average')
        percentiles = (ranks - 1) / max(n_valid - 1, 1) * 100.0

        # Write percentiles into matrix (NaN positions stay at 50 = neutral)
        percentile_matrix[valid_mask, fi] = percentiles

        # Flip descending features so higher percentile = better outcome
        direction = surv.get("direction", "ascending")
        if direction == "descending":
            percentile_matrix[:, fi] = 100.0 - percentile_matrix[:, fi]

        enriched.append(_build_feature_dict(surv, fi))

    elapsed = time.time() - t0
    print(f"  Scoring curves built ({elapsed:.1f}s)")
    return enriched, percentile_matrix


def _build_feature_dict(surv, col_idx):
    """Build the output feature dict (no 'values' array — stripped before save)."""
    return {
        "name": surv.get("name"),
        "source": surv.get("source", "market"),
        "instrument": surv.get("instrument"),
        "expression": surv.get("expression"),
        "screen_type": surv.get("screen_type"),
        "wr_spread": surv.get("wr_spread"),
        "mfe_spread": surv.get("mfe_spread"),
        "direction": surv.get("direction"),
        "weight": max(surv.get("wr_spread", 0), surv.get("mfe_spread", 0) / 10.0),
        "decile_boundaries": surv.get("decile_boundaries"),
        "decile_wr": surv.get("decile_wr"),
        "decile_mfe": surv.get("decile_mfe"),
        "n_per_decile": surv.get("n_per_decile"),
        "col_idx": col_idx,
    }


def _vectorized_interp_decile(pct_array, decile_values):
    """Vectorized interpolation of a decile curve for an array of percentiles.

    pct_array: np.ndarray shape (n_signals,) of percentiles 0-100
    decile_values: list of 10 values (D1..D10), may contain None

    Returns: np.ndarray shape (n_signals,) with interpolated values. NaN where
             the decile curve has None/NaN at the relevant points.
    """
    if decile_values is None or len(decile_values) != 10:
        return np.full(len(pct_array), np.nan, dtype=np.float64)

    # Convert decile values to numpy, None -> NaN
    dv = np.array([float(v) if v is not None else np.nan for v in decile_values], dtype=np.float64)

    # Midpoints: D1=5, D2=15, ..., D10=95
    midpoints = np.array([5.0, 15.0, 25.0, 35.0, 45.0, 55.0, 65.0, 75.0, 85.0, 95.0])

    # Clamp percentiles to 5..95
    p = np.clip(pct_array, 5.0, 95.0)

    # Find which interval each percentile falls in: searchsorted gives index of
    # first midpoint > p, so the interval is (idx-1, idx). Clamp to valid range.
    idx = np.searchsorted(midpoints, p, side='right')  # idx in 1..10
    idx = np.clip(idx, 1, 9)  # interval indices 1..9 -> pairs (0,1)..(8,9)

    lo_idx = idx - 1
    hi_idx = idx

    lo_val = dv[lo_idx]
    hi_val = dv[hi_idx]
    lo_mid = midpoints[lo_idx]
    hi_mid = midpoints[hi_idx]

    # Interpolation fraction
    with np.errstate(divide='ignore', invalid='ignore'):
        frac = (p - lo_mid) / (hi_mid - lo_mid)

    result = lo_val + frac * (hi_val - lo_val)

    # NaN where either endpoint is NaN
    bad = ~(np.isfinite(lo_val) & np.isfinite(hi_val))
    result[bad] = np.nan

    return result


def score_signals(percentile_matrix, features, all_signals, label):
    """Score every signal using percentile-based continuous scoring (fully vectorized).

    For each signal:
      1. quality_score = weighted average of its feature percentiles (0-100)
      2. predicted_wr = weighted interpolation of decile WR curves
      3. predicted_mfe = weighted interpolation of decile MFE curves
      4. ev = (predicted_wr * predicted_mfe) - ((1 - predicted_wr) * 1.0)

    All computation is vectorized — no Python loops over signals.
    Loop over ~1800 features does vectorized ops on all 893 signals at once.
    """
    print(f"\n  ── SIGNAL SCORING ({label.upper()}) ──")
    t0 = time.time()
    n_signals, n_features = percentile_matrix.shape
    print(f"  {n_signals} signals × {n_features} features")

    if n_features == 0:
        print("  WARNING: No features — all signals get neutral scores")
        return [{"quality_score": 50.0, "setup_score": 50.0, "market_score": 50.0,
                 "predicted_wr": 0.5, "predicted_mfe": 0.0,
                 "ev": 0.0} for _ in range(n_signals)]

    # Category-balanced weighting: market features and setup features each get
    # 50% of total weight. Without this, 1800 market features drown out 3 setup
    # features even though each individual feature has similar screening strength.
    raw_weights = np.array([f.get("weight", 0.0) for f in features], dtype=np.float64)
    is_market = np.array([f.get("source") == "market" for f in features])
    is_setup = ~is_market

    market_sum = raw_weights[is_market].sum()
    setup_sum = raw_weights[is_setup].sum()

    weights = np.zeros_like(raw_weights)
    if market_sum > 1e-10 and setup_sum > 1e-10:
        # Both categories present: each gets 50%
        weights[is_market] = raw_weights[is_market] / market_sum * 0.5
        weights[is_setup] = raw_weights[is_setup] / setup_sum * 0.5
    elif market_sum > 1e-10:
        weights[is_market] = raw_weights[is_market] / market_sum
    elif setup_sum > 1e-10:
        weights[is_setup] = raw_weights[is_setup] / setup_sum

    total_weight = weights.sum()
    if total_weight < 1e-10:
        total_weight = 1.0

    n_mkt = int(is_market.sum())
    n_stp = int(is_setup.sum())
    print(f"  Weights: {n_mkt} market (50%) + {n_stp} setup (50%) = {n_features} features")

    # 1. Quality score: matrix-vector multiply (already vectorized)
    quality_scores = percentile_matrix @ weights / total_weight  # (n_signals,)

    # 1b. Component scores: setup-only and market-only sub-scores for Scan Tuning sliders
    setup_weights = np.zeros_like(raw_weights)
    market_weights = np.zeros_like(raw_weights)
    if setup_sum > 1e-10:
        setup_weights[is_setup] = raw_weights[is_setup] / setup_sum
    if market_sum > 1e-10:
        market_weights[is_market] = raw_weights[is_market] / market_sum
    setup_wt = setup_weights.sum()
    market_wt = market_weights.sum()
    setup_scores = (percentile_matrix @ setup_weights / setup_wt) if setup_wt > 1e-10 else np.full(n_signals, 50.0)
    market_scores = (percentile_matrix @ market_weights / market_wt) if market_wt > 1e-10 else np.full(n_signals, 50.0)

    # 2 & 3. Predicted WR and MFE: vectorized decile interpolation
    # For each feature, un-flip percentiles to raw distribution position,
    # then interpolate all signals at once via _vectorized_interp_decile.
    # Accumulate weighted sums using numpy arrays, not Python floats.

    wr_numer = np.zeros(n_signals, dtype=np.float64)
    wr_denom = np.zeros(n_signals, dtype=np.float64)
    mfe_numer = np.zeros(n_signals, dtype=np.float64)
    mfe_denom = np.zeros(n_signals, dtype=np.float64)

    for fi, feat in enumerate(features):
        w = weights[fi]
        if w < 1e-10:
            continue

        # Un-flip: percentile_matrix is direction-flipped (higher=better).
        # For decile lookup we need raw distribution position.
        direction = feat.get("direction", "ascending")
        if direction == "descending":
            raw_pct = 100.0 - percentile_matrix[:, fi]
        else:
            raw_pct = percentile_matrix[:, fi]

        # WR interpolation — all signals at once
        decile_wr = feat.get("decile_wr")
        if decile_wr and len(decile_wr) == 10:
            wr_vals = _vectorized_interp_decile(raw_pct, decile_wr)
            valid = np.isfinite(wr_vals)
            wr_numer[valid] += wr_vals[valid] * w
            wr_denom[valid] += w

        # MFE interpolation — all signals at once
        decile_mfe = feat.get("decile_mfe")
        if decile_mfe and len(decile_mfe) == 10:
            mfe_vals = _vectorized_interp_decile(raw_pct, decile_mfe)
            valid = np.isfinite(mfe_vals)
            mfe_numer[valid] += mfe_vals[valid] * w
            mfe_denom[valid] += w

    # Compute final WR and MFE arrays
    with np.errstate(divide='ignore', invalid='ignore'):
        predicted_wr = np.where(wr_denom > 0, wr_numer / wr_denom, 0.5)
    predicted_wr = np.clip(predicted_wr, 0.01, 0.99)

    with np.errstate(divide='ignore', invalid='ignore'):
        predicted_mfe = np.where(mfe_denom > 0, mfe_numer / mfe_denom, 0.0)
    predicted_mfe = np.maximum(predicted_mfe, 0.0)

    # 4. EV = (WR * MFE) - ((1 - WR) * 1.0 ADR assumed stop)
    ev = predicted_wr * predicted_mfe - (1.0 - predicted_wr) * 1.0

    elapsed = time.time() - t0

    # Build results list
    results = []
    for si in range(n_signals):
        results.append({
            "quality_score": round(float(quality_scores[si]), 2),
            "setup_score": round(float(setup_scores[si]), 2),
            "market_score": round(float(market_scores[si]), 2),
            "predicted_wr": round(float(predicted_wr[si]), 4),
            "predicted_mfe": round(float(predicted_mfe[si]), 3),
            "ev": round(float(ev[si]), 3),
        })

    # Verification
    print(f"\n  Score distributions:")
    print(f"    quality_score: min={quality_scores.min():.1f} med={np.median(quality_scores):.1f} max={quality_scores.max():.1f}")
    print(f"    setup_score:   min={setup_scores.min():.1f} med={np.median(setup_scores):.1f} max={setup_scores.max():.1f}")
    print(f"    market_score:  min={market_scores.min():.1f} med={np.median(market_scores):.1f} max={market_scores.max():.1f}")
    print(f"    predicted_wr:  min={predicted_wr.min():.4f} med={np.median(predicted_wr):.4f} max={predicted_wr.max():.4f}")
    print(f"    predicted_mfe: min={predicted_mfe.min():.2f} med={np.median(predicted_mfe):.2f} max={predicted_mfe.max():.2f}")
    print(f"    ev:            min={ev.min():.3f} med={np.median(ev):.3f} max={ev.max():.3f}")

    # Sanity: top decile by quality_score should have higher actual WR
    is_winner = np.array(["WIN" in s.get("classification", "") for s in all_signals])
    n_dec = max(n_signals // 10, 1)
    order = np.argsort(quality_scores)
    bot_wr = float(is_winner[order[:n_dec]].mean())
    top_wr = float(is_winner[order[-n_dec:]].mean())
    print(f"\n  Calibration check:")
    print(f"    Bottom 10% by quality_score: actual WR = {bot_wr:.1%}")
    print(f"    Top 10% by quality_score:    actual WR = {top_wr:.1%}")
    if top_wr > bot_wr:
        print(f"    ✓ Top > Bottom by {top_wr - bot_wr:.1%}")
    else:
        print(f"    ⚠ WARNING: Top ({top_wr:.1%}) ≤ Bottom ({bot_wr:.1%}) — model may not be calibrated")

    example_indices = [i for i, s in enumerate(all_signals) if s.get("is_example")]
    examples_scored = sum(1 for i in example_indices if results[i]["quality_score"] is not None)
    print(f"\n  Examples scored: {examples_scored}/{len(example_indices)}")
    print(f"  Signal scoring complete ({elapsed:.1f}s)")
    return results


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════


def run(setup_type):
    print("\n" + "=" * 70)
    print("  EV GRINDER — Phase 3 Correlative Scoring")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  CPUs:  {os.cpu_count()}")
    t_total = time.time()

    cp, cf = find_raw_clusters(setup_type)
    if not cp:
        print(f"\n  ERROR: No raw clusters"); return None
    print(f"\n  Raw clusters: {cf}")
    all_signals, _ = load_clusters(cp)
    nw = sum(1 for s in all_signals if "WIN" in s["classification"])
    nl = sum(1 for s in all_signals if "LOSS" in s["classification"])
    ne = sum(1 for s in all_signals if s["is_example"])
    print(f"  Pre: {len(all_signals)} ({nw}W+{nl}L, {ne} ex)")

    rp, rf = find_latest_refinement(setup_type)
    if not rp:
        print(f"\n  ERROR: No refinement"); return None
    print(f"\n  Refinement: {rf}")
    rc, ps, _, _ = load_refinement(rp)
    npw = sum(1 for s in ps if "WIN" in s.get("classification", ""))
    npl = sum(1 for s in ps if "LOSS" in s.get("classification", ""))
    print(f"  Post: {len(ps)} ({npw}W+{npl}L), Conditions: {len(rc)}")

    print(f"\n  Loading expression cache...")
    from expr_cache_builder import ExprSeriesCache
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("  ERROR: Expression cache invalid"); return None
    print(f"  Expr cache: {ec.n_expressions} expressions")

    # Inc 1: Refinement depth replay
    kad, ds, peel = replay_refinement_depth(all_signals, rc, ec)
    print(f"\n  ── DEPTH VERIFICATION ──")
    d0, dm = ds[0], ds[len(rc)]
    checks = [
        ("D0 total", d0["total"], len(all_signals)),
        ("D0 winners", d0["winners"], nw),
        (f"D{len(rc)} total", dm["total"], len(ps)),
        (f"D{len(rc)} losers", dm["losers"], npl),
        ("Monotonic", all(ds[i]["total"] <= ds[i-1]["total"] for i in range(1, len(ds))), True),
        ("Winners const", all(d["winners"] == nw for d in ds), True),
        ("Kill sum", sum(len(c["clusters_killed"]) for c in peel), len(kad)),
    ]
    dok = all(a == e for _, a, e in checks)
    for l, a, e in checks:
        if a != e: print(f"  ✗ {l}: {a} != {e}")
    if dok: print(f"  ✓ All {len(checks)} depth checks passed")

    # Inc 2: Setup features
    fcov = compute_setup_features(all_signals)
    fok, fcomp = validate_setup_features(all_signals)

    # Inc 3: Feature screening
    n_workers = os.cpu_count() or 8
    setup_surv_pre, setup_surv_post, setup_stats = screen_setup_features(all_signals, ps)
    mkt_surv_pre, mkt_surv_post, mkt_stats = run_market_screening(
        all_signals, ps, n_workers=n_workers)

    for s in mkt_surv_pre + mkt_surv_post:
        s["source"] = "market"
        parts = s["name"].split("__", 1)
        s["instrument"] = parts[0] if len(parts) == 2 else None
        s["expression"] = parts[1] if len(parts) == 2 else s["name"]

    total_pre = len(setup_surv_pre) + len(mkt_surv_pre)
    total_post = len(setup_surv_post) + len(mkt_surv_post)
    print(f"\n  ── SCREENING SUMMARY ──")
    print(f"  Setup features:  {len(setup_surv_pre)} pre / {len(setup_surv_post)} post")
    print(f"  Market features: {len(mkt_surv_pre)} pre / {len(mkt_surv_post)} post")
    print(f"  Total survivors: {total_pre} pre / {total_post} post")

    example_sigs = [s for s in all_signals if s["is_example"]]
    ex_ok = len(example_sigs) == ne
    print(f"  Examples: {len(example_sigs)}/{ne} {'✓' if ex_ok else '✗'}")

    # Inc 4: Cross-instrument dedup
    combined_pre = setup_surv_pre + mkt_surv_pre
    combined_post = setup_surv_post + mkt_surv_post

    sig_dates_pre = [s["date"] for s in all_signals]
    post_dates_set = set((s.get("ticker"), s.get("signal_date", s.get("date")))
                         for s in ps)
    post_indices = [i for i, s in enumerate(all_signals)
                    if (s["ticker"], s["date"]) in post_dates_set]
    sig_dates_post = [all_signals[i]["date"] for i in post_indices]

    deduped_pre, dedup_stats_pre = run_cross_dedup(
        combined_pre, sig_dates_pre, "pre", n_workers=n_workers)
    deduped_post, dedup_stats_post = run_cross_dedup(
        combined_post, sig_dates_post, "post", n_workers=n_workers)

    # Dedup verification
    print(f"\n  ── DEDUP VERIFICATION ──")
    def _verify_no_high_corr(deduped, label, threshold=0.95):
        n = len(deduped)
        if n < 2:
            print(f"  {label}: {n} features, skip correlation check")
            return True, 0
        max_corr = 0.0
        max_pair = ("", "")
        violations = 0
        check_limit = min(n, 500)
        for i in range(check_limit):
            vi = deduped[i].get("values")
            if vi is None: continue
            for j in range(i + 1, check_limit):
                vj = deduped[j].get("values")
                if vj is None: continue
                both = np.isfinite(vi) & np.isfinite(vj)
                nv = int(both.sum())
                if nv < 50: continue
                ci, cj = vi[both], vj[both]
                if np.std(ci) < 1e-10 or np.std(cj) < 1e-10: continue
                c = abs(np.corrcoef(ci, cj)[0, 1])
                if c > max_corr:
                    max_corr = c
                    max_pair = (deduped[i]["name"], deduped[j]["name"])
                if c >= threshold: violations += 1
        ok = violations == 0
        print(f"  {label}: {n} features, max_corr={max_corr:.4f}, violations={violations} "
              f"{'✓' if ok else '✗'}")
        if not ok:
            print(f"    Worst pair: {max_pair[0]} ↔ {max_pair[1]}")
        return ok, violations

    corr_ok_pre, v_pre = _verify_no_high_corr(deduped_pre, "Pre")
    corr_ok_post, v_post = _verify_no_high_corr(deduped_post, "Post")

    # ── Inc 5: Scoring curves + signal scoring ──
    post_signals_for_scoring = [all_signals[i] for i in post_indices]

    features_pre, pct_matrix_pre = build_scoring_curves(deduped_pre, all_signals, "pre")
    scores_pre = score_signals(pct_matrix_pre, features_pre, all_signals, "pre")

    features_post, pct_matrix_post = build_scoring_curves(deduped_post, post_signals_for_scoring, "post")
    scores_post = score_signals(pct_matrix_post, features_post, post_signals_for_scoring, "post")

    # Hard fail: all examples must be scored
    example_scored_count = sum(
        1 for i, s in enumerate(all_signals)
        if s.get("is_example") and scores_pre[i]["quality_score"] is not None
    )
    if example_scored_count < ne:
        print(f"\n  ✗ HARD FAIL: Only {example_scored_count}/{ne} examples scored")
        return None
    print(f"\n  ✓ All {ne} examples scored")

    # ── Inc 6: Decile calibration tables ──
    print(f"\n  ── DECILE CALIBRATION ──")

    def _build_calibration(scores, signals, label):
        """Build 10-row calibration table: predicted vs actual WR per quality_score decile."""
        n = len(scores)
        if n < 20:
            print(f"  {label}: too few signals ({n})")
            return []
        order = sorted(range(n), key=lambda i: scores[i]["quality_score"])
        dec_size = n // 10
        rows = []
        for d in range(10):
            start = d * dec_size
            end = (d + 1) * dec_size if d < 9 else n
            chunk_idx = order[start:end]
            actual_wins = sum(1 for i in chunk_idx if "WIN" in signals[i].get("classification", ""))
            actual_wr = actual_wins / len(chunk_idx)
            avg_qs = sum(scores[i]["quality_score"] for i in chunk_idx) / len(chunk_idx)
            avg_pwr = sum(scores[i]["predicted_wr"] for i in chunk_idx) / len(chunk_idx)
            avg_pmfe = sum(scores[i]["predicted_mfe"] for i in chunk_idx) / len(chunk_idx)
            avg_ev = sum(scores[i]["ev"] for i in chunk_idx) / len(chunk_idx)
            # Actual MFE among winners in this decile
            winner_moves = [signals[i].get("move_adr") for i in chunk_idx
                           if "WIN" in signals[i].get("classification", "")
                           and signals[i].get("move_adr") is not None]
            actual_mfe = sorted(winner_moves)[len(winner_moves)//2] if winner_moves else None
            row = {
                "decile": d + 1,
                "n": len(chunk_idx),
                "avg_quality_score": round(avg_qs, 1),
                "predicted_wr": round(avg_pwr, 4),
                "actual_wr": round(actual_wr, 4),
                "wr_error": round(avg_pwr - actual_wr, 4),
                "predicted_mfe": round(avg_pmfe, 2),
                "actual_mfe": round(actual_mfe, 2) if actual_mfe is not None else None,
                "avg_ev": round(avg_ev, 3),
            }
            rows.append(row)
            print(f"    {label} D{d+1:2d}: QS={avg_qs:5.1f}  pred_WR={avg_pwr:.3f}  "
                  f"actual_WR={actual_wr:.3f}  err={avg_pwr-actual_wr:+.3f}  n={len(chunk_idx)}")

        # RMSE
        rmse_wr = (sum(r["wr_error"]**2 for r in rows) / len(rows)) ** 0.5
        print(f"    {label} WR calibration RMSE: {rmse_wr:.4f}")
        return rows

    calibration_pre = _build_calibration(scores_pre, all_signals, "Pre")
    calibration_post = _build_calibration(scores_post, post_signals_for_scoring, "Post")

    # ── Inc 6: Redundancy analysis ──
    print(f"\n  ── REDUNDANCY ANALYSIS ──")
    pre_names = set(f["name"] for f in features_pre)
    post_names = set(f["name"] for f in features_post)
    both = pre_names & post_names
    pre_only = pre_names - post_names
    post_only = post_names - pre_names
    print(f"  Pre-only:  {len(pre_only)} (survived pre but not post = refinement captured)")
    print(f"  Post-only: {len(post_only)} (survived post but not pre = rare)")
    print(f"  Both:      {len(both)} (genuine additive value)")

    redundancy = {
        "features_pre_only": sorted(pre_only),
        "features_post_only": sorted(post_only),
        "features_both": sorted(both),
        "n_pre_only": len(pre_only),
        "n_post_only": len(post_only),
        "n_both": len(both),
    }

    # Build output signals array
    signals_out = []
    for i, s in enumerate(all_signals):
        signals_out.append({
            "ticker": s["ticker"], "date": s["date"], "close": s.get("close"),
            "classification": s.get("classification"),
            "is_example": s.get("is_example", False),
            "move_adr": s.get("move_adr"), "adr_at_signal": s.get("adr_at_signal"),
            "entry_high": s.get("entry_high"), "cluster_id": s.get("cluster_id"),
            "killed_at_depth": kad.get(s.get("cluster_id")),
            "quality_score": scores_pre[i]["quality_score"],
            "setup_score": scores_pre[i]["setup_score"],
            "market_score": scores_pre[i]["market_score"],
            "predicted_wr": scores_pre[i]["predicted_wr"],
            "predicted_mfe": scores_pre[i]["predicted_mfe"],
            "ev": scores_pre[i]["ev"],
        })

    post_signals_out = []
    for pi, orig_idx in enumerate(post_indices):
        s = all_signals[orig_idx]
        post_signals_out.append({
            "ticker": s["ticker"], "date": s["date"],
            "classification": s.get("classification"),
            "is_example": s.get("is_example", False),
            "move_adr": s.get("move_adr"),
            "quality_score": scores_post[pi]["quality_score"],
            "setup_score": scores_post[pi]["setup_score"],
            "market_score": scores_post[pi]["market_score"],
            "predicted_wr": scores_post[pi]["predicted_wr"],
            "predicted_mfe": scores_post[pi]["predicted_mfe"],
            "ev": scores_post[pi]["ev"],
        })

    # Save output
    tt = time.time() - t_total
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    out = {
        "setup": setup_type, "increment": 6,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(tt, 1),
        "clusters_file": cf, "refinement_file": rf,
        "verification": {
            "depth_replay_passed": dok, "features_passed": fok,
            "feature_comparison": fcomp,
            "dedup_corr_check_pre": {"passed": corr_ok_pre, "violations": v_pre},
            "dedup_corr_check_post": {"passed": corr_ok_post, "violations": v_post},
            "examples_scored": example_scored_count, "examples_total": ne,
        },
        "feature_coverage": fcov,
        "depth_stats": ds,
        "refinement_depth_map": {"conditions_in_order": peel},
        "screening": {
            "setup": setup_stats,
            "market": {k: v for k, v in mkt_stats.items()},
            "pre_refinement": {
                "n_signals": len(all_signals),
                "n_setup_survivors": len(setup_surv_pre),
                "n_market_survivors": len(mkt_surv_pre),
                "n_total_survivors": total_pre,
            },
            "post_refinement": {
                "n_signals": len(ps),
                "n_setup_survivors": len(setup_surv_post),
                "n_market_survivors": len(mkt_surv_post),
                "n_total_survivors": total_post,
            },
        },
        "dedup": {
            "pre_refinement": dedup_stats_pre,
            "post_refinement": dedup_stats_post,
        },
        "validation": {
            "calibration_pre": calibration_pre,
            "calibration_post": calibration_post,
            "calibration_rmse_wr_pre": round((sum(r["wr_error"]**2 for r in calibration_pre) / max(len(calibration_pre), 1)) ** 0.5, 4) if calibration_pre else None,
            "calibration_rmse_wr_post": round((sum(r["wr_error"]**2 for r in calibration_post) / max(len(calibration_post), 1)) ** 0.5, 4) if calibration_post else None,
        },
        "redundancy": redundancy,
        "features_pre": features_pre,
        "features_post": features_post,
        "signals": signals_out,
        "signals_post": post_signals_out,
        "scan_config": {
            "signal_conditions_count": 87,
            "refinement_conditions_count": len(rc),
            "default_refinement_depth": len(rc),
            "quality_score_range": {
                "min": round(min(s["quality_score"] for s in signals_out), 1),
                "max": round(max(s["quality_score"] for s in signals_out), 1),
            },
            "setup_score_range": {
                "min": round(min(s["setup_score"] for s in signals_out), 1),
                "max": round(max(s["setup_score"] for s in signals_out), 1),
            },
            "market_score_range": {
                "min": round(min(s["market_score"] for s in signals_out), 1),
                "max": round(max(s["market_score"] for s in signals_out), 1),
            },
            "assumed_stop_adr": 1.0,
        },
        "summary": {
            "pre_refinement_signals": len(all_signals),
            "post_refinement_signals": len(ps),
            "refinement_conditions": len(rc),
            "clusters_killed": len(kad), "examples": ne,
            "total_features_tested": mkt_stats.get("n_features_tested", 0) + setup_stats.get("n_features", 0),
            "screening_survivors_pre": total_pre,
            "screening_survivors_post": total_post,
            "deduped_survivors_pre": len(deduped_pre),
            "deduped_survivors_post": len(deduped_post),
            "scoring_features_pre": len(features_pre),
            "scoring_features_post": len(features_post),
        },
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    op = os.path.join(CACHE_DIR, f"ev_{setup_type}_inc6_{ts}.json")
    with open(op, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Saved: {op}")
    try:
        from file_mirror import mirror_file
        mirror_file(op)
        print(f"  Mirrored to Railway")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  {'=' * 50}")
    print(f"  INCREMENT 6 COMPLETE ({tt:.1f}s)")
    print(f"  Features: {len(features_pre)} pre / {len(features_post)} post")
    print(f"  Signals scored: {len(signals_out)} pre / {len(post_signals_out)} post")
    qs_all = [s["quality_score"] for s in signals_out]
    print(f"  Quality score range: {min(qs_all):.1f} — {max(qs_all):.1f}")
    evs_all = [s["ev"] for s in signals_out]
    print(f"  EV range: {min(evs_all):.3f} — {max(evs_all):.3f}")
    print(f"  {'=' * 50}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Grinder — Phase 3")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    args = parser.parse_args()
    result = run(args.setup)
    if result is None:
        sys.exit(1)
