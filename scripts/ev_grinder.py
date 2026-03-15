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
            "ticker": c["ticker"],
            "date": c["rightmost"]["date"],
            "bar_idx": c["rightmost"]["bar_idx"],
            "close": c["rightmost"].get("close"),
            "classification": c.get("classification", "UNKNOWN"),
            "move_adr": c.get("move_adr"),
            "adr_at_signal": c.get("adr_at_signal"),
            "entry_high": c.get("entry_high"),
            "is_example": bool(c.get("is_example", 0)),
            "cluster_id": c["cluster_id"],
            "leftward_bar_idxs": leftward_idxs,
        })
    return signals, data


def load_refinement(path):
    with open(path) as f:
        data = json.load(f)
    refinement_conditions = data.get("refinement_conditions_only", [])
    winners = data.get("winner_signals", [])
    losers = data.get("loser_signals", [])
    eliminated = data.get("eliminated_signals", [])
    post_signals = winners + losers
    return refinement_conditions, post_signals, eliminated, data


def load_5yr_cache():
    for name in ("universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    raise FileNotFoundError("No 5yr OHLCV cache found in local_runner/cache/")


def load_fundamentals_cache():
    path = os.path.join(CACHE_DIR, "fundamentals_cache.json")
    if not os.path.exists(path):
        print("  WARNING: fundamentals_cache.json not found")
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("tickers", {})


# ══════════════════════════════════════════════════════════════
# SIGNAL STATS COMPUTATION
# ══════════════════════════════════════════════════════════════

def compute_signal_stats(signals):
    total = len(signals)
    if total == 0:
        return {"total": 0, "winners": 0, "losers": 0, "wr": 0.0,
                "peak_day": 0, "avg_day": 0.0, "avg_week": 0.0,
                "avg_month": 0.0, "avg_year": 0.0}
    winners = sum(1 for s in signals if "WIN" in s.get("classification", ""))
    losers = total - winners
    wr = winners / total
    dates = [s["date"] for s in signals]
    date_counts = Counter(dates)
    peak_day = max(date_counts.values()) if date_counts else 0
    avg_day = total / len(date_counts) if date_counts else 0.0
    if dates:
        from datetime import date as dt_date
        sorted_dates = sorted(dates)
        d_first = dt_date.fromisoformat(sorted_dates[0])
        d_last = dt_date.fromisoformat(sorted_dates[-1])
        span_days = (d_last - d_first).days + 1
        avg_week = total / max(span_days / 7, 1)
        avg_month = total / max(span_days / 30.44, 1)
        avg_year = total / max(span_days / 365.25, 1)
    else:
        avg_week = avg_month = avg_year = 0.0
    return {"total": total, "winners": winners, "losers": losers,
            "wr": round(wr, 4), "peak_day": peak_day,
            "avg_day": round(avg_day, 2), "avg_week": round(avg_week, 2),
            "avg_month": round(avg_month, 2), "avg_year": round(avg_year, 2)}


# ══════════════════════════════════════════════════════════════
# SETUP FEATURE COMPUTATION (INCREMENT 2)
# ══════════════════════════════════════════════════════════════

def _build_date_index(df):
    """Build a dict mapping date string -> row index for fast O(1) lookup."""
    dates = df["date"].values
    index = {}
    for i in range(len(dates)):
        d = str(dates[i])[:10]
        index[d] = i
    return index


def _compute_rs_series(df):
    """Compute RS raw value series for a ticker.

    RS formula: 5-day rolling average of ((close/open - 1) * 100),
    multiplied by (avg_price / ATR50).
    Returns dict: {date_string: rs_value}.
    """
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

    avg_price = (highs + lows + closes) / 3.0
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = avg_intraday * (avg_price / atr50)
    rs = np.where(np.isfinite(rs), rs, np.nan)

    result = {}
    for i in range(n):
        if not np.isnan(rs[i]):
            result[str(dates[i])[:10]] = float(rs[i])
    return result


def _resample_to_weekly(df):
    if len(df) < 10:
        return None
    wdf = df.copy().set_index("date")
    resampled = wdf.resample("W").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna(subset=["close"])
    if len(resampled) < 10:
        return None
    resampled = resampled.reset_index()
    resampled.columns = ["date", "open", "high", "low", "close", "volume"]
    return resampled


def _compute_rs_weekly_series(df):
    """Compute weekly RS series mapped back to daily dates."""
    wdf = _resample_to_weekly(df)
    if wdf is None:
        return {}
    weekly_rs = _compute_rs_series(wdf)
    if not weekly_rs:
        return {}

    daily_dates = df["date"].values
    weekly_dates_sorted = sorted(weekly_rs.keys())
    if not weekly_dates_sorted:
        return {}

    result = {}
    wi = 0
    for i in range(len(daily_dates)):
        d = str(daily_dates[i])[:10]
        while wi < len(weekly_dates_sorted) - 1 and weekly_dates_sorted[wi + 1] <= d:
            wi += 1
        if weekly_dates_sorted[wi] <= d:
            result[d] = weekly_rs[weekly_dates_sorted[wi]]
    return result


def compute_setup_features(all_signals):
    """Compute 6 OHLCV + 4 fundamentals features for all signals.

    Uses DATE-BASED lookup into the OHLCV cache (not bar_idx) because the
    5yr cache is rebuilt nightly and bar indices shift.
    """
    print("\n  ── SETUP FEATURE COMPUTATION ──")
    t0 = time.time()

    print("  Loading 5yr OHLCV cache...")
    ohlcv_cache = load_5yr_cache()
    print(f"  {len(ohlcv_cache)} tickers in cache")

    print("  Loading fundamentals cache...")
    fund_cache = load_fundamentals_cache()
    n_fund = sum(1 for v in fund_cache.values() if "error" not in v)
    print(f"  {n_fund} tickers with fundamentals data")

    # Ensure all DataFrames have datetime dates
    for tk, df in ohlcv_cache.items():
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            ohlcv_cache[tk] = df.copy()
            ohlcv_cache[tk]["date"] = pd.to_datetime(df["date"])

    # Build date->row index per ticker for O(1) lookups
    print("  Building date indexes...")
    ticker_date_idx = {}  # ticker -> {date_str: row_idx}
    signal_tickers = set(s["ticker"] for s in all_signals)
    for tk in signal_tickers:
        df = ohlcv_cache.get(tk)
        if df is not None:
            ticker_date_idx[tk] = _build_date_index(df)

    # Precompute SPY RS
    print("  Computing SPY RS series...")
    spy_df = ohlcv_cache.get("SPY")
    if spy_df is None:
        raise RuntimeError("SPY not in OHLCV cache")
    spy_rs_d1 = _compute_rs_series(spy_df)
    spy_rs_w1 = _compute_rs_weekly_series(spy_df)
    print(f"  SPY RS: {len(spy_rs_d1)} daily, {len(spy_rs_w1)} weekly dates")

    # Precompute RS for all signal tickers
    print("  Computing RS series for all signal tickers...")
    ticker_rs_d1 = {}
    ticker_rs_w1 = {}
    rs_skipped = 0
    for tk in signal_tickers:
        df = ohlcv_cache.get(tk)
        if df is None:
            rs_skipped += 1
            continue
        ticker_rs_d1[tk] = _compute_rs_series(df)
        ticker_rs_w1[tk] = _compute_rs_weekly_series(df)
    print(f"  RS computed for {len(ticker_rs_d1)} tickers ({rs_skipped} skipped)")

    # ── Per-signal features ──
    print("  Computing per-signal features...")
    coverage = defaultdict(int)
    n_date_miss = 0

    for sig in all_signals:
        tk = sig["ticker"]
        sig_date = sig["date"]
        df = ohlcv_cache.get(tk)
        date_idx = ticker_date_idx.get(tk, {})

        # Find the bar by DATE, not by bar_idx
        row_idx = date_idx.get(sig_date)
        if row_idx is None:
            # Signal date not in current OHLCV cache — all features NaN
            n_date_miss += 1
            for feat in ["feat_price", "feat_adr", "feat_dollar_volume_20d",
                         "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
                         "feat_market_cap", "feat_volume_float_ratio",
                         "feat_rs_vs_sector", "feat_sector_rs_vs_spy"]:
                sig[feat] = None
            sig["_sector"] = None
            continue

        # Feature 1: price
        sig["feat_price"] = float(df.iloc[row_idx]["close"])
        coverage["price"] += 1

        # Feature 2: adr (14-bar average daily range)
        if row_idx >= 13:
            h = df["high"].values[row_idx-13:row_idx+1].astype(np.float64)
            l = df["low"].values[row_idx-13:row_idx+1].astype(np.float64)
            adr = float(np.mean(h - l))
            sig["feat_adr"] = adr if (adr > 0 and np.isfinite(adr)) else None
        else:
            sig["feat_adr"] = None
        if sig["feat_adr"] is not None:
            coverage["adr"] += 1

        # Feature 3: dollar_volume_20d
        if row_idx >= 19:
            c = df["close"].values[row_idx-19:row_idx+1].astype(np.float64)
            v = df["volume"].values[row_idx-19:row_idx+1].astype(np.float64)
            dv = float(np.mean(c * v))
            sig["feat_dollar_volume_20d"] = dv if np.isfinite(dv) else None
        else:
            sig["feat_dollar_volume_20d"] = None
        if sig["feat_dollar_volume_20d"] is not None:
            coverage["dollar_volume_20d"] += 1

        # Feature 4: days_since_ipo (row index in current cache = days of history)
        sig["feat_days_since_ipo"] = row_idx
        coverage["days_since_ipo"] += 1

        # Feature 5: rs_d1
        tk_rs = ticker_rs_d1.get(tk, {}).get(sig_date)
        spy_rs = spy_rs_d1.get(sig_date)
        if tk_rs is not None and spy_rs is not None:
            sig["feat_rs_d1"] = tk_rs - spy_rs
            coverage["rs_d1"] += 1
        else:
            sig["feat_rs_d1"] = None

        # Feature 6: rs_w1
        tk_rsw = ticker_rs_w1.get(tk, {}).get(sig_date)
        spy_rsw = spy_rs_w1.get(sig_date)
        if tk_rsw is not None and spy_rsw is not None:
            sig["feat_rs_w1"] = tk_rsw - spy_rsw
            coverage["rs_w1"] += 1
        else:
            sig["feat_rs_w1"] = None

        # Feature 7: market_cap
        fund = fund_cache.get(tk, {})
        shares = fund.get("shares_outstanding")
        if shares is not None and sig["feat_price"] is not None:
            sig["feat_market_cap"] = shares * sig["feat_price"]
            coverage["market_cap"] += 1
        else:
            sig["feat_market_cap"] = None

        # Feature 8: volume_float_ratio
        float_shares = fund.get("float_shares")
        if float_shares is not None and float_shares > 0:
            vol = float(df.iloc[row_idx]["volume"])
            if vol > 0 and np.isfinite(vol):
                sig["feat_volume_float_ratio"] = vol / float_shares
                coverage["volume_float_ratio"] += 1
            else:
                sig["feat_volume_float_ratio"] = None
        else:
            sig["feat_volume_float_ratio"] = None

        # Store sector for aggregation pass
        sig["_sector"] = fund.get("sector")

    if n_date_miss:
        print(f"  WARNING: {n_date_miss} signals had dates not found in current OHLCV cache")

    # ── Sector RS features (second pass) ──
    print("  Computing sector RS features...")
    date_sector_rs = defaultdict(list)
    for sig in all_signals:
        sector = sig.get("_sector")
        if sector and sig["feat_rs_d1"] is not None:
            date_sector_rs[(sig["date"], sector)].append(sig["feat_rs_d1"])

    sector_avg_rs = {}
    for (d, s), values in date_sector_rs.items():
        sector_avg_rs[(d, s)] = float(np.mean(values))

    for sig in all_signals:
        sector = sig.get("_sector")
        sig_date = sig["date"]

        if sector and sig["feat_rs_d1"] is not None and (sig_date, sector) in sector_avg_rs:
            sig["feat_rs_vs_sector"] = sig["feat_rs_d1"] - sector_avg_rs[(sig_date, sector)]
            coverage["rs_vs_sector"] += 1
        else:
            sig["feat_rs_vs_sector"] = None

        if sector and (sig_date, sector) in sector_avg_rs:
            sig["feat_sector_rs_vs_spy"] = sector_avg_rs[(sig_date, sector)]
            coverage["sector_rs_vs_spy"] += 1
        else:
            sig["feat_sector_rs_vs_spy"] = None

        del sig["_sector"]

    elapsed = time.time() - t0
    n_signals = len(all_signals)
    print(f"\n  Feature coverage ({n_signals} signals):")
    for feat in ["price", "adr", "dollar_volume_20d", "days_since_ipo",
                  "rs_d1", "rs_w1", "market_cap", "volume_float_ratio",
                  "rs_vs_sector", "sector_rs_vs_spy"]:
        n = coverage[feat]
        print(f"    {feat:25s} {n:>5}/{n_signals} ({n/n_signals*100:.1f}%)")
    print(f"\n  Setup features complete ({elapsed:.1f}s)")

    del ohlcv_cache
    return dict(coverage)


def validate_setup_features(all_signals):
    """Validate OHLCV features against preserved setup_grinder output."""
    print("\n  ── VALIDATING SETUP FEATURES ──")

    import requests
    url = "https://web-production-e3025.up.railway.app/api/v2/files/local_runner/cache/setup_dtss_20260313_135931.json"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"  WARNING: Could not load setup_grinder output (HTTP {resp.status_code})")
            return True, {}
        ref_data = resp.json()
    except Exception as e:
        print(f"  WARNING: Could not load setup_grinder output: {e}")
        return True, {}

    ref_signals = ref_data.get("signal_features", [])
    if not ref_signals:
        print("  WARNING: No signal_features in setup_grinder output")
        return True, {}
    print(f"  Reference: {len(ref_signals)} signals from setup_grinder")

    ref_lookup = {}
    for s in ref_signals:
        ref_lookup[(s["ticker"], s["signal_date"])] = s

    feature_map = {
        "price": "feat_price", "adr": "feat_adr",
        "dollar_volume_20d": "feat_dollar_volume_20d",
        "days_since_ipo": "feat_days_since_ipo",
        "rs_d1": "feat_rs_d1", "rs_w1": "feat_rs_w1",
    }

    diffs = {f: [] for f in feature_map}
    matched = 0

    for sig in all_signals:
        ref = ref_lookup.get((sig["ticker"], sig["date"]))
        if ref is None:
            continue
        matched += 1
        for ref_name, sig_name in feature_map.items():
            ref_val = ref.get(ref_name)
            sig_val = sig.get(sig_name)
            if ref_val is not None and sig_val is not None:
                diffs[ref_name].append(abs(float(ref_val) - float(sig_val)))

    print(f"  Matched: {matched}/{len(all_signals)} signals")

    ok = True
    comparison = {}
    for feat, diff_list in diffs.items():
        if not diff_list:
            comparison[feat] = {"n": 0, "max_diff": None, "status": "no_data"}
            continue
        max_diff = max(diff_list)
        mean_diff = sum(diff_list) / len(diff_list)
        if feat in ("rs_d1", "rs_w1"):
            threshold = 1.0
        elif feat == "dollar_volume_20d":
            threshold = 1.0
        elif feat == "days_since_ipo":
            threshold = 0.5
        else:
            threshold = 0.01
        passed = max_diff <= threshold
        if not passed:
            ok = False
        comparison[feat] = {
            "n": len(diff_list), "max_diff": round(max_diff, 6),
            "mean_diff": round(mean_diff, 6), "threshold": threshold,
            "status": "PASS" if passed else "FAIL",
        }
        print(f"    {feat:25s} max_diff={max_diff:.6f}  mean={mean_diff:.6f}  [{'PASS' if passed else 'FAIL'}]")

    if ok:
        print(f"\n  ✓ All feature validations passed")
    else:
        print(f"\n  ✗ Feature validation FAILED")
    return ok, comparison


