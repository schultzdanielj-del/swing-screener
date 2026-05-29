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

# A tightening range is only tradeable once it has coiled tight enough that the
# stop fits inside the range. The live band (trendlines projected to today) must
# be no wider than this fraction of price. Names whose range is still a quarter
# to half the stock price are "too soon" / too wide to trade and are dropped.
# Sits in the natural gap above the widest acceptable keeper (CRML ~20%).
MAX_BAND_PCT = 25.0


def _ray_to_today(h, l):
    """Fit the two trendlines as rays anchored on the window's price extremes
    and read them at the most recent bar ("today").

    Resistance is the ray from the highest high that just grazes the highest of
    the later highs — the shallowest descending line off the peak that still
    sits above every subsequent high (the upper hull's final segment). Support
    is the mirror: the ray from the lowest low grazing the highest-constraining
    of the later lows. Reading those rays at the current bar gives the LIVE
    range a trader would draw, instead of the wedge's wide (old) mouth.

    This is the fix for the "range is way too wide at current day" problem: the
    old code reported the last-third percentile box, which scoops up month-old
    extremes; these rays converge forward to today.

    Returns (res_today, sup_today, slope_r, slope_s) per bar, or None when an
    extreme is the last bar (no forward span to define the ray).
    """
    n = len(h) - 1
    pk = int(np.argmax(h))
    tr = int(np.argmin(l))
    if pk >= n or tr >= n:
        return None
    slope_r = max((h[j] - h[pk]) / (j - pk) for j in range(pk + 1, n + 1))
    slope_s = min((l[j] - l[tr]) / (j - tr) for j in range(tr + 1, n + 1))
    res_today = h[pk] + slope_r * (n - pk)
    sup_today = l[tr] + slope_s * (n - tr)
    return res_today, sup_today, slope_r, slope_s


