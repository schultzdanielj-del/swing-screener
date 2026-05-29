"""Build First Flags snapshots for UNIVERSE tickers.

Run once on demand + every morning (Step 0 of the theme dashboard build).

Output: local_runner/cache/first_flags_snapshots.json

A "First Flag" is a bottom-reversal continuation candidate:
  1. A bullish MACD 6/20-line divergence (price lower-low, MACD higher-low) —
     detected by theme_dashboard.detect_divergences, the SAME detector the
     composite chart uses. We take the MOST RECENT bullish divergence.
  2. Its bottom (the anchor low) closed BELOW the 200-day SMA at that bar.
  3. Price has since risen >= 25% from the bottom low to the highest high
     after it (the flagpole).
  4. The close has held ABOVE the 50-day SMA for the last 10 bars straight
     (the reversal has reclaimed the 50 — health gate).
  5. The 6/20 MACD line is STILL below its 9-EMA signal at the as-of bar
     (bear cross intact — the flag is actively forming, timing gate).

The dashboard's Setups page reads this snapshot and renders the matches; the
first flag (entry pullback) itself is vetted on the chart, not auto-detected.

Drift note: divergence detection is imported from theme_dashboard (single
source of truth) — there is no second copy of the pivot/divergence logic.

Cost: detect_divergences is light (pure numpy/python pivot scan); the full
~916-ticker UNIVERSE runs in well under a minute with multiprocessing.
"""
import os
import sys
import json
import time
import pickle
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)
CACHE_DIR = os.environ.get("SCANPERFECT_CACHE_DIR", os.path.join(THIS_DIR, "cache"))

sys.path.insert(0, THIS_DIR)
sys.path.insert(0, PROJECT_ROOT)

from theme_map import UNIVERSE  # noqa: E402
from vectorized_indicators import sma_2d, macd_2d, ema_2d  # noqa: E402

# Match rule constants (Dan's given numbers — not tuned here).
MOVE_MIN_PCT = 25.0     # >= 25% pole off the divergence bottom
ABOVE50_BARS = 10       # close must hold above the 50-SMA this many bars straight
MIN_BARS = 210          # need SMA200 at the bottom bar + a little room


# ── Cache I/O ────────────────────────────────────────────────

def _load_ohlcv_cache(cache_dir):
    """Load the freshest OHLCV cache available. Prefer the intraday snapshot.

    Returns (cache_dict, source_path, is_intraday). When ``is_intraday`` is
    True the caller drops the last (partial) bar before scanning — the
    intraday bar must not create new pivots; the day's divergence set is
    fixed at the prior EOD close.
    """
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


# ── Per-ticker compute ───────────────────────────────────────

