"""Build Tightening Range snapshots for UNIVERSE tickers (daily/weekly/monthly).

A Tightening Range is a converging wedge: a descending resistance (lower highs)
meeting a rising support (higher lows), with price inside and the band
contracting — and the apex (where the lines would meet) NOT pointing down.

Output: local_runner/cache/tightening_range_snapshots.json

Each ticker is scanned on all three timeframes (daily as-is; weekly/monthly
resampled from the daily cache) so the dashboard's D/W/M toggle is instant.
The detector fits the two lines by regression through the last few swing pivots
— deliberately a "surface it" tool, not a pixel-perfect drawer: the dashboard
lists the ticker and the user vets/draws the lines + entry on their own chart.
Tuned for recall; the list sorts tightest-band-first so near-apex names sit on
top and the user sifts the rest.
"""
import os
import sys
import json
import time
import pickle
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)
CACHE_DIR = os.environ.get("SCANPERFECT_CACHE_DIR", os.path.join(THIS_DIR, "cache"))

sys.path.insert(0, THIS_DIR)
sys.path.insert(0, PROJECT_ROOT)

from theme_map import UNIVERSE  # noqa: E402

# Detector constants (regression-through-recent-pivots fit; recall-tuned).
PIVOT_W = 3        # swing-pivot half-window
ANCHOR_K = 4       # fit each line through the last K swing pivots
TIMEFRAMES = ("D", "W", "M")
MIN_BARS = {"D": 40, "W": 30, "M": 24}   # min resampled bars per timeframe
RESAMPLE_RULE = {"W": "W-FRI", "M": "ME"}


def _load_ohlcv_cache(cache_dir):
    main_path = os.path.join(cache_dir, "universe_ohlcv_daily.pkl")
    intraday_path = os.path.join(cache_dir, "universe_ohlcv_daily_intraday.pkl")
    chosen = None
    if os.path.exists(intraday_path) and os.path.exists(main_path):
        chosen = intraday_path if os.path.getmtime(intraday_path) >= os.path.getmtime(main_path) else main_path
    elif os.path.exists(intraday_path):
        chosen = intraday_path
    elif os.path.exists(main_path):
        chosen = main_path
    if chosen is None:
        raise FileNotFoundError(f"No OHLCV cache in {cache_dir}")
    with open(chosen, "rb") as f:
        cache = pickle.load(f)
    is_intraday = chosen.endswith("universe_ohlcv_daily_intraday.pkl")
    return cache, chosen, is_intraday


def _resample(df, tf):
    """Daily as-is; weekly/monthly OHLC aggregation off the daily bars."""
    if tf == "D":
        return df.reset_index(drop=True)
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    rule = RESAMPLE_RULE[tf]
    r = pd.DataFrame({
        "open": d["open"].resample(rule).first(),
        "high": d["high"].resample(rule).max(),
        "low":  d["low"].resample(rule).min(),
        "close": d["close"].resample(rule).last(),
    }).dropna().reset_index()
    return r


def _pivots(arr, w, kind):
    out = []
    for i in range(w, len(arr) - w):
        seg = arr[i - w:i + w + 1]
        if kind == "high" and arr[i] == max(seg):
            out.append(i)
        elif kind == "low" and arr[i] == min(seg):
            out.append(i)
    return out


WINDOWS = {"D": (40, 60, 80, 100, 130),
           "W": (24, 36, 50, 65),
           "M": (18, 28, 40)}


