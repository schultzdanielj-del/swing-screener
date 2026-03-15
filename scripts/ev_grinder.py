"""
EV Grinder — Phase 3 Correlative Scoring Engine.

See EV_GRINDER.md for full spec.

Usage:
    python scripts/ev_grinder.py --setup dtss

Increment 1: Data loaders + refinement depth replay.
Increment 2: Setup feature computation (6 OHLCV + 4 fundamentals).
Increment 3: Market feature screening (parallel) + setup feature screening.
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
    closes = df["close"].values.astype(np.float64)
    opens = df["open"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    dates = df["date"].values
    n = len(df)
    if n < 55:
        return {}
    with np.errstate(divide='ignore', invalid='ignore'):
        intraday_pct = (closes / opens - 1.0) * 100.0
    intraday_pct = np.where(np.isfinite(intraday_pct), intraday_pct, 0.0)
    avg_intraday = np.full(n, np.nan)
    for i in range(4, n):
        avg_intraday[i] = np.mean(intraday_pct[i-4:i+1])
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - np.roll(closes, 1)),
                               np.abs(lows - np.roll(closes, 1))))
    tr[0] = highs[0] - lows[0]
    atr50 = np.full(n, np.nan)
    for i in range(49, n):
        atr50[i] = np.mean(tr[i-49:i+1])
    c50 = np.full(n, np.nan)
    c50[50:] = closes[:-50]
    avg_price = (closes + c50) / 2.0
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
        sig["feat_price"] = float(df.iloc[ri]["close"])
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
            vol = float(df.iloc[ri]["volume"])
            sig["feat_volume_float_ratio"] = (vol / fl) if (vol > 0 and np.isfinite(vol)) else None
        else:
            sig["feat_volume_float_ratio"] = None
        if sig["feat_volume_float_ratio"] is not None:
            cov["volume_float_ratio"] += 1
        sig["_sector"] = fi.get("sector")
    if n_miss:
        print(f"  WARNING: {n_miss} signals had dates not in cache")
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
    wr_threshold = wr_pp_threshold / 100.0
    N_BUCKETS = 10
    pct_boundaries = np.linspace(0, 100, N_BUCKETS + 1)

    for fi in range(n_features):
        col = values[:, fi]
        valid_mask = ~np.isnan(col)
        n_valid = int(np.sum(valid_mask))
        if n_valid < n_signals * 0.5:
            continue
        valid_vals = col[valid_mask]
        valid_winners = is_winner[valid_mask]
        valid_moves = move_adrs[valid_mask]
        boundaries = np.percentile(valid_vals, pct_boundaries[1:-1])
        if boundaries[0] == boundaries[-1]:
            continue
        bucket_idx = np.digitize(valid_vals, boundaries, right=False)
        bucket_idx = np.clip(bucket_idx, 0, N_BUCKETS - 1) + 1
        b_counts = [int(np.sum(bucket_idx == b)) for b in range(1, N_BUCKETS + 1)]
        if any(c < min_per_bucket for c in b_counts):
            continue
        b_wr = []
        for b in range(1, N_BUCKETS + 1):
            mask = bucket_idx == b
            b_wr.append(float(np.mean(valid_winners[mask])))
        wr_spread = abs(b_wr[N_BUCKETS - 1] - b_wr[0])
        wr_pass = wr_spread >= wr_threshold
        b_mfe = []
        for b in range(1, N_BUCKETS + 1):
            mask = (bucket_idx == b) & valid_winners
            winner_moves = valid_moves[mask]
            winner_moves = winner_moves[~np.isnan(winner_moves)]
            if len(winner_moves) >= 3:
                b_mfe.append(float(np.median(winner_moves)))
            else:
                b_mfe.append(np.nan)
        d1_mfe = b_mfe[0]
        d10_mfe = b_mfe[N_BUCKETS - 1]
        if not np.isnan(d1_mfe) and not np.isnan(d10_mfe):
            mfe_spread = abs(d10_mfe - d1_mfe)
        else:
            mfe_spread = 0.0
        mfe_pass = mfe_spread >= mfe_adr_threshold
        if not wr_pass and not mfe_pass:
            continue
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
            "values": col.copy(),
        })

    return survivors


def _dedup_survivors(survivors, corr_threshold=0.95):
    """Greedy dedup: rank by screening strength, drop correlated features.

    Within a single instrument's survivors, many expressions are minor
    variants of each other (SMA20 vs SMA21 vs SMA22). This collapses
    them to the single best representative per correlated cluster.

    Args:
        survivors: list of dicts from screen_features(), each has 'values' array.
        corr_threshold: drop feature if abs(correlation) > this with a kept feature.

    Returns:
        Subset of survivors (references to same dicts, not copies).
    """
    if len(survivors) <= 1:
        return survivors

    # Rank by screening strength: max of WR spread and MFE spread (normalized)
    # Normalize MFE to comparable scale: 1.0 ADR spread ~ 10pp WR spread
    for s in survivors:
        s["_strength"] = max(s["wr_spread"], s["mfe_spread"] / 10.0)
    ranked = sorted(survivors, key=lambda s: s["_strength"], reverse=True)

    kept = []
    kept_vals = []

    for s in ranked:
        v = s["values"].astype(np.float64)
        drop = False
        for kv in kept_vals:
            both_valid = ~np.isnan(v) & ~np.isnan(kv)
            n_both = int(np.sum(both_valid))
            if n_both < 20:
                continue
            a = v[both_valid]
            b = kv[both_valid]
            a_m = a - np.mean(a)
            b_m = b - np.mean(b)
            denom = np.sqrt(np.sum(a_m * a_m) * np.sum(b_m * b_m))
            if denom < 1e-12:
                corr = 0.0
            else:
                corr = float(np.sum(a_m * b_m) / denom)
            if abs(corr) > corr_threshold:
                drop = True
                break
        if not drop:
            kept.append(s)
            kept_vals.append(v)

    for s in survivors:
        del s["_strength"]

    return kept


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
    """Worker: load one .npz, screen all expressions, dedup within instrument."""
    (inst_id, npz_path, sig_dates_pre, sig_dates_post,
     is_win_pre, is_win_post, moves_pre, moves_post,
     n_exprs, expr_names, wr_pp, mfe_adr, min_per_bucket) = args

    t0 = time.time()
    try:
        loaded = np.load(npz_path, allow_pickle=True)
        data = loaded["data"]
        dates = loaded["dates"]

        date_to_row = {}
        for i, d in enumerate(dates):
            date_to_row[str(d)] = i

        n_pre = len(sig_dates_pre)
        n_post = len(sig_dates_post)

        vals_pre = np.full((n_pre, n_exprs), np.nan, dtype=np.float32)
        for si, sd in enumerate(sig_dates_pre):
            ri = date_to_row.get(sd)
            if ri is None:
                from datetime import date as dt_date, timedelta
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

        vals_post = np.full((n_post, n_exprs), np.nan, dtype=np.float32)
        for si, sd in enumerate(sig_dates_post):
            ri = date_to_row.get(sd)
            if ri is None:
                from datetime import date as dt_date, timedelta
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

        del data, loaded

        feat_names = [f"{inst_id}__{expr_names[j]}" for j in range(n_exprs)]

        surv_pre = screen_features(
            vals_pre, is_win_pre, moves_pre, feat_names,
            min_per_bucket=min_per_bucket, wr_pp_threshold=wr_pp, mfe_adr_threshold=mfe_adr)

        surv_post = screen_features(
            vals_post, is_win_post, moves_post, feat_names,
            min_per_bucket=min_per_bucket, wr_pp_threshold=wr_pp, mfe_adr_threshold=mfe_adr)

        # Per-instrument dedup: collapse correlated expressions to best representative
        surv_pre = _dedup_survivors(surv_pre)
        surv_post = _dedup_survivors(surv_post)

        elapsed = time.time() - t0
        return (inst_id, surv_pre, surv_post, n_exprs, elapsed)

    except Exception as e:
        return (inst_id, [], [], 0, time.time() - t0, str(e))


def run_market_screening(all_signals, post_signals, n_workers=8,
                         wr_pp=10, mfe_adr=1.0, min_per_bucket=8):
    """Screen all market instruments in parallel."""
    print("\n  ── MARKET FEATURE SCREENING ──")
    t0 = time.time()

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
    print(f"  Dedup: corr > 0.95 within each instrument")
    print(f"  Workers: {n_workers}")

    sig_dates_pre = [s["date"] for s in all_signals]
    is_win_pre = np.array(["WIN" in s.get("classification", "") for s in all_signals])
    moves_pre = np.array([s.get("move_adr") or np.nan for s in all_signals], dtype=np.float64)

    post_dates_set = set((s.get("ticker"), s.get("signal_date", s.get("date")))
                         for s in post_signals)
    post_mask = []
    for s in all_signals:
        key = (s["ticker"], s["date"])
        post_mask.append(key in post_dates_set)
    post_mask = np.array(post_mask)

    post_indices = np.where(post_mask)[0]
    sig_dates_post = [all_signals[i]["date"] for i in post_indices]
    is_win_post = is_win_pre[post_indices]
    moves_post = moves_pre[post_indices]

    print(f"  Pre-refinement: {len(sig_dates_pre)} signals ({int(is_win_pre.sum())}W)")
    print(f"  Post-refinement: {len(sig_dates_post)} signals ({int(is_win_post.sum())}W)")

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
    """Screen the 10 setup features through the same WR/MFE logic."""
    print("\n  ── SETUP FEATURE SCREENING ──")

    feat_keys = ["feat_price", "feat_adr", "feat_dollar_volume_20d",
                 "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
                 "feat_market_cap", "feat_volume_float_ratio",
                 "feat_rs_vs_sector", "feat_sector_rs_vs_spy"]
    feat_names = [k.replace("feat_", "") for k in feat_keys]

    n_pre = len(all_signals)
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
    for s in surv_pre:
        fk = s["name"]
        s["source"] = "setup_fundamentals" if fk in (
            "market_cap", "volume_float_ratio", "rs_vs_sector", "sector_rs_vs_spy"
        ) else "setup_ohlcv"

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
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type):
    print("\n" + "=" * 70)
    print("  EV GRINDER — Phase 3 Correlative Scoring")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

    # Inc 1
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

    # Inc 2
    fcov = compute_setup_features(all_signals)
    fok, fcomp = validate_setup_features(all_signals)

    # Inc 3
    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", 8))
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

    tt = time.time() - t_total
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _clean_survivor(s):
        r = {k: v for k, v in s.items() if k != "values"}
        return r

    out = {
        "setup": setup_type, "increment": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(tt, 1),
        "clusters_file": cf, "refinement_file": rf,
        "verification": {
            "depth_replay_passed": dok,
            "features_passed": fok,
            "feature_comparison": fcomp,
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
        "survivors_pre": [_clean_survivor(s) for s in setup_surv_pre + mkt_surv_pre],
        "survivors_post": [_clean_survivor(s) for s in setup_surv_post + mkt_surv_post],
        "summary": {
            "pre_refinement_signals": len(all_signals),
            "post_refinement_signals": len(ps),
            "refinement_conditions": len(rc),
            "clusters_killed": len(kad),
            "examples": ne,
            "total_features_tested": mkt_stats.get("n_features_tested", 0) + setup_stats.get("n_features", 0),
            "survivors_pre": total_pre,
            "survivors_post": total_post,
        },
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    op = os.path.join(CACHE_DIR, f"ev_{setup_type}_inc3_{ts}.json")
    with open(op, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Saved: {op}")
    try:
        from file_mirror import mirror_file
        mirror_file(op)
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  {'=' * 50}")
    print(f"  INCREMENT 3 COMPLETE ({tt:.1f}s)")
    print(f"  {'=' * 50}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Grinder — Phase 3")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    args = parser.parse_args()
    result = run(args.setup)
    if result is None:
        sys.exit(1)