def _ticker_snapshot(args):
    """Worker: produce one ticker's First Flags match (or None).

    ``drop_last`` strips today's partial intraday bar so the divergence
    pivots reflect the most recent COMPLETED EOD bar.

    Returns (ticker, payload_or_None, error_or_None). payload is None when the
    ticker simply doesn't match (not an error).
    """
    ticker, df, drop_last = args
    try:
        if df is None or len(df) < MIN_BARS:
            return ticker, None, None
        if drop_last and len(df) >= 2:
            df = df.iloc[:-1]
        if len(df) < MIN_BARS:
            return ticker, None, None

        # Single source of truth for divergence detection.
        from theme_dashboard import detect_divergences

        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)
        dates = df["date"].tolist()
        n = len(close)
        asof_bar = n - 1

        divs = detect_divergences(df)
        bull = divs.get("bull") or []
        if not bull:
            return ticker, None, None

        # Most recent bullish divergence — its anchor low is the bottom.
        d = bull[-1]
        bottom_idx = int(d["p2_idx"])
        bottom_low = float(low[bottom_idx])
        bottom_close = float(close[bottom_idx])
        if bottom_low <= 0 or bottom_idx >= asof_bar:
            return ticker, None, None

        # 200-SMA at the bottom bar — the divergence must have happened below it.
        sma200 = sma_2d(close.reshape(1, -1), 200)[0]
        s200 = sma200[bottom_idx]
        if np.isnan(s200) or s200 <= 0:
            return ticker, None, None
        if not (bottom_close < s200):
            return ticker, None, None
        below_200_pct = (bottom_close / s200 - 1.0) * 100.0

        # Pole: highest high after the bottom through the as-of bar.
        post_high = high[bottom_idx + 1:]
        if post_high.size == 0:
            return ticker, None, None
        pole_off = int(np.nanargmax(post_high))
        pole_high_idx = bottom_idx + 1 + pole_off
        pole_high_price = float(post_high[pole_off])
        pole_pct = (pole_high_price / bottom_low - 1.0) * 100.0
        if pole_pct < MOVE_MIN_PCT:
            return ticker, None, None

        # Health gate: close has held above the 50-SMA for the last 10 bars.
        sma50 = sma_2d(close.reshape(1, -1), 50)[0]
        c10 = close[-ABOVE50_BARS:]
        s10 = sma50[-ABOVE50_BARS:]
        if np.any(np.isnan(s10)) or not np.all(c10 > s10):
            return ticker, None, None

        # Timing gate: the 6/20 MACD line is still below its 9-EMA signal
        # (bear cross intact) — the flag is actively forming, not yet released.
        macd_line = macd_2d(close.reshape(1, -1), 6, 20)[0]
        macd_signal = ema_2d(macd_line.reshape(1, -1), 9)[0]
        ml = macd_line[asof_bar]
        sg = macd_signal[asof_bar]
        if np.isnan(ml) or np.isnan(sg) or not (ml < sg):
            return ticker, None, None

        last_close = float(close[asof_bar])
        pullback_pct = ((pole_high_price - last_close) / pole_high_price * 100.0
                        if pole_high_price > 0 else 0.0)

        payload = {
            "asof_bar":         asof_bar,
            "asof_date":        str(dates[asof_bar])[:10],
            "bottom_idx":       bottom_idx,
            "bottom_date":      str(dates[bottom_idx])[:10],
            "bottom_low":       bottom_low,
            "bottom_close":     bottom_close,
            "sma200_at_bottom": float(s200),
            "below_200_pct":    below_200_pct,
            "prior_low_idx":    int(d["p1_idx"]),
            "prior_low_date":   str(dates[int(d["p1_idx"])])[:10],
            "prior_low_price":  float(d["p1_price"]),
            "macd_prior":       float(d["m1_macd"]),
            "macd_bottom":      float(d["m2_macd"]),
            "pole_high_idx":    pole_high_idx,
            "pole_high_date":   str(dates[pole_high_idx])[:10],
            "pole_high_price":  pole_high_price,
            "pole_pct":         pole_pct,
            "bars_since_bottom": asof_bar - bottom_idx,
            "pullback_pct":     pullback_pct,
            "macd_line":        float(ml),
            "macd_signal":      float(sg),
        }
        return ticker, payload, None
    except Exception as exc:
        return ticker, None, repr(exc)


# ── Main driver ──────────────────────────────────────────────

def build(workers=None, verbose=True, cache_dir=None, out_dir=None):
    """Scan UNIVERSE for First Flags matches and write the snapshot JSON.

    ``cache_dir`` overrides where the OHLCV pickle is READ from (defaults to
    the module CACHE_DIR). ``out_dir`` overrides where the snapshot is WRITTEN
    (defaults to cache_dir) — kept separate so a test run can read the real
    cache while writing its output somewhere isolated.
    """
    cache_dir = cache_dir or CACHE_DIR
    out_dir = out_dir or cache_dir
    cache, src_path, is_intraday = _load_ohlcv_cache(cache_dir)
    n_cache = len(cache)
    if verbose:
        print(f"OHLCV cache: {src_path}")
        print(f"  {n_cache} tickers")
        if is_intraday:
            print("  intraday cache detected — dropping today's partial bar before scanning")

    work = []
    missing = []
    for tk in UNIVERSE:
        if tk in cache:
            work.append((tk, cache[tk], is_intraday))
        else:
            missing.append(tk)
    if verbose:
        print(f"  UNIVERSE={len(UNIVERSE)}  present={len(work)}  missing={len(missing)}")

    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    if verbose:
        print(f"\nScanning for First Flags with {workers} workers...")

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
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(f"  {done}/{len(work)}  matches={len(out)}  elapsed={elapsed:.1f}s  rate={rate:.0f}/s")
    elapsed = time.time() - t0
    if verbose:
        print(f"\nDone in {elapsed:.1f}s — {len(out)} matches, {len(errors)} errors")
        if errors:
            for tk, e in list(errors.items())[:5]:
                print(f"    {tk}: {e}")

    out_path = os.path.join(out_dir, "first_flags_snapshots.json")
    tmp_path = out_path + ".tmp"
    payload_doc = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "built_in_seconds": round(elapsed, 1),
        "source_cache": os.path.basename(src_path),
        "intraday_partial_dropped": bool(is_intraday),
        "move_min_pct": MOVE_MIN_PCT,
        "above50_bars": ABOVE50_BARS,
        "requires_macd_bearcross": True,
        "n_universe": len(UNIVERSE),
        "n_matches": len(out),
        "n_errors": len(errors),
        "tickers": out,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload_doc, f, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    if verbose:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"\nWrote {out_path} ({size_kb:.1f} KB)")
    return payload_doc


if __name__ == "__main__":
    build()
