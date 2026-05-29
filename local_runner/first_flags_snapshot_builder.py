"""Build First Flags snapshots for UNIVERSE tickers.

Run once on demand + every morning (Step 0 of the theme dashboard build).

Output: local_runner/cache/first_flags_snapshots.json

A "First Flag" is a bottom-reversal continuation candidate:
  1. A bullish MACD 6/20-line divergence (price lower-low, MACD higher-low) —
     detected by theme_dashboard.detect_divergences, the SAME detector the
     composite chart uses. We take the MOST RECENT bullish divergence, and its
     bottom (the anchor low) must have closed BELOW the 200-day SMA.
  2. After the bottom the 10/20/50 SMAs stacked in order (10 > 20 > 50) — the
     trend / "pole." No fixed % move; the MA stack IS the trend.
  3. The 10/20/50 stack is STILL intact at the scan bar.
  4. Price is currently BELOW the highest high made since the stack formed —
     pulled back into a flag, not a fresh breakout.
  5. That pullback is RIDING the fast MA — close within a few % of the 10 or
     20 SMA (the flag hugging the rising MAs).
  6. The divergence is REAL — the anchor low is at least 2% below the prior
     pivot low (drops the -0.1% micro "divergences").
  7. It's the FIRST / early flag — at most 2 swing highs since the stack
     formed (drops multi-leg runners that already ran far past the divergence).

Conditions 6-7 were derived from a labeled set of A+ first flags vs clear
fails, not eyeballed. Tuned for recall: the scan only needs to surface a name
on SOME day during its flag; the user takes it to a TC2000 watchlist.

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
from vectorized_indicators import sma_2d  # noqa: E402

# Match rule constants. RIDE_PCT is a recall dial (looser = catches the flag on
# more days); the rest of the rule is threshold-free MA structure.
RIDE_PCT = 5.0            # flag: close within this % of the 10 or 20 SMA (riding the fast MA)
DIV_MIN_LL_PCT = 2.0      # divergence anchor low must be >= this % below the prior pivot low (a real lower-low)
MAX_SWING_HIGHS = 2       # at most this many swing highs since the stack formed (first/early flag)
MIN_BARS = 210            # need SMA200 at the bottom bar + a little room


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

        # Single source of truth for divergence detection + pivots.
        from theme_dashboard import detect_divergences, _find_pivots

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

        # Real divergence: the anchor low must be a meaningful lower-low vs the
        # prior pivot low — drops -0.1% micro "divergences" the detector counts.
        prior_low = float(d["p1_price"])
        div_ll_pct = (bottom_low / prior_low - 1.0) * 100.0 if prior_low > 0 else 0.0
        if div_ll_pct > -DIV_MIN_LL_PCT:
            return ticker, None, None

        # 200-SMA at the bottom bar — the divergence must have happened below it.
        sma200 = sma_2d(close.reshape(1, -1), 200)[0]
        s200 = sma200[bottom_idx]
        if np.isnan(s200) or s200 <= 0:
            return ticker, None, None
        if not (bottom_close < s200):
            return ticker, None, None
        below_200_pct = (bottom_close / s200 - 1.0) * 100.0

        # ── The trend / "pole": 10/20/50 SMAs stacked in order ──────────────
        # SMAs to match TC2000's Avg 10 / 20 / 50. No fixed % move — the stack
        # IS the trend. The stack must FORM after the bottom and still be intact.
        sma10 = sma_2d(close.reshape(1, -1), 10)[0]
        sma20 = sma_2d(close.reshape(1, -1), 20)[0]
        sma50 = sma_2d(close.reshape(1, -1), 50)[0]
        stack_start = None
        for i in range(bottom_idx + 1, asof_bar + 1):
            if (not (np.isnan(sma10[i]) or np.isnan(sma20[i]) or np.isnan(sma50[i]))
                    and sma10[i] > sma20[i] > sma50[i]):
                stack_start = i
                break
        if stack_start is None:
            return ticker, None, None
        if not (sma10[asof_bar] > sma20[asof_bar] > sma50[asof_bar]):
            return ticker, None, None

        # First / early flag: not many legs since the stack formed. Counting
        # swing highs since the stack keeps it to the first pullback or two and
        # drops multi-leg runners that already ran far past the divergence.
        swing_highs = [p for p in _find_pivots(high, 3, "high") if p > stack_start]
        if len(swing_highs) > MAX_SWING_HIGHS:
            return ticker, None, None

        # ── Pulled back into a flag ─────────────────────────────────────────
        # Highest high since the stack formed; price must currently be BELOW it
        # (a pullback), and that high must be in the past (not a fresh breakout).
        seg_high = high[stack_start:asof_bar + 1]
        peak_off = int(np.nanargmax(seg_high))
        peak_idx = stack_start + peak_off
        peak_high = float(seg_high[peak_off])
        last_close = float(close[asof_bar])
        if peak_idx >= asof_bar or last_close >= peak_high:
            return ticker, None, None

        # ── Flag = riding the fast MA: close within RIDE_PCT of the 10 or 20 ──
        ride_pct = min(abs(last_close / sma10[asof_bar] - 1.0),
                       abs(last_close / sma20[asof_bar] - 1.0)) * 100.0
        if ride_pct > RIDE_PCT:
            return ticker, None, None

        pole_pct = (peak_high / bottom_low - 1.0) * 100.0
        pullback_pct = ((peak_high - last_close) / peak_high * 100.0
                        if peak_high > 0 else 0.0)

        payload = {
            "asof_bar":         asof_bar,
            "asof_date":        str(dates[asof_bar])[:10],
            "bottom_idx":       bottom_idx,
            "bottom_date":      str(dates[bottom_idx])[:10],
            "bottom_low":       bottom_low,
            "bottom_close":     bottom_close,
            "below_200_pct":    below_200_pct,
            "stack_start_idx":  stack_start,
            "stack_start_date": str(dates[stack_start])[:10],
            "pole_high_idx":    peak_idx,
            "pole_high_date":   str(dates[peak_idx])[:10],
            "pole_high_price":  peak_high,
            "pole_pct":         pole_pct,
            "bars_since_bottom": asof_bar - bottom_idx,
            "bars_since_stack": asof_bar - stack_start,
            "pullback_pct":     pullback_pct,
            "ride_pct":         ride_pct,
            "div_ll_pct":       div_ll_pct,
            "swing_highs_since_stack": len(swing_highs),
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
        "ride_pct": RIDE_PCT,
        "div_min_ll_pct": DIV_MIN_LL_PCT,
        "max_swing_highs": MAX_SWING_HIGHS,
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