def _check_triangle(h, l, c_last):
    """Shape test that excludes the most recent bars from envelope definition,
    then projects the wedge's trendlines forward to the current bar and
    rejects if the current close has broken outside the projection.

    Without the margin, a recent breakout *expands* the "last third" envelope
    so the broken-out close still tests as "inside" — that was the bug.
    """
    L = len(h)
    if L < 30:
        return None
    # Reserve the last ~10% of the window (min 5 bars) as the breakout zone —
    # those bars are NOT used to define the wedge envelope.
    margin = max(5, L // 10)
    Ls = L - margin
    third = max(8, Ls // 3)

    # 90th / 10th percentiles smooth one-off spikes vs absolute max/min.
    s_hi = float(np.percentile(h[:third], 90))
    s_lo = float(np.percentile(l[:third], 10))
    e_hi = float(np.percentile(h[Ls - third:Ls], 90))
    e_lo = float(np.percentile(l[Ls - third:Ls], 10))
    if not (e_hi < s_hi):         # highs descended
        return None
    if not (e_lo > s_lo):         # lows ascended
        return None
    s_range = s_hi - s_lo
    e_range = e_hi - e_lo
    if s_range <= 0:
        return None
    contract = e_range / s_range
    if contract > 0.85:           # need >=15% contraction (margin excluded so threshold is looser)
        return None

    # Project the trendlines from segment centers (in the SHAPE window) forward
    # to the most recent bar in the FULL window. That's the test point.
    s_center = third // 2
    e_center = Ls - 1 - third // 2
    span = e_center - s_center
    if span <= 0:
        return None
    slope_r = (e_hi - s_hi) / span
    slope_s = (e_lo - s_lo) / span
    if not (slope_r < 0 and slope_s > 0):
        return None
    n = L - 1
    res_proj = e_hi + slope_r * (n - e_center)
    sup_proj = e_lo + slope_s * (n - e_center)
    if res_proj <= sup_proj:                  # apex already passed
        return None

    # Breakout check. An intact tightening range has price COILING inside the
    # contracting envelope — sitting in the body of the band, not pinned to or
    # piercing its upper edge. Once the close reaches the upper boundary it is
    # breaking out (or already has). Two gates:
    #   1) Position-in-band: where the latest close sits between the envelope
    #      floor (0.0) and ceiling (1.0). Require it in the lower ~80% of the
    #      band — pressing the ceiling (>0.80) means breaking out the top; below
    #      the floor (<0.0) means it broke down.
    #   2) Margin guard: the recent (margin) bars must not have poked decisively
    #      past the pre-margin envelope (more than 20% of the band beyond it).
    e_range = e_hi - e_lo
    pos = (c_last - e_lo) / e_range          # 0 = at floor, 1 = at ceiling
    if pos > 0.80:
        return None                          # close pressing / above the top = breaking out up
    if pos < 0.0:
        return None                          # close below the floor = broken down
    margin_tol = 0.20 * e_range
    margin_h_max = float(np.max(h[Ls:]))
    margin_l_min = float(np.min(l[Ls:]))
    if margin_h_max > e_hi + margin_tol:
        return None                          # recent bar decisively broke out the top
    if margin_l_min < e_lo - margin_tol:
        return None                          # recent bar decisively broke down

    # Live trendlines projected to TODAY (the range a trader actually draws).
    # The percentile slopes above already confirmed the body tapers; these rays
    # give the current band off the real price extremes. Require the two lines
    # to still converge at today (resistance above support) — otherwise the
    # apex has passed and there is no range left.
    rays = _ray_to_today(h, l)
    if rays is None:
        return None
    res_today, sup_today, rslope_r, rslope_s = rays
    if not (rslope_r < 0 and rslope_s > 0):
        return None                          # resistance must fall, support must rise
    if res_today <= sup_today:
        return None                          # lines already crossed = apex passed

    return {
        "s_hi": s_hi, "s_lo": s_lo, "e_hi": e_hi, "e_lo": e_lo,
        "res_proj": float(res_proj), "sup_proj": float(sup_proj),
        "slope_r": float(slope_r), "slope_s": float(slope_s),
        "res_today": float(res_today), "sup_today": float(sup_today),
        "rslope_r": float(rslope_r), "rslope_s": float(rslope_s),
        "contract": float(contract), "third": int(third), "L": int(L), "Ls": int(Ls),
    }


def _fit_wedge(r, tf="D"):
    """Detect a triangle SHAPE across multiple lookback windows; pick the
    tightest one that fires AND is still intact (no breakout in the margin).
    """
    full = r.reset_index(drop=True)
    c_full = full["close"].values.astype(np.float64)
    if len(c_full) < 30:
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
        # Apex direction from the LIVE (ray) trendlines: allow a mild downward
        # tilt (resistance falling a bit faster than support rises is still a
        # valid converging triangle), but reject structures whose midline points
        # clearly down — falling wedges / bear patterns, not tightening ranges.
        mid_norm = ((info["rslope_r"] + info["rslope_s"]) / 2.0) / close_last
        if mid_norm < -0.0015:
            continue
        # Band = the trendlines projected to TODAY (not the old wide mouth).
        band_now = info["res_today"] - info["sup_today"]
        if band_now / close_last * 100.0 > MAX_BAND_PCT:
            continue                         # current range too wide to trade
        cand = {"W": W, "info": info, "mid_norm": mid_norm, "band_now": band_now}
        # Pick the window whose live range is tightest — i.e. the one caught
        # nearest the apex, which is the tradeable one.
        if best is None or band_now < best["band_now"]:
            best = cand
    if best is None:
        return None
    info = best["info"]
    band_now = best["band_now"]
    h_all = full["high"].values.astype(np.float64)
    l_all = full["low"].values.astype(np.float64)
    adr_win = min(20, len(h_all))
    adr = float(np.mean((h_all[-adr_win:] / l_all[-adr_win:] - 1.0) * 100.0))
    adr_px = adr / 100.0 * close_last if adr > 0 else close_last * 0.02
    rslope_r = info["rslope_r"]; rslope_s = info["rslope_s"]
    bars_to_apex = band_now / (rslope_s - rslope_r) if rslope_s > rslope_r else 9999.0
    n = len(full) - 1
    return {
        "res": float(info["res_today"]),
        "sup": float(info["sup_today"]),
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
