"""Build ext50-trendline snapshots for UNIVERSE tickers.

Run once on demand + every morning at 7:30 AM (after EOD bar lands).

Output: local_runner/cache/ext50_trendline_snapshots.json

Per-ticker payload captures the u1/u2/u3 + l1/l2/l3 line equations
at the last EOD bar. The theme dashboard reads this snapshot, projects
each line forward by today's intraday-bar offset, and compares against
the live ext50 to detect "Extension Peek" matches without re-fitting
the trendlines mid-day.

Uses the locked logic in scripts/ext50_trendlines.py and
scripts/reversal_profile.py — no spec deviation.

Cost: ~1-3 sec per ticker (cascade_at + reversal_profile pass).
With 14 workers, full 916-ticker UNIVERSE runs in ~3-10 min.
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
CACHE_DIR = os.path.join(THIS_DIR, "cache")

sys.path.insert(0, THIS_DIR)
sys.path.insert(0, PROJECT_ROOT)

from theme_map import UNIVERSE  # noqa: E402
from vectorized_indicators import sma_2d  # noqa: E402


# ── Cache I/O ────────────────────────────────────────────────

def _load_ohlcv_cache():
    """Load the freshest OHLCV cache available. Prefer intraday snapshot.

    Returns (cache_dict, source_path, is_intraday). When ``is_intraday`` is
    True, the caller must drop the last bar before fitting — the intraday
    bar is partial and is NOT allowed to create new pivots (per locked spec:
    the trendline set is fixed for the trading day; only the projection vs
    live price moves).
    """
    main_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    intraday_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily_intraday.pkl")
    chosen = None
    if os.path.exists(intraday_path) and os.path.exists(main_path):
        chosen = intraday_path if os.path.getmtime(intraday_path) >= os.path.getmtime(main_path) else main_path
    elif os.path.exists(intraday_path):
        chosen = intraday_path
    elif os.path.exists(main_path):
        chosen = main_path
    if chosen is None:
        raise FileNotFoundError(f"No OHLCV cache in {CACHE_DIR}")
    with open(chosen, "rb") as f:
        cache = pickle.load(f)
    is_intraday = chosen.endswith("universe_ohlcv_daily_intraday.pkl")
    return cache, chosen, is_intraday


# ── Per-ticker compute ───────────────────────────────────────

def _compute_ext50_series(df, adr_period=20):
    """ext50 = (close - SMA50) / ADR20 in percent units.

        adr20 = mean((H/L - 1) * 100) over 20 bars
        ext   = ((close - SMA50) / SMA50 * 100) / adr20

    Result reads as "how many average daily ranges the close is from
    the SMA50." Uses 20-bar ADR to match the rest of the dashboard's
    ADR convention. None if < 50 bars.
    """
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    n = len(c)
    if n < 50:
        return None
    sma50 = sma_2d(c.reshape(1, -1), 50)[0]
    adr = np.full(n, np.nan, dtype=np.float64)
    for i in range(adr_period - 1, n):
        win_h = h[i - (adr_period - 1):i + 1]
        win_l = l[i - (adr_period - 1):i + 1]
        mask = (~np.isnan(win_h)) & (~np.isnan(win_l)) & (win_l > 0)
        if mask.sum() < 1:
            continue
        adr[i] = float(np.mean((win_h[mask] / win_l[mask] - 1.0) * 100.0))
    pct_dev = (c - sma50) / np.where(sma50 > 0, sma50, np.nan) * 100.0
    ext = pct_dev / np.where(adr > 0, adr, np.nan)
    return ext


def _ticker_snapshot(args):
    """Worker: produce one ticker's trendline snapshot at the last EOD bar.

    ``drop_last`` strips today's partial intraday bar before fitting, so the
    snapshot always reflects the most recent COMPLETED EOD bar. The
    intraday bar must not be allowed to create new pivots.

    Returns (ticker, payload_dict_or_None, error_or_None).
    """
    ticker, df, drop_last = args
    try:
        if df is None or len(df) < 60:
            return ticker, None, "insufficient bars"
        if drop_last and len(df) >= 2:
            df = df.iloc[:-1]

        # Lazy imports inside worker keeps cold-start light + avoids
        # multiprocessing issues with module-level state.
        from scripts.reversal_profile import compute_all_reversal_profile_series
        from scripts.ext50_trendlines import cascade_at, _has_line_break

        ext = _compute_ext50_series(df)
        if ext is None or np.all(np.isnan(ext)):
            return ticker, None, "no ext50 series"

        # Reversal-profile levels for the full series (needed as input to
        # cascade_at). We only consume the last bar's values but the
        # primitive computes the full curve in one pass.
        rp = compute_all_reversal_profile_series(ext)
        levels_keys = ("upside_1", "upside_2", "downside_1", "downside_2", "chop_upper")
        levels_at_asof = {}
        for k in levels_keys:
            arr = rp.get(k)
            if arr is None or len(arr) != len(ext):
                levels_at_asof[k] = float("nan")
            else:
                levels_at_asof[k] = float(arr[-1])

        asof_bar = len(ext) - 1
        asof_date = str(df["date"].iloc[-1])[:10]
        asof_ext50 = float(ext[asof_bar]) if not np.isnan(ext[asof_bar]) else None

        snap = cascade_at(ext, asof_bar, levels_at_asof)

        # Strict break enforcement: the locked algorithm uses an asymmetric
        # break rule that TOLERATES line crosses for descending lines once
        # the projection drops below zero. For the Extension Peek scan that's
        # the wrong shape — a "broken" trendline isn't a trendline. So we
        # re-filter the descending candidates with the strict _has_line_break
        # check (the same one the algorithm already applies to ascending
        # lines) and re-rank top-3 from the survivors. Ascending side passes
        # through untouched.
        all_cands = snap.get("all_candidates") or []
        clean_upper = []
        clean_lower = []
        for c in all_cands:
            if c["anchor_type"] == "peak_anchored":
                if _has_line_break(ext, c["i0"], c["v0"], c["i1"], c["v1"], asof_bar):
                    continue
                clean_upper.append(c)
            else:
                clean_lower.append(c)
        clean_upper.sort(key=lambda c: abs(c["signed_dist"]))
        clean_lower.sort(key=lambda c: abs(c["signed_dist"]))
        clean_upper = clean_upper[:3]
        clean_lower = clean_lower[:3]

        def _pack(slots):
            packed = []
            for s in (slots or [])[:3]:
                packed.append({
                    "i0":        int(s["i0"]),
                    "v0":        float(s["v0"]),
                    "i1":        int(s["i1"]),
                    "v1":        float(s["v1"]),
                    "slope":     float(s["slope"]),
                    "anchor_type": str(s["anchor_type"]),
                    "proj_asof": float(s["proj_asof"]),
                    "signed_dist": float(s["signed_dist"]),
                    "span":      int(s["span"]),
                })
            return packed

        payload = {
            "asof_bar":   asof_bar,
            "asof_date":  asof_date,
            "asof_ext50": asof_ext50,
            "u":          _pack(clean_upper),
            "l":          _pack(clean_lower),
        }
        return ticker, payload, None
    except Exception as exc:
        return ticker, None, repr(exc)


# ── Main driver ──────────────────────────────────────────────

def build(workers=None, verbose=True):
    cache, src_path, is_intraday = _load_ohlcv_cache()
    n_cache = len(cache)
    if verbose:
        print(f"OHLCV cache: {src_path}")
        print(f"  {n_cache} tickers")
        if is_intraday:
            print(f"  intraday cache detected — dropping today's partial bar before fitting")
        if "SPY" in cache:
            try:
                last_bar = str(cache["SPY"]["date"].iloc[-1])[:10]
                print(f"  SPY last bar in cache: {last_bar}")
            except Exception:
                pass

    # Build the per-ticker work list. Skip any UNIVERSE tickers missing from cache.
    work = []
    missing = []
    for tk in UNIVERSE:
        if tk in cache:
            work.append((tk, cache[tk], is_intraday))
        else:
            missing.append(tk)
    if verbose:
        print(f"  UNIVERSE={len(UNIVERSE)}  present={len(work)}  missing={len(missing)}")
        if missing:
            print(f"  missing tickers: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")

    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    if verbose:
        print(f"\nComputing snapshots with {workers} workers...")

    out = {}
    errors = {}
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(_ticker_snapshot, item) for item in work]
        for fut in as_completed(futures):
            ticker, payload, err = fut.result()
            done += 1
            if payload is not None:
                out[ticker] = payload
            else:
                errors[ticker] = err
            if verbose and done % 50 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(work) - done) / rate if rate > 0 else 0
                print(f"  {done}/{len(work)}  elapsed={elapsed:.1f}s  rate={rate:.1f}/s  ETA={eta:.0f}s")
    elapsed = time.time() - t0
    if verbose:
        print(f"\nDone in {elapsed:.1f}s — {len(out)} snapshots, {len(errors)} errors")
        if errors:
            print(f"  Sample errors:")
            for tk, e in list(errors.items())[:5]:
                print(f"    {tk}: {e}")

    # Write atomic
    out_path = os.path.join(CACHE_DIR, "ext50_trendline_snapshots.json")
    tmp_path = out_path + ".tmp"
    payload_doc = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "built_in_seconds": round(elapsed, 1),
        "source_cache": os.path.basename(src_path),
        "intraday_partial_dropped": bool(is_intraday),
        "n_universe": len(UNIVERSE),
        "n_snapshots": len(out),
        "n_errors": len(errors),
        "tickers": out,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload_doc, f, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    if verbose:
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        print(f"\nWrote {out_path} ({size_mb:.2f} MB)")
    return payload_doc


if __name__ == "__main__":
    build()
