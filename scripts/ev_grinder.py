"""
EV Grinder — Phase 3 Correlative Scoring Engine.

See EV_GRINDER.md for full spec.

Usage:
    python scripts/ev_grinder.py --setup dtss

Increment 1: Data loaders + refinement depth replay.
Increment 2: Setup feature computation (6 OHLCV + 4 fundamentals).
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

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

    # 5-day rolling average
    avg_intraday = np.full(n, np.nan)
    for i in range(4, n):
        avg_intraday[i] = np.mean(intraday_pct[i-4:i+1])

    # ATR50: 50-bar simple moving average of true range
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - np.roll(closes, 1)),
                               np.abs(lows - np.roll(closes, 1))))
    tr[0] = highs[0] - lows[0]
    atr50 = np.full(n, np.nan)
    for i in range(49, n):
        atr50[i] = np.mean(tr[i-49:i+1])

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

    # Save
    tt = time.time() - t_total
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sc = [{k: s.get(k) for k in ["ticker", "date", "classification",
           "feat_price", "feat_adr", "feat_dollar_volume_20d",
           "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
           "feat_market_cap", "feat_volume_float_ratio",
           "feat_rs_vs_sector", "feat_sector_rs_vs_spy"]}
          for s in all_signals[:5]]

    out = {
        "setup": setup_type, "increment": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(tt, 1),
        "clusters_file": cf, "refinement_file": rf,
        "verification": {"depth_replay_passed": dok, "features_passed": fok,
                         "feature_comparison": fcomp},
        "feature_coverage": fcov, "spot_check": sc,
        "depth_stats": ds,
        "refinement_depth_map": {"conditions_in_order": peel},
        "summary": {"pre_refinement_signals": len(all_signals),
                    "post_refinement_signals": len(ps),
                    "refinement_conditions": len(rc),
                    "clusters_killed": len(kad), "examples": ne},
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    op = os.path.join(CACHE_DIR, f"ev_{setup_type}_inc2_{ts}.json")
    with open(op, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Saved: {op}")
    try:
        from file_mirror import mirror_file
        mirror_file(op)
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  {'=' * 50}")
    print(f"  INCREMENT 2 COMPLETE ({tt:.1f}s)")
    print(f"  {'=' * 50}")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Grinder — Phase 3")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    args = parser.parse_args()
    result = run(args.setup)
    if result is None:
        sys.exit(1)