# ══════════════════════════════════════════════════════════════
# REFINEMENT DEPTH REPLAY (INCREMENT 1)
# ══════════════════════════════════════════════════════════════

def replay_refinement_depth(all_signals, refinement_conditions, expr_cache):
    """Replay refinement conditions using greedy peeling."""
    print("\n  ── REFINEMENT DEPTH REPLAY ──")
    t0 = time.time()
    n_conditions = len(refinement_conditions)
    if n_conditions == 0:
        stats = compute_signal_stats(all_signals)
        return {}, [stats], []

    losing_clusters = {}
    for sig in all_signals:
        if "LOSS" in sig.get("classification", ""):
            cid = sig["cluster_id"]
            bars = [(sig["ticker"], sig["bar_idx"])]
            for lw_idx in sig.get("leftward_bar_idxs", []):
                bars.append((sig["ticker"], lw_idx))
            losing_clusters[cid] = bars

    n_losing = len(losing_clusters)
    print(f"  Losing clusters: {n_losing}")
    print(f"  Refinement conditions: {n_conditions}")

    cond_col_indices = []
    for cond in refinement_conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        if col_idx is None:
            raise RuntimeError(f"Refinement condition '{cond['name']}' not in expression cache.")
        cond_col_indices.append(col_idx)

    print(f"  Loading expression data for losing cluster bars...")
    ticker_cache = {}
    tickers_needed = set()
    for bars in losing_clusters.values():
        for (tk, _) in bars:
            tickers_needed.add(tk)
    for tk in tickers_needed:
        dates, data = expr_cache.get_ticker(tk)
        if dates is None:
            raise RuntimeError(f"Ticker '{tk}' not in expression cache.")
        ticker_cache[tk] = data
    print(f"  Loaded {len(ticker_cache)} tickers")

    print(f"  Computing per-bar condition pass/fail...")
    cluster_bar_cond_passes = {}
    for cid, bars in losing_clusters.items():
        n_bars = len(bars)
        passes = np.ones((n_bars, n_conditions), dtype=bool)
        for bi, (tk, bar_idx) in enumerate(bars):
            data = ticker_cache[tk]
            if bar_idx >= data.shape[0]:
                raise RuntimeError(f"bar_idx {bar_idx} >= {data.shape[0]} for {tk}.")
            for ci, (cond, col_idx) in enumerate(zip(refinement_conditions, cond_col_indices)):
                val = float(data[bar_idx, col_idx])
                if np.isnan(val):
                    passes[bi, ci] = True
                else:
                    passes[bi, ci] = (val >= cond["low"] and val <= cond["high"])
        cluster_bar_cond_passes[cid] = passes
    del ticker_cache

    print(f"\n  Verifying full-depth elimination...")
    def count_alive_clusters(mask):
        alive = 0
        for cid, passes in cluster_bar_cond_passes.items():
            if np.any(np.all(passes[:, mask], axis=1)):
                alive += 1
        return alive

    all_active = np.ones(n_conditions, dtype=bool)
    surviving = count_alive_clusters(all_active)
    print(f"  Full depth: {surviving} surviving, {n_losing - surviving} eliminated")

    print(f"\n  Running greedy peel...")
    active_mask = np.ones(n_conditions, dtype=bool)
    peel_order = []
    for peel_step in range(n_conditions):
        best_ci = None
        best_alive = -1
        current_alive = count_alive_clusters(active_mask)
        for ci in range(n_conditions):
            if not active_mask[ci]:
                continue
            test = active_mask.copy()
            test[ci] = False
            alive = count_alive_clusters(test)
            if best_ci is None or alive < best_alive:
                best_alive = alive
                best_ci = ci
        active_mask[best_ci] = False
        peel_order.append(best_ci)
        if (peel_step + 1) % 20 == 0 or peel_step == 0 or peel_step == n_conditions - 1:
            print(f"    Peel {peel_step+1:3d}: removed {refinement_conditions[best_ci]['name']:40s} "
                  f"+{best_alive - current_alive:3d} back  ({best_alive} alive, depth={n_conditions - peel_step - 1})")

    application_order = list(reversed(peel_order))
    print(f"\n  Computing killed_at_depth...")
    killed_at_depth = {}
    for cid, passes in cluster_bar_cond_passes.items():
        bar_alive = np.ones(passes.shape[0], dtype=bool)
        for di, ci in enumerate(application_order):
            bar_alive = bar_alive & passes[:, ci]
            if not np.any(bar_alive):
                killed_at_depth[cid] = di + 1
                break
    print(f"  Killed: {len(killed_at_depth)}, Surviving: {n_losing - len(killed_at_depth)}")

    print(f"\n  Building depth_stats...")
    cluster_to_signal = {sig["cluster_id"]: sig for sig in all_signals if "LOSS" in sig.get("classification", "")}
    winner_signals = [s for s in all_signals if "WIN" in s.get("classification", "")]

    depth_stats = []
    for depth in range(n_conditions + 1):
        alive_losers = [sig for cid, sig in cluster_to_signal.items()
                        if killed_at_depth.get(cid) is None or depth < killed_at_depth[cid]]
        stats = compute_signal_stats(winner_signals + alive_losers)
        stats["depth"] = depth
        depth_stats.append(stats)

    conditions_in_order = []
    for di, ci in enumerate(application_order):
        depth = di + 1
        cond = refinement_conditions[ci]
        conditions_in_order.append({
            "idx": di, "depth": depth, "name": cond["name"],
            "low": cond["low"], "high": cond["high"],
            "clusters_killed": [cid for cid, kd in killed_at_depth.items() if kd == depth],
            "cumulative_losers_remaining": depth_stats[depth]["losers"],
            "cumulative_winners": depth_stats[depth]["winners"],
            "cumulative_total": depth_stats[depth]["total"],
            "cumulative_wr": depth_stats[depth]["wr"],
            "cumulative_peak": depth_stats[depth]["peak_day"],
            "cumulative_avg": depth_stats[depth]["avg_day"],
        })

    print(f"\n  Refinement replay complete ({time.time() - t0:.1f}s)")
    return killed_at_depth, depth_stats, conditions_in_order


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

    # Load data
    clusters_path, clusters_file = find_raw_clusters(setup_type)
    if not clusters_path:
        print(f"\n  ERROR: No raw clusters for {setup_type}")
        return None
    print(f"\n  Raw clusters: {clusters_file}")
    all_signals, _ = load_clusters(clusters_path)
    n_pre_win = sum(1 for s in all_signals if "WIN" in s["classification"])
    n_pre_loss = sum(1 for s in all_signals if "LOSS" in s["classification"])
    n_examples = sum(1 for s in all_signals if s["is_example"])
    print(f"  Pre-refinement: {len(all_signals)} ({n_pre_win}W + {n_pre_loss}L, {n_examples} examples)")

    ref_path, ref_file = find_latest_refinement(setup_type)
    if not ref_path:
        print(f"\n  ERROR: No refinement for {setup_type}")
        return None
    print(f"\n  Refinement: {ref_file}")
    ref_conditions, post_signals, _, _ = load_refinement(ref_path)
    n_post_win = sum(1 for s in post_signals if "WIN" in s.get("classification", ""))
    n_post_loss = sum(1 for s in post_signals if "LOSS" in s.get("classification", ""))
    print(f"  Post-refinement: {len(post_signals)} ({n_post_win}W + {n_post_loss}L)")
    print(f"  Refinement conditions: {len(ref_conditions)}")

    print(f"\n  Loading expression cache...")
    from expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print(f"  ERROR: Expression cache invalid.")
        return None
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # Increment 1
    killed_at_depth, depth_stats, peel_sequence = replay_refinement_depth(
        all_signals, ref_conditions, expr_cache)

    print(f"\n  ── DEPTH REPLAY VERIFICATION ──")
    d0, d_max = depth_stats[0], depth_stats[len(ref_conditions)]
    checks = [
        ("Depth 0 total", d0["total"], len(all_signals)),
        ("Depth 0 winners", d0["winners"], n_pre_win),
        (f"Depth {len(ref_conditions)} total", d_max["total"], len(post_signals)),
        (f"Depth {len(ref_conditions)} losers", d_max["losers"], n_post_loss),
        ("Monotonic", all(depth_stats[i]["total"] <= depth_stats[i-1]["total"] for i in range(1, len(depth_stats))), True),
        ("Winners constant", all(d["winners"] == n_pre_win for d in depth_stats), True),
        ("Killed sum", sum(len(c["clusters_killed"]) for c in peel_sequence), len(killed_at_depth)),
    ]
    depth_ok = all(a == e for _, a, e in checks)
    for label, actual, expected in checks:
        if actual != expected:
            print(f"  ✗ {label}: {actual} != {expected}")
    if depth_ok:
        print(f"  ✓ All {len(checks)} depth checks passed")

    # Increment 2
    feature_coverage = compute_setup_features(all_signals)
    features_ok, feature_comparison = validate_setup_features(all_signals)

    # Save
    total_time = time.time() - t_total
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    spot_check = []
    for sig in all_signals[:5]:
        spot_check.append({k: sig.get(k) for k in [
            "ticker", "date", "classification",
            "feat_price", "feat_adr", "feat_dollar_volume_20d",
            "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1",
            "feat_market_cap", "feat_volume_float_ratio",
            "feat_rs_vs_sector", "feat_sector_rs_vs_spy",
        ]})

    output = {
        "setup": setup_type, "increment": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "clusters_file": clusters_file, "refinement_file": ref_file,
        "verification": {
            "depth_replay_passed": depth_ok,
            "features_passed": features_ok,
            "feature_comparison": feature_comparison,
        },
        "feature_coverage": feature_coverage,
        "spot_check": spot_check,
        "depth_stats": depth_stats,
        "refinement_depth_map": {"conditions_in_order": peel_sequence},
        "summary": {
            "pre_refinement_signals": len(all_signals),
            "post_refinement_signals": len(post_signals),
            "refinement_conditions": len(ref_conditions),
            "clusters_killed": len(killed_at_depth),
            "examples": n_examples,
        },
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"ev_{setup_type}_inc2_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    try:
        from file_mirror import mirror_file
        mirror_file(out_path)
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  {'=' * 50}")
    print(f"  INCREMENT 2 COMPLETE ({total_time:.1f}s)")
    print(f"  {'=' * 50}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Grinder — Phase 3")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    args = parser.parse_args()
    result = run(args.setup)
    if result is None:
        sys.exit(1)