def _check_triangle(h, l, c_last):
    """Triangle test on a slice: compare percentile bounds of the first vs
    the last third. Highs lower (90th pct end < 90th pct start), lows higher
    (10th pct end > 10th pct start), recent envelope narrower than start.
    Single wicks don't break it. Returns the score (contraction) or None.
    """
    n = len(c_last) if hasattr(c_last, '__len__') else None  # not used here
    L = len(h)
    if L < 24:
        return None
    third = max(8, L // 3)
    # 90th / 10th percentiles smooth one-off spikes vs absolute max/min.
    s_hi = float(np.percentile(h[:third], 90))
    s_lo = float(np.percentile(l[:third], 10))
    e_hi = float(np.percentile(h[-third:], 90))
    e_lo = float(np.percentile(l[-third:], 10))
    if not (e_hi < s_hi):         # highs descended
        return None
    if not (e_lo > s_lo):         # lows ascended
        return None
    s_range = s_hi - s_lo
    e_range = e_hi - e_lo
    if s_range <= 0:
        return None
    contract = e_range / s_range
    if contract > 0.70:           # need >=30% contraction
        return None
    # Use the EXTREMES (max high, min low) of the end third as the visible band,
    # and percentile bounds of the start third as the wedge mouth.
    end_hi_abs = float(np.max(h[-third:]))
    end_lo_abs = float(np.min(l[-third:]))
    return {
        "s_hi": s_hi, "s_lo": s_lo, "e_hi": e_hi, "e_lo": e_lo,
        "end_hi_abs": end_hi_abs, "end_lo_abs": end_lo_abs,
        "contract": contract, "third": third, "L": L,
    }


def _fit_wedge(r, tf="D"):
    """Detect a triangle SHAPE across multiple lookback windows; pick the
    tightest one that fires. No trendline fitting — just envelope contraction
    measured by percentile bounds of the first vs last third.
    """
    full = r.reset_index(drop=True)
    c_full = full["close"].values.astype(np.float64)
    if len(c_full) < 24:
        return None
    dates_full = [str(x)[:10] for x in full["date"].tolist()]
    close_last = float(c_full[-1])
    best = None
    for W in WINDOWS.get(tf, (60, 100)):
        if len(c_full) < W:
            continue
        rr = full.tail(W).reset_index(drop=True)
        h = rr["high"].values.astype(np.float64)
        l = rr["low"].values.astype(np.float64)
        info = _check_triangle(h, l, close_last)
        if info is None:
            continue
        # Price must be inside the recent envelope (not breaking out).
        if not (info["end_lo_abs"] <= close_last <= info["end_hi_abs"]):
            continue
        # Slopes for apex direction + bars_to_apex (approximate, from segment centers).
        third = info["third"]; L = info["L"]
        s_center = third // 2
        e_center = L - 1 - third // 2
        span_bars = max(1, e_center - s_center)
        slope_r = (info["e_hi"] - info["s_hi"]) / span_bars
        slope_s = (info["e_lo"] - info["s_lo"]) / span_bars
        if slope_s <= slope_r:
            continue
        mid_norm = ((slope_r + slope_s) / 2.0) / close_last
        if mid_norm < 0:
            continue
        # Score: tightest end range wins.
        band_now = info["end_hi_abs"] - info["end_lo_abs"]
        cand = {
            "W": W, "info": info, "slope_r": slope_r, "slope_s": slope_s,
            "mid_norm": mid_norm, "band_now": band_now, "span_bars": span_bars,
        }
        if best is None or band_now < best["band_now"]:
            best = cand
    if best is None:
        return None
    info = best["info"]
    slope_r = best["slope_r"]; slope_s = best["slope_s"]
    band_now = best["band_now"]
    h_all = full["high"].values.astype(np.float64)
    l_all = full["low"].values.astype(np.float64)
    adr_win = min(20, len(h_all))
    adr = float(np.mean((h_all[-adr_win:] / l_all[-adr_win:] - 1.0) * 100.0))
    adr_px = adr / 100.0 * close_last if adr > 0 else close_last * 0.02
    bars_to_apex = band_now / (slope_s - slope_r) if slope_s > slope_r else 9999.0
    n = len(full) - 1
    return {
        "res": float(info["end_hi_abs"]),
        "sup": float(info["end_lo_abs"]),
        "band_pct": float(band_now / close_last * 100.0),
        "band_adr": float(band_now / adr_px) if adr_px > 0 else 999.0,
        "bars_to_apex": float(bars_to_apex),
        "wedge_span": int(best["W"] - 1),
        "mid_norm": float(best["mid_norm"]),
        "asof_date": dates_full[n],
        "contraction": float(info["contract"]),
        "window_bars": int(best["W"]),
        "start_hi": float(info["s_hi"]), "start_lo": float(info["s_lo"]),
    }


def _ticker_snapshot(args):
    """Worker: scan one ticker across all timeframes. Returns (tk, {tf: payload}, err)."""
    ticker, df, drop_last = args
    try:
        if df is None or len(df) < 2:
            return ticker, None, None
        if drop_last and len(df) >= 2:
            df = df.iloc[:-1]
        out = {}
        for tf in TIMEFRAMES:
            r = _resample(df, tf)
            if len(r) < MIN_BARS[tf]:
                continue
            m = _fit_wedge(r, tf)
            if m is not None:
                out[tf] = m
        if not out:
            return ticker, None, None
        return ticker, out, None
    except Exception as exc:
        return ticker, None, repr(exc)


def build(workers=None, verbose=True, cache_dir=None, out_dir=None):
    cache_dir = cache_dir or CACHE_DIR
    out_dir = out_dir or cache_dir
    cache, src_path, is_intraday = _load_ohlcv_cache(cache_dir)
    if verbose:
        print(f"OHLCV cache: {src_path}")
        print(f"  {len(cache)} tickers")
        if is_intraday:
            print("  intraday cache detected — dropping today's partial bar before scanning")

    work = []
    for tk in UNIVERSE:
        if tk in cache:
            work.append((tk, cache[tk], is_intraday))
    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    if verbose:
        print(f"  UNIVERSE present={len(work)}")
        print(f"\nScanning tightening ranges (D/W/M) with {workers} workers...")

    out = {}
    errors = {}
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(_ticker_snapshot, item) for item in work]
        for fut in as_completed(futures):
            ticker, payload, err = fut.result()
            done += 1
            if err is not None:
                errors[ticker] = err
            elif payload is not None:
                out[ticker] = payload
            if verbose and done % 100 == 0:
                print(f"  {done}/{len(work)}  matches={len(out)}  elapsed={time.time()-t0:.1f}s")
    elapsed = time.time() - t0
    n_by_tf = {tf: sum(1 for p in out.values() if tf in p) for tf in TIMEFRAMES}
    if verbose:
        print(f"\nDone in {elapsed:.1f}s — {len(out)} tickers match (by tf: {n_by_tf}), {len(errors)} errors")
        if errors:
            for tk, e in list(errors.items())[:5]:
                print(f"    {tk}: {e}")

    out_path = os.path.join(out_dir, "tightening_range_snapshots.json")
    tmp_path = out_path + ".tmp"
    payload_doc = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "built_in_seconds": round(elapsed, 1),
        "source_cache": os.path.basename(src_path),
        "intraday_partial_dropped": bool(is_intraday),
        "pivot_w": PIVOT_W,
        "anchor_k": ANCHOR_K,
        "n_universe": len(UNIVERSE),
        "n_matches": len(out),
        "n_matches_by_tf": n_by_tf,
        "n_errors": len(errors),
        "tickers": out,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload_doc, f, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    if verbose:
        print(f"\nWrote {out_path} ({os.path.getsize(out_path)/1024:.1f} KB)")
    return payload_doc


if __name__ == "__main__":
    build()
