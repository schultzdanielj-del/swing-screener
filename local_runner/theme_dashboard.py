"""Hot Theme Dashboard generator.

Builds a single self-contained HTML file with one section per theme.
Each section shows an equal-weight synthetic composite chart (Plotly,
interactive, candles + SMA 5/10/20 + volume + MACD 6/20/9) and a grid
of member mini-charts (hand-built SVG, 100 daily bars each).

Visual design pulled verbatim from
swing-screener-regime-meter/regime_meter/dashboard/colors_and_type.css
(TC2000-flavored: black canvas, gray gradient chrome strips, bright
green/red candles, cyan price axis).

Usage:
    python local_runner/theme_dashboard.py [--theme NAME] [--bars 250] [--open]
"""

import argparse
import os
import pickle
import sys
import textwrap
import webbrowser
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.environ.get("SCANPERFECT_REPO_ROOT", os.path.dirname(LOCAL_DIR))
CACHE_DIR = os.environ.get("SCANPERFECT_CACHE_DIR", os.path.join(LOCAL_DIR, "cache"))
sys.path.insert(0, LOCAL_DIR)

from vectorized_indicators import sma_2d, ema_2d, macd_2d, atr_2d  # noqa: E402
from theme_map import THEMES, THEME_LABELS, UNIVERSE  # noqa: E402
try:
    from theme_map import THEME_NARRATIVES  # noqa: E402
except ImportError:
    THEME_NARRATIVES = {}
try:
    from theme_map import (  # noqa: E402
        MACROTHEMES, NARRATIVE_ZONES, THEME_CHAIN_POSITION,
        TICKER_ZONE_OVERRIDE, NARRATIVE_ZONE_PRIORITY,
    )
except ImportError:
    MACROTHEMES, NARRATIVE_ZONES, THEME_CHAIN_POSITION = {}, {}, {}
    TICKER_ZONE_OVERRIDE, NARRATIVE_ZONE_PRIORITY = {}, []

# ────────────────────────────────────────────────────────────
# Palette (mirrors regime_meter/dashboard/colors_and_type.css)
# ────────────────────────────────────────────────────────────
COLOR_BG = "#000000"
COLOR_GRID = "#1a1a1c"
COLOR_BORDER_SOFT = "#2a2c30"
COLOR_BORDER_STRONG = "#7a8088"
COLOR_TEXT = "#ffffff"
COLOR_TEXT_DIM = "#c8ccd2"
COLOR_TEXT_MUTED = "#888c92"
COLOR_UP = "#1eff1e"
COLOR_DOWN = "#ff3030"
COLOR_CYAN = "#5fc8ff"
COLOR_GOLD = "#ffcc00"
COLOR_ORANGE = "#ff8800"
COLOR_CHROME_TOP = "#6e737d"
COLOR_CHROME_BOT = "#535860"

OUTPUT_HTML = os.path.join(CACHE_DIR, "theme_dashboard.html")
PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"


# ════════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════════

def load_fundamentals():
    """Load per-ticker sector/industry from fundamentals_cache.json. Returns {} on failure."""
    path = os.path.join(CACHE_DIR, "fundamentals_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tickers", {})
    except Exception as exc:
        print(f"WARNING: could not load fundamentals_cache: {exc}")
        return {}


def load_company_meta():
    """Load per-ticker longName + longBusinessSummary from company_meta.json. Returns {} when missing."""
    path = os.path.join(CACHE_DIR, "company_meta.json")
    if not os.path.exists(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tickers", {})
    except Exception as exc:
        print(f"WARNING: could not load company_meta: {exc}")
        return {}


def _first_sentences(text, n=2):
    """Return first n sentences of text, joined.

    Standard period-splitting breaks on company suffixes like "Inc.", "Corp.", "Ltd.",
    "N.V." and "Co." — which would clip business descriptions to just the company name.
    We mask those tokens before splitting so the first real sentence survives.
    """
    if not text:
        return ""
    import re
    SAFE = ""
    abbrevs = [
        "Inc.", "Corp.", "Ltd.", "LLC.", "L.L.C.", "L.P.", "LP.", "Co.",
        "PLC.", "Plc.", "plc.", "N.V.", "S.A.", "A.G.", "Pty.", "Pte.",
        "St.", "Mr.", "Mrs.", "Ms.", "Dr.", "Jr.", "Sr.",
        "U.S.", "U.K.", "U.S.A.", "E.U.",
        "vs.", "etc.", "e.g.", "i.e.", "No.",
    ]
    masked = text
    for ab in abbrevs:
        masked = masked.replace(ab, ab.replace(".", SAFE))
    parts = re.split(r'(?<=[.!?])\s+', masked.strip())
    joined = " ".join(parts[:n])
    return joined.replace(SAFE, ".")


def _xml_escape(s):
    """Escape XML special characters for safe insertion into SVG text/attributes."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _truncate(s, n):
    """Truncate to n chars with an ellipsis if it overflows."""
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else (s[: n - 1].rstrip() + "…")


def validate_theme_sectors(themes, fundamentals, company_meta=None):
    """Cross-check every ticker in THEMES against its GICS sector from fundamentals.

    For each theme, finds the dominant sector(s) of its members. Any member
    whose sector is not among the top 2 most-common is flagged as a likely
    miscategorization (e.g., a solar ticker accidentally placed in Payments).

    When ``company_meta`` (from company_meta.json) is provided, each flagged
    ticker is printed with its ``longName`` and the first sentence of its
    ``longBusinessSummary`` so the human review is evidence-based rather than
    label-based. The sector cross-check remains the primary safety net — the
    longName / summary lines are added context, not a replacement.

    Prints a warning block on stdout. Does not halt — humans review and edit.

    Returns list of (theme_key, ticker, ticker_sector, theme_dominant_sector) outliers.
    """
    if not fundamentals:
        print("WARNING: no fundamentals_cache available — skipping sector cross-check.")
        return []

    company_meta = company_meta or {}
    from collections import Counter
    outliers = []
    print("\n" + "=" * 78)
    print("SECTOR CROSS-CHECK against fundamentals_cache.json")
    if company_meta:
        print("(longName + first sentence of longBusinessSummary added from company_meta.json)")
    print("=" * 78)

    for theme_key, tickers in themes.items():
        ticker_sectors = []
        unknown_sector = []
        for tk in tickers:
            info = fundamentals.get(tk, {})
            sec = info.get("sector") if isinstance(info, dict) else None
            if sec:
                ticker_sectors.append((tk, sec))
            else:
                unknown_sector.append(tk)
        if len(ticker_sectors) < 2:
            continue  # need at least 2 members to derive a "dominant" sector

        sector_counts = Counter(s for _, s in ticker_sectors)
        top_two = [s for s, _ in sector_counts.most_common(2)]
        for tk, sec in ticker_sectors:
            if sec not in top_two:
                outliers.append((theme_key, tk, sec, top_two[0]))

    if outliers:
        print(f"\n[!] {len(outliers)} potential miscategorizations "
              f"(ticker's sector != theme's dominant sector):\n")
        # Group by theme
        by_theme = {}
        for theme_key, tk, sec, dominant in outliers:
            by_theme.setdefault(theme_key, []).append((tk, sec, dominant))
        for theme_key in sorted(by_theme.keys()):
            print(f"  Theme '{theme_key}' (dominant sector: {by_theme[theme_key][0][2]}):")
            for tk, sec, _ in by_theme[theme_key]:
                info = fundamentals.get(tk, {})
                industry = info.get("industry") if isinstance(info, dict) else None
                print(f"    {tk:6s}  sector={sec:25s}  industry={industry}")
                meta_info = company_meta.get(tk) if isinstance(company_meta, dict) else None
                if isinstance(meta_info, dict):
                    long_name = meta_info.get("longName") or ""
                    long_summary = meta_info.get("longBusinessSummary") or ""
                    first_sent = _first_sentences(long_summary, n=1) if long_summary else ""
                    if long_name or first_sent:
                        if long_name:
                            print(f"           name: {long_name}")
                        if first_sent:
                            # Keep the printed line readable — clip very long sentences
                            clipped = first_sent if len(first_sent) <= 200 else (first_sent[:197].rstrip() + "...")
                            print(f"           biz : {clipped}")
        print("\nIf any of these are actually correct (overlapping narrative), ignore.")
        print("Otherwise, edit local_runner/theme_map.py and rerun.")
    else:
        print("\n[OK] No outliers - every ticker's sector matches its theme's dominant sector.")
    print("=" * 78 + "\n")
    return outliers


def load_daily_cache():
    """Load the OHLCV daily cache.

    Prefers `universe_ohlcv_daily_intraday.pkl` when it exists AND its mtime
    is newer than the main `universe_ohlcv_daily.pkl`'s mtime. This is the
    handoff contract with `local_runner/intraday_refresh.py`: the intraday
    refresh writes the intraday pickle at ~4:20 PM ET; next morning's
    nightly overwrites the main pickle (mtime advances), so the intraday
    pickle is automatically ignored from that point on.

    Returns a tuple ``(cache, source_meta)`` where ``source_meta`` is a dict
    with keys ``source`` ('main' | 'intraday'), ``last_bar_date`` (string
    YYYY-MM-DD or '?'), ``label`` (display label suffix or '').
    """
    main_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    intraday_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily_intraday.pkl")
    legacy_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")

    chosen = None
    source = None
    if os.path.exists(intraday_path) and os.path.exists(main_path):
        if os.path.getmtime(intraday_path) > os.path.getmtime(main_path):
            chosen = intraday_path
            source = "intraday"
    if chosen is None:
        if os.path.exists(main_path):
            chosen = main_path
            source = "main"
        elif os.path.exists(legacy_path):
            chosen = legacy_path
            source = "main"
        else:
            raise FileNotFoundError(f"No OHLCV cache found in {CACHE_DIR}. Run cache_builder.py first.")

    print(f"Loading {chosen}  (source={source})")
    with open(chosen, "rb") as f:
        cache = pickle.load(f)
    n = len(cache)
    print(f"  {n} tickers in cache")
    if n < 11_200:
        raise RuntimeError(f"Cache has only {n} tickers (expected ~11,500). Aborting.")

    last_bar = "?"
    if "SPY" in cache:
        try:
            last_bar = str(cache["SPY"]["date"].iloc[-1])[:10]
            print(f"  SPY last bar: {last_bar}")
        except Exception:
            pass

    label = ""
    if source == "intraday":
        # Use the human label the refresh wrote alongside the pickle (e.g.
        # "intraday 9:44am" / "intraday 4:20pm"); fall back to a generic tag
        # if the marker is missing or unreadable.
        label = "(intraday)"
        import json
        marker_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily_intraday.meta")
        try:
            with open(marker_path, encoding="utf-8") as mf:
                lbl = json.load(mf).get("label", "").strip()
            if lbl:
                label = f"({lbl})"
        except (OSError, ValueError):
            pass
    source_meta = {"source": source, "last_bar_date": last_bar, "label": label}
    return cache, source_meta


# ════════════════════════════════════════════════════════════
# COMPOSITE BUILDER
# ════════════════════════════════════════════════════════════

def _canonical_dates(members_data, n_bars):
    """Return last n_bars dates from the member with the most-recent last bar."""
    best_dates = None
    best_last = None
    for _, df in members_data:
        if df.empty:
            continue
        last = df["date"].iloc[-1]
        if best_last is None or last > best_last:
            best_last = last
            best_dates = df["date"].tail(n_bars).tolist()
    return best_dates


def _find_pivots(arr, w, kind):
    """Find swing pivot indices in a 1D array.

    A pivot at index i requires arr[i] to be the strict extremum within
    arr[i-w:i+w+1]. kind: 'low' or 'high'. Returns sorted list of indices.
    """
    n = len(arr)
    out = []
    for i in range(w, n - w):
        v = arr[i]
        if np.isnan(v):
            continue
        window = arr[i - w : i + w + 1]
        if kind == "low":
            if v == np.nanmin(window):
                out.append(i)
        else:
            if v == np.nanmax(window):
                out.append(i)
    return out


def _line_clean(series, p1_idx, p2_idx, side):
    """Return True if the trendline from (p1_idx, series[p1_idx]) to
    (p2_idx, series[p2_idx]) is NOT pierced by intervening bars.

    side='low'  : check no intermediate low < line value (bullish trendline).
    side='high' : check no intermediate high > line value (bearish trendline).
    """
    if p2_idx <= p1_idx + 1:
        return True
    p1_val = series[p1_idx]
    p2_val = series[p2_idx]
    if np.isnan(p1_val) or np.isnan(p2_val):
        return False
    inner = np.arange(p1_idx + 1, p2_idx)
    line_vals = p1_val + (p2_val - p1_val) * (inner - p1_idx) / (p2_idx - p1_idx)
    inner_vals = series[inner]
    mask = ~np.isnan(inner_vals)
    if not np.any(mask):
        return True
    if side == "low":
        # Allow a tiny float epsilon so equal-to-line bars don't reject
        return not np.any(inner_vals[mask] < line_vals[mask] - 1e-9)
    else:
        return not np.any(inner_vals[mask] > line_vals[mask] + 1e-9)


def detect_divergences(df, w=3, lookback=240, min_spacing=6, macd_offset_max=10):
    """Detect every bullish and bearish MACD-line divergence in the window.

    Divergence is measured against the MACD LINE (EMA6 − EMA20).

    For each price-pivot (anchor), the algorithm walks backward through prior
    price pivots and records the FIRST prior pivot that forms a divergence —
    one divergence per anchor. This means longer chains (A→B→C, all making
    HH price + LH MACD) produce multiple consecutive divergences, not one
    summary span.

    Returns dict {'bull': [div, ...], 'bear': [div, ...]} ordered oldest first.
    """
    close = df["close"].values.astype(np.float64)
    high  = df["high"].values.astype(np.float64)
    low   = df["low"].values.astype(np.float64)
    n = len(close)
    if n < 60:
        return {"bull": [], "bear": []}

    macd_line = macd_2d(close.reshape(1, -1), 6, 20)[0]

    price_lows   = [p for p in _find_pivots(low,       w=w, kind="low")   if p >= n - lookback]
    price_highs  = [p for p in _find_pivots(high,      w=w, kind="high")  if p >= n - lookback]
    macd_lows    = [m for m in _find_pivots(macd_line, w=w, kind="low")   if m >= n - lookback - macd_offset_max]
    macd_highs   = [m for m in _find_pivots(macd_line, w=w, kind="high")  if m >= n - lookback - macd_offset_max]

    def _match_macd(macd_pivots, price_idx):
        cand = [m for m in macd_pivots if abs(m - price_idx) <= macd_offset_max]
        if not cand:
            return None
        return min(cand, key=lambda m: abs(m - price_idx))

    out = {"bull": [], "bear": []}

    # ── Bullish: scan every price-low as anchor, look for the nearest prior pivot that creates a div ──
    for idx_anchor in range(1, len(price_lows)):
        p_anchor = price_lows[idx_anchor]
        m_anchor = _match_macd(macd_lows, p_anchor)
        if m_anchor is None:
            continue
        for p_prior in reversed(price_lows[:idx_anchor]):
            if p_anchor - p_prior < min_spacing:
                continue
            if not (low[p_anchor] < low[p_prior]):
                continue
            m_prior = _match_macd(macd_lows, p_prior)
            if m_prior is None:
                continue
            if not (macd_line[m_anchor] > macd_line[m_prior]):
                continue
            # Trendline-clean validation: no intermediate low may pierce below
            # the connecting line, otherwise the divergence is invalidated.
            if not _line_clean(low, p_prior, p_anchor, side="low"):
                continue
            out["bull"].append(dict(
                p1_idx=p_prior, p2_idx=p_anchor,
                p1_price=float(low[p_prior]), p2_price=float(low[p_anchor]),
                m1_idx=m_prior, m2_idx=m_anchor,
                m1_macd=float(macd_line[m_prior]),
                m2_macd=float(macd_line[m_anchor]),
            ))
            break

    # ── Bearish: scan every price-high as anchor ──
    for idx_anchor in range(1, len(price_highs)):
        p_anchor = price_highs[idx_anchor]
        m_anchor = _match_macd(macd_highs, p_anchor)
        if m_anchor is None:
            continue
        for p_prior in reversed(price_highs[:idx_anchor]):
            if p_anchor - p_prior < min_spacing:
                continue
            if not (high[p_anchor] > high[p_prior]):
                continue
            m_prior = _match_macd(macd_highs, p_prior)
            if m_prior is None:
                continue
            if not (macd_line[m_anchor] < macd_line[m_prior]):
                continue
            # Trendline-clean validation: no intermediate high may pierce above
            # the connecting line, otherwise the divergence is invalidated.
            if not _line_clean(high, p_prior, p_anchor, side="high"):
                continue
            out["bear"].append(dict(
                p1_idx=p_prior, p2_idx=p_anchor,
                p1_price=float(high[p_prior]), p2_price=float(high[p_anchor]),
                m1_idx=m_prior, m2_idx=m_anchor,
                m1_macd=float(macd_line[m_prior]),
                m2_macd=float(macd_line[m_anchor]),
            ))
            break

    return out


def n_period_return(close_arr, period):
    """Return the n-period % return based on close prices. None if insufficient data."""
    if close_arr is None or len(close_arr) < period + 1:
        return None
    c_now = close_arr[-1]
    c_then = close_arr[-(period + 1)]
    if c_then is None or c_then == 0 or np.isnan(c_then) or np.isnan(c_now):
        return None
    return (c_now / c_then - 1.0) * 100.0


def adr_pct(high_arr, low_arr, period=20):
    """TC2000-style 20-bar ADR percent: mean(H / L - 1) * 100. None if insufficient data."""
    if high_arr is None or low_arr is None:
        return None
    if len(high_arr) < period or len(low_arr) < period:
        return None
    h = np.asarray(high_arr[-period:], dtype=np.float64)
    l = np.asarray(low_arr[-period:], dtype=np.float64)
    mask = (~np.isnan(h)) & (~np.isnan(l)) & (l > 0)
    if not np.any(mask):
        return None
    ratios = (h[mask] / l[mask] - 1.0) * 100.0
    val = float(np.mean(ratios))
    return val if val > 0 else None


def load_extension_peek_snapshot():
    """Load the ext50-trendline snapshot built by ext50_trendline_snapshot_builder.

    Returns the parsed JSON doc or None if missing / unreadable. The doc
    carries each ticker's u1/u2/u3 + l1/l2/l3 line equations as of the
    most recent EOD bar. The dashboard's live Extension-Peek scan
    projects those lines forward and compares against today's live ext50.
    """
    import json as _json
    path = os.path.join(CACHE_DIR, "ext50_trendline_snapshots.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def load_first_flags_snapshot():
    """Load the First Flags snapshot built by first_flags_snapshot_builder.

    Returns the parsed JSON doc or None if missing / unreadable. The doc
    carries every ticker whose most-recent bullish MACD 6/20 divergence
    bottomed below its 200-SMA and has since produced a >=25% pole.
    """
    import json as _json
    path = os.path.join(CACHE_DIR, "first_flags_snapshots.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def compute_first_flags(snapshot_doc, cache):
    """Turn the First Flags snapshot into match rows, refreshing the pullback
    against today's live close.

    The snapshot fixes the bottom + pole for the trading day (computed at the
    prior EOD bar). Here we only re-derive how far the live close sits below
    that pole, so the "Pullback %" column tracks the intraday tape. No
    divergence re-detection — pivots stay fixed for the day.

    Returns (list_of_match_dicts, asof_date).
    """
    if not snapshot_doc:
        return [], "?"
    tickers = snapshot_doc.get("tickers", {})
    sample = next(iter(tickers.values()), None)
    asof_date = (sample or {}).get("asof_date") or "?"
    out = []
    for tk, p in tickers.items():
        pole_high = p.get("pole_high_price") or 0.0
        last_close = p.get("bottom_close")
        df = cache.get(tk)
        if df is not None and len(df) > 0:
            try:
                lc = float(df["close"].iloc[-1])
                if not np.isnan(lc):
                    last_close = lc
            except Exception:
                pass
        if pole_high > 0 and last_close is not None:
            pullback = max(0.0, (pole_high - last_close) / pole_high * 100.0)
        else:
            pullback = p.get("pullback_pct", 0.0)
        row = dict(p)
        row["ticker"] = tk
        row["live_pullback_pct"] = pullback
        out.append(row)
    return out, asof_date


def load_tightening_range_snapshot():
    """Load the Tightening Range snapshot built by tightening_range_snapshot_builder.

    Returns the parsed JSON doc or None if missing / unreadable. The doc carries
    each ticker's converging-wedge state per timeframe (daily/weekly/monthly).
    """
    import json as _json
    path = os.path.join(CACHE_DIR, "tightening_range_snapshots.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def compute_tightening_ranges(snapshot_doc):
    """Flatten the Tightening Range snapshot into match rows (one per ticker ×
    matched timeframe). The wedge lines are fixed for the trading day; no live
    refresh. Returns (list_of_rows, asof_date) where each row is
    {ticker, tf, ...payload}.
    """
    if not snapshot_doc:
        return [], "?"
    tickers = snapshot_doc.get("tickers", {})
    asof_date = "?"
    out = []
    for tk, per_tf in tickers.items():
        for tf, p in per_tf.items():
            row = dict(p)
            row["ticker"] = tk
            row["tf"] = tf
            out.append(row)
            if asof_date == "?":
                asof_date = p.get("asof_date") or "?"
    return out, asof_date


def _compute_ext50_series_for_chart(df, adr_period=20):
    """Full ext50 series aligned to the ticker's full OHLCV history.

    Same 20-bar ADR formula as the snapshot builder + the live peek check.
    Returns an array of len(df); leading bars before the SMA50/ADR20
    lookback are NaN. None if < 50 bars.
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


def _live_ext50_for_peek(df, adr_period=20):
    """Today's ext50 value using 20-bar ADR. Matches the snapshot builder."""
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    n = len(c)
    if n < 50:
        return None
    sma50 = sma_2d(c.reshape(1, -1), 50)[0]
    win_h = h[-adr_period:]
    win_l = l[-adr_period:]
    mask = (~np.isnan(win_h)) & (~np.isnan(win_l)) & (win_l > 0)
    if mask.sum() < 1:
        return None
    adr = float(np.mean((win_h[mask] / win_l[mask] - 1.0) * 100.0))
    if adr <= 0 or sma50[-1] <= 0:
        return None
    return ((c[-1] - sma50[-1]) / sma50[-1] * 100.0) / adr


# Extension Peek upper bound: skip breaks where price has already extended
# this many ADRs (or more) above the 50-day SMA. Keeps the setup to early
# breaks, not chases. Tune here.
EXT50_PEEK_MAX_EXTENSION_ADR = 4.5


def compute_extension_peeks(snapshot_doc, cache, universe):
    """Scan UNIVERSE for tickers peeking above a descending 50-SMA-extension
    trendline today.

    Peek rule (per locked sign convention signed_dist = proj - ext):
      - Yesterday's stored signed_dist >= 0 (price was at/below the line)
      - Today's live signed_dist  <  0 (price has crossed above)
      - Today's ext50 < EXT50_PEEK_MAX_EXTENSION_ADR (not already stretched
        too far above the 50 SMA — keeps the setup to early breaks)

    Sorted ascending by |today_sd| — tightest peek first (the "just barely
    poked through" candidates that historically rip the next day).

    Returns a list of dicts; empty if snapshot_doc is None.
    """
    if not snapshot_doc or "tickers" not in snapshot_doc:
        return []
    snap_tickers = snapshot_doc["tickers"]
    matches = []
    for tk in universe:
        snap = snap_tickers.get(tk)
        if not snap or not snap.get("u"):
            continue
        df = cache.get(tk)
        if df is None:
            continue
        today_ext = _live_ext50_for_peek(df)
        if today_ext is None:
            continue
        # Don't include breaks that have already run too far above the 50 SMA.
        # today_ext is the signed extension in ADR units (positive = above SMA).
        # Names peeking from below the SMA (negative) still pass — only those
        # already stretched >= EXT50_PEEK_MAX_EXTENSION_ADR above it are dropped.
        if today_ext >= EXT50_PEEK_MAX_EXTENSION_ADR:
            continue
        today_bar = len(df) - 1
        # Check u1/u2/u3 in proximity order — keep the first that peeks.
        for slot_idx, u in enumerate(snap["u"], 1):
            proj_today = u["v1"] + u["slope"] * (today_bar - u["i1"])
            today_sd = proj_today - today_ext   # locked convention
            yest_sd  = u["signed_dist"]
            if today_sd < 0 and yest_sd >= 0:
                matches.append(dict(
                    ticker=tk,
                    slot=slot_idx,
                    today_sd=today_sd,        # negative = above line
                    today_sd_abs=abs(today_sd),
                    yest_sd=yest_sd,
                    today_ext=today_ext,
                    proj_today=proj_today,
                    slope=float(u["slope"]),
                    v0=float(u["v0"]),
                    v1=float(u["v1"]),
                    i0=int(u["i0"]),
                    i1=int(u["i1"]),
                    line_drop=float(u["v0"] - u["v1"]),
                    span=int(u.get("span", 0)),
                ))
                break  # tightest line per ticker only
    matches.sort(key=lambda m: m["today_sd_abs"])
    return matches


def tc2000_rs_raw(open_arr, high_arr, low_arr, close_arr, n_bars=5):
    """Relative-strength PCF, averaged over n_bars bars.

    PCF:
        avg = mean over last n_bars of (close / prev_close - 1) * 100
        mult = ((close + close_50_bars_ago) / 2) / ATR50
        RS = avg * mult

    The "today's strength" component measures CLOSE vs PREVIOUS CLOSE
    (day-over-day return), not close vs same-day open. So a market that
    gaps up and fades intraday still reads as a positive day, matching
    end-of-day P&L. ``open_arr`` is retained in the signature for caller
    compatibility but is no longer used.

    Returns None if insufficient data.
    """
    if close_arr is None or high_arr is None or low_arr is None:
        return None
    n = len(close_arr)
    # Need n_bars closes plus one prior close for the day-over-day return,
    # and the 50-bar lookback for the multiplier.
    if n < max(51, n_bars + 1):
        return None
    h = np.asarray(high_arr,  dtype=np.float64)
    l = np.asarray(low_arr,   dtype=np.float64)
    c = np.asarray(close_arr, dtype=np.float64)

    # Average close-to-PREVIOUS-close % change over last n_bars bars
    c_win  = c[-n_bars:]
    c_prev = c[-n_bars - 1:-1]
    if np.any(np.isnan(c_win)) or np.any(np.isnan(c_prev)) or np.any(c_prev == 0):
        return None
    pct_changes = (c_win / c_prev - 1.0) * 100.0
    avg_change = float(np.mean(pct_changes))

    # Close, and close 50 bars ago
    c_last  = c[-1]
    c_50ago = c[-51]
    if np.isnan(c_last) or np.isnan(c_50ago) or c_50ago <= 0:
        return None

    # ATR50 — TC2000 style (SMA of true range)
    atr_arr = atr_2d(h.reshape(1, -1), l.reshape(1, -1), c.reshape(1, -1), 50)[0]
    atr50 = atr_arr[-1]
    if np.isnan(atr50) or atr50 <= 0:
        return None

    multiplier = (float(c_last) + float(c_50ago)) / 2.0 / float(atr50)
    return avg_change * multiplier


def compression_n(high_arr, low_arr, n_bars, adr_value):
    """N-bar range as a multiple of the 20-bar ADR%.

        comp = (maxH[-n:] / minL[-n:] - 1) * 100 / ADR%

    Lower = tighter consolidation. A value of 1.0 means the N-bar
    range equals one typical day's range. Returns None on insufficient
    data or invalid ADR.
    """
    if high_arr is None or low_arr is None or adr_value is None or adr_value <= 0:
        return None
    if len(high_arr) < n_bars or len(low_arr) < n_bars:
        return None
    h = np.asarray(high_arr[-n_bars:], dtype=np.float64)
    l = np.asarray(low_arr[-n_bars:],  dtype=np.float64)
    mask = (~np.isnan(h)) & (~np.isnan(l)) & (l > 0)
    if not np.any(mask):
        return None
    max_h = float(np.max(h[mask]))
    min_l = float(np.min(l[mask]))
    if min_l <= 0:
        return None
    range_pct = (max_h / min_l - 1.0) * 100.0
    return range_pct / float(adr_value)


def tc2000_rs_intraday(open_arr, high_arr, low_arr, close_arr):
    """Intraday version of tc2000_rs_raw — single bar, today only.

    PCF (identical to tc2000_rs_raw, only the "today's strength" term
    is intraday close-vs-open instead of close-vs-previous-close):
        avg = (close / open - 1) * 100        # today only, single bar
        mult = ((close + close_50_bars_ago) / 2) / ATR50
        RS = avg * mult

    The same price-per-ATR multiplier means the score is comparable
    across tickers with different ADRs, identical to the existing
    multi-bar windows.

    Returns None if insufficient data.
    """
    if open_arr is None or close_arr is None or high_arr is None or low_arr is None:
        return None
    n = len(close_arr)
    if n < 51:
        return None
    o = np.asarray(open_arr,  dtype=np.float64)
    h = np.asarray(high_arr,  dtype=np.float64)
    l = np.asarray(low_arr,   dtype=np.float64)
    c = np.asarray(close_arr, dtype=np.float64)

    o_today = o[-1]; c_today = c[-1]
    if np.isnan(o_today) or np.isnan(c_today) or o_today == 0:
        return None
    avg_change = (c_today / o_today - 1.0) * 100.0

    c_50ago = c[-51]
    if np.isnan(c_50ago) or c_50ago <= 0:
        return None
    atr_arr = atr_2d(h.reshape(1, -1), l.reshape(1, -1), c.reshape(1, -1), 50)[0]
    atr50 = atr_arr[-1]
    if np.isnan(atr50) or atr50 <= 0:
        return None
    multiplier = (float(c_today) + float(c_50ago)) / 2.0 / float(atr50)
    return avg_change * multiplier


def position_vs_200d(df):
    """Return composite position relative to its 200-day SMA at the last bar.

    Returns (pct_distance, label, css_class).
      pct_distance: float, e.g. +12.4 means 12.4% above SMA200, -8.2 means below.
                    None if the composite is shorter than 200 bars.
      label: "> 200D" / "< 200D" / "—"
      css_class: "pos-above" / "pos-below" / "pos-na"
    """
    close = df["close"].values.astype(np.float64)
    n = len(close)
    if n < 200:
        return None, "—", "pos-na"
    sma200 = sma_2d(close.reshape(1, -1), 200)[0]
    last_sma = sma200[-1]
    last_c = close[-1]
    if np.isnan(last_sma) or last_sma <= 0:
        return None, "—", "pos-na"
    pct = (last_c - last_sma) / last_sma * 100.0
    if pct >= 0:
        return float(pct), "> 200D", "pos-above"
    else:
        return float(pct), "< 200D", "pos-below"


def build_composite(theme_tickers, cache, n_bars=250):
    """Equal-weighted composite OHLC for a theme.

    For each member: find first valid bar in window, scale OHLC and volume
    so close at that first bar = 100. Members contribute to the composite
    only from their first-valid bar onward. Composite OHLC at each bar is
    the arithmetic mean across members that have data at that bar.

    Returns: (composite_df, used_tickers, missing_tickers)
        composite_df: DataFrame[date, open, high, low, close, volume, n_members]
        used_tickers: list of tickers present in OHLCV cache and used
        missing_tickers: list of tickers in the theme but not in cache
    """
    members_data = []
    missing = []
    for tk in theme_tickers:
        if tk not in cache:
            missing.append(tk)
            continue
        df = cache[tk]
        if df is None or len(df) < 10:
            missing.append(tk)
            continue
        members_data.append((tk, df))

    if not members_data:
        return None, [], missing

    canonical_dates = _canonical_dates(members_data, n_bars)
    if not canonical_dates:
        return None, [], missing
    date_index = pd.Index(canonical_dates, name="date")

    cols = ["open", "high", "low", "close", "volume"]
    stacked = {col: [] for col in cols}
    used_tickers = []
    for tk, df in members_data:
        s = df.set_index("date").reindex(date_index)
        valid_close = s["close"].notna()
        if not valid_close.any():
            continue
        first_idx_pos = valid_close.values.argmax()
        base_close = s["close"].iloc[first_idx_pos]
        base_vol = s["volume"].iloc[first_idx_pos]
        if base_close is None or base_close <= 0 or np.isnan(base_close):
            continue
        scale_p = 100.0 / base_close
        scale_v = 100.0 / base_vol if (base_vol and not np.isnan(base_vol) and base_vol > 0) else np.nan
        for col in ["open", "high", "low", "close"]:
            stacked[col].append(s[col].values * scale_p)
        stacked["volume"].append(s["volume"].values * scale_v)
        used_tickers.append(tk)

    if not used_tickers:
        return None, [], missing

    out = {"date": canonical_dates}
    for col in cols:
        arr = np.vstack(stacked[col])
        out[col] = np.nanmean(arr, axis=0)
    out["n_members"] = np.sum(~np.isnan(np.vstack(stacked["close"])), axis=0)
    composite_df = pd.DataFrame(out)
    # Drop bars where fewer than half the members have data (mostly affects early window when newer IPOs missing)
    threshold = max(1, len(used_tickers) // 2)
    composite_df = composite_df[composite_df["n_members"] >= threshold].reset_index(drop=True)
    return composite_df, used_tickers, missing


def _round_list(arr, ndigits=4):
    """Round a 1-D numeric array, replacing NaN with None for JSON."""
    out = []
    for v in arr:
        if v is None:
            out.append(None)
        elif isinstance(v, float) and np.isnan(v):
            out.append(None)
        else:
            out.append(round(float(v), ndigits))
    return out


def is_momo(df):
    """A "momo": a >=30% low-to-high run over the last ~50 bars. Take the lowest
    low in the window and the highest high AT OR AFTER it; True if that high is
    >=30% above the low — a stock that bottomed and ran (not one that fell)."""
    if df is None or len(df) < 2:
        return False
    n = min(50, len(df))
    h = np.asarray(df["high"].values[-n:], dtype=np.float64)
    l = np.asarray(df["low"].values[-n:], dtype=np.float64)
    if np.any(np.isnan(l)) or np.any(np.isnan(h)):
        return False
    lo_i = int(np.argmin(l))
    lo = float(l[lo_i])
    if lo <= 0:
        return False
    return bool((float(np.max(h[lo_i:])) / lo - 1.0) >= 0.30)


def is_tight_d1(df):
    """A "tight day": today's candle range is under 1.10 x the 20-bar ADR — the
    same definition the Tickers-view "Tight D1" filter uses (TC2000 ADR
    convention). False when there isn't enough history to form a 20-bar ADR."""
    if df is None or len(df) < 21:
        return False
    high_v = df["high"].values.astype(np.float64)
    low_v = df["low"].values.astype(np.float64)
    adr20 = adr_pct(high_v, low_v, 20)
    if adr20 is None or adr20 <= 0:
        return False
    h_last = float(high_v[-1]); l_last = float(low_v[-1])
    if np.isnan(h_last) or np.isnan(l_last) or l_last <= 0:
        return False
    todays_range_pct = (h_last / l_last - 1.0) * 100.0
    return bool((todays_range_pct / adr20) < 1.10)


def compute_ticker_pack(ticker, df, bench_rs_0d, bench_rs_1, bench_rs_3, bench_rs_5, bench_rs_20,
                        bench_rs_65, bench_rs_130, n_bars,
                        company_meta=None, fundamentals=None):
    """Compute everything needed to render a single ticker in the dashboard:

    - sidebar row stats (1d / 5d / 20d RS ratios vs Universe, position vs 200D)
    - chart data arrays (date axis + OHLCV for the last n_bars) ready for
      Plotly to render lazily in JS
    - MACD-line divergence pairs detected on the same window

    Returns None only when the ticker has fewer than 10 bars (matches
    the per-theme `build_composite` admission rule). Tickers with 10–50
    bars are still tradable in the tree: the chart renders against
    whatever bars exist, and the RS columns simply show `—` because
    `tc2000_rs_raw` returns None when the 50-bar lookback isn't satisfied
    yet. Without this, recent IPOs that show up in a theme's composite
    silently dropped from the sidebar expansion.
    """
    if df is None or len(df) < 10:
        return None

    win = df.tail(n_bars).reset_index(drop=True)
    if len(win) < 10:
        return None

    open_v  = win["open"].values.astype(np.float64)
    high_v  = win["high"].values.astype(np.float64)
    low_v   = win["low"].values.astype(np.float64)
    close_v = win["close"].values.astype(np.float64)
    vol_v   = win["volume"].values.astype(np.float64)
    dates   = [str(d)[:10] for d in win["date"].tolist()]

    # Per-ticker RS uses the FULL df history (needs 50-bar lookback) just
    # like the theme composite's RS does. Result is the ratio against the
    # Universe composite (equal-weight of all UNIVERSE tickers).
    rs0d_raw  = tc2000_rs_intraday(df["open"].values, df["high"].values,
                                    df["low"].values, df["close"].values)
    rs1_raw   = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=1)
    rs3_raw   = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=3)
    rs5_raw   = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=5)
    rs20_raw  = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=20)
    rs65_raw  = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=65)
    rs130_raw = tc2000_rs_raw(df["open"].values, df["high"].values,
                               df["low"].values, df["close"].values, n_bars=130)
    # Divide by abs(bench) so the sign of the displayed ratio always follows
    # the theme/ticker's own raw RS direction. Without abs(), a negative
    # benchmark day (e.g. broad gap-up-and-fade) would flip the sign of
    # every ratio — making intraday-strong themes display as weak and
    # intraday-weak themes display as strong.
    rs0d  = (rs0d_raw  / abs(bench_rs_0d))  if (rs0d_raw  is not None and bench_rs_0d)  else None
    rs1   = (rs1_raw   / abs(bench_rs_1))   if (rs1_raw   is not None and bench_rs_1)   else None
    rs3   = (rs3_raw   / abs(bench_rs_3))   if (rs3_raw   is not None and bench_rs_3)   else None
    rs5   = (rs5_raw   / abs(bench_rs_5))   if (rs5_raw   is not None and bench_rs_5)   else None
    rs20  = (rs20_raw  / abs(bench_rs_20))  if (rs20_raw  is not None and bench_rs_20)  else None
    rs65  = (rs65_raw  / abs(bench_rs_65))  if (rs65_raw  is not None and bench_rs_65)  else None
    rs130 = (rs130_raw / abs(bench_rs_130)) if (rs130_raw is not None and bench_rs_130) else None

    pct_200, pos_label, pos_css = position_vs_200d(df)
    below_200 = (pos_css == "pos-below")

    last_close = float(close_v[-1]) if not np.isnan(close_v[-1]) else None
    prev_close = float(close_v[-2]) if len(close_v) >= 2 and not np.isnan(close_v[-2]) else last_close
    if last_close is not None and prev_close and prev_close > 0:
        day_chg     = last_close - prev_close
        day_chg_pct = (last_close / prev_close - 1.0) * 100.0
    else:
        day_chg = 0.0
        day_chg_pct = 0.0
    vol_last = float(vol_v[-1]) if not np.isnan(vol_v[-1]) else 0.0

    five_d = n_period_return(close_v, 5)
    adr20  = adr_pct(high_v, low_v, 20)

    # Today's candle range as a ratio of the 20-day ADR. The "tight day"
    # filter on the Tickers view shows only tickers whose current daily
    # candle is tighter than 1.10 * ADR. (TC2000 ADR convention.)
    today_adr_ratio = None
    if adr20 is not None and adr20 > 0:
        h_last = float(high_v[-1]) if not np.isnan(high_v[-1]) else None
        l_last = float(low_v[-1])  if not np.isnan(low_v[-1])  else None
        if h_last is not None and l_last is not None and l_last > 0:
            todays_range_pct = (h_last / l_last - 1.0) * 100.0
            today_adr_ratio = todays_range_pct / adr20

    # Compression: N-bar range as a multiple of 20-bar ADR. Lower = tighter
    # consolidation. Uses the FULL df history (not the visible window) so
    # the value isn't truncated by the dashboard's display window.
    full_h = df["high"].values
    full_l = df["low"].values
    comp3  = compression_n(full_h, full_l, 3,  adr20)
    comp5  = compression_n(full_h, full_l, 5,  adr20)
    comp10 = compression_n(full_h, full_l, 10, adr20)
    comp20 = compression_n(full_h, full_l, 20, adr20)
    comp30 = compression_n(full_h, full_l, 30, adr20)

    # Distance from the 50-day SMA in ADRs — the same metric the Setups page
    # and the per-ticker extension panel use (positive = above the 50). Powers
    # the "Near 50SMA" filter on the Tickers view. None when < 50 bars.
    ext50 = _live_ext50_for_peek(df)

    # MACD-line divergences were removed from the dashboard 2026-05-22.
    # The MACD line + signal still render in the lower panel, but we no
    # longer detect or draw divergence pairs anywhere.

    long_name = ""
    long_summary = ""
    if company_meta and ticker in company_meta:
        long_name    = company_meta[ticker].get("longName", "") or ""
        long_summary = company_meta[ticker].get("longBusinessSummary", "") or ""

    # Sector / industry lookup for the filter panel. fundamentals_cache.json
    # is the source of truth; tickers without an entry get the "Unknown"
    # bucket so they remain togglable in the filter. Industry is exposed
    # separately so the panel can add narrower filters (e.g. Biotech) that
    # cut across sectors.
    sector = "Unknown"
    industry = "Unknown"
    if fundamentals and ticker in fundamentals:
        sec = fundamentals[ticker].get("sector")
        if sec:
            sector = sec
        ind = fundamentals[ticker].get("industry")
        if ind:
            industry = ind

    momo = is_momo(df)   # >=30% low-to-high run over the last ~50 bars

    return dict(
        ticker=ticker,
        long_name=long_name,
        long_summary=long_summary,
        dates=dates,
        open=_round_list(open_v, 4),
        high=_round_list(high_v, 4),
        low=_round_list(low_v, 4),
        close=_round_list(close_v, 4),
        volume=_round_list(vol_v, 0),
        rs0d=rs0d, rs1=rs1, rs3=rs3, rs5=rs5, rs20=rs20, rs65=rs65, rs130=rs130,
        comp3=comp3, comp5=comp5, comp10=comp10, comp20=comp20, comp30=comp30,
        pct_200=pct_200,
        pos_label=pos_label,
        below_200=below_200,
        last_close=last_close,
        day_chg=day_chg, day_chg_pct=day_chg_pct,
        vol_last=vol_last,
        adr=adr20, five_d_return=five_d,
        today_adr_ratio=today_adr_ratio,
        ext50=ext50,
        sector=sector,
        industry=industry,
        momo=momo,
    )


# ════════════════════════════════════════════════════════════
# COMPOSITE CHART (Plotly)
# ════════════════════════════════════════════════════════════

def build_composite_figure(composite_df, theme_label, used_tickers, divergences=None, narrative=None):
    df = composite_df
    close = df["close"].values.reshape(1, -1).astype(np.float64)
    high = df["high"].values.reshape(1, -1).astype(np.float64)
    low = df["low"].values.reshape(1, -1).astype(np.float64)
    openv = df["open"].values.astype(np.float64)
    closev = df["close"].values.astype(np.float64)

    sma5 = sma_2d(close, 5)[0]
    sma10 = sma_2d(close, 10)[0]
    sma20 = sma_2d(close, 20)[0]
    sma50 = sma_2d(close, 50)[0]
    sma200 = sma_2d(close, 200)[0]

    macd_line = macd_2d(close, 6, 20)[0]
    signal_line = ema_2d(macd_line.reshape(1, -1).astype(np.float64), 9)[0]

    atr14 = atr_2d(high, low, close, 14)[0]

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.62, 0.13, 0.25],
        vertical_spacing=0.012,
    )

    dates = pd.to_datetime(df["date"])

    fig.add_trace(go.Candlestick(
        x=dates,
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLOR_UP, width=1), fillcolor=COLOR_UP),
        decreasing=dict(line=dict(color=COLOR_DOWN, width=1), fillcolor=COLOR_DOWN),
        showlegend=False, name="",
        hoverlabel=dict(font=dict(family="Consolas, monospace", size=11)),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=dates, y=sma5, mode="lines",
                             line=dict(color="#ff8800", width=1.2),
                             name="SMA 5", showlegend=False, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=sma10, mode="lines",
                             line=dict(color="#5fc8ff", width=1.2),
                             name="SMA 10", showlegend=False, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=sma20, mode="lines",
                             line=dict(color="#e8c890", width=1.2),
                             name="SMA 20", showlegend=False, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=sma50, mode="lines",
                             line=dict(color="#ffcc00", width=1.2),
                             name="SMA 50", showlegend=False, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=sma200, mode="lines",
                             line=dict(color="#ffffff", width=1.5),
                             name="SMA 200", showlegend=False, hoverinfo="skip"), row=1, col=1)

    vol_colors = [COLOR_UP if c >= o else COLOR_DOWN for o, c in zip(openv, closev)]
    fig.add_trace(go.Bar(x=dates, y=df["volume"], marker=dict(color=vol_colors, line=dict(width=0)),
                          showlegend=False, name="", hoverinfo="skip"), row=2, col=1)

    fig.add_trace(go.Scatter(x=dates, y=macd_line, mode="lines",
                             line=dict(color=COLOR_CYAN, width=1.6),
                             showlegend=False, name="MACD", hoverinfo="skip"), row=3, col=1)
    fig.add_trace(go.Scatter(x=dates, y=signal_line, mode="lines",
                             line=dict(color=COLOR_ORANGE, width=1.6),
                             showlegend=False, name="Signal", hoverinfo="skip"), row=3, col=1)
    fig.add_hline(y=0, line=dict(color=COLOR_BORDER_STRONG, width=0.6), row=3, col=1)

    # ── TC2000-style canvas annotations (top-left of candle panel) ──
    fig.add_annotation(
        text=f"<b>{theme_label}, D</b>",
        xref="paper", yref="paper",
        x=0.008, y=0.98, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(family="Segoe UI, Tahoma, sans-serif", size=34, color=COLOR_TEXT),
    )
    fig.add_annotation(
        text=f"<b>{len(used_tickers)} symbols · equal weight</b>",
        xref="paper", yref="paper",
        x=0.008, y=0.90, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(family="Segoe UI, Tahoma, sans-serif", size=14, color=COLOR_TEXT),
    )
    member_str = ", ".join(used_tickers)
    wrapped = textwrap.fill(member_str, width=32)
    member_line_count = wrapped.count("\n") + 1
    fig.add_annotation(
        text=wrapped.replace("\n", "<br>"),
        xref="paper", yref="paper",
        x=0.008, y=0.85, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(family="Segoe UI, Tahoma, sans-serif", size=11, color=COLOR_TEXT_DIM),
        align="left",
    )

    # ── Optional 4th line: theme narrative beneath the member list ──
    if narrative:
        # Place narrative below member list — each wrapped member-list line ≈ 0.025 y-units
        narrative_y = 0.85 - (member_line_count * 0.025) - 0.025
        # Clamp so a very long member list does not push the narrative below the candle panel
        if narrative_y < 0.45:
            narrative_y = 0.45
        wrapped_narrative = textwrap.fill(narrative, width=58)
        fig.add_annotation(
            text=wrapped_narrative.replace("\n", "<br>"),
            xref="paper", yref="paper",
            x=0.008, y=narrative_y, xanchor="left", yanchor="top",
            showarrow=False,
            font=dict(family="Segoe UI, Tahoma, sans-serif", size=11, color=COLOR_GOLD),
            align="left",
        )

    # Small panel labels in top-left of volume / MACD panels (TC2000 style)
    fig.add_annotation(
        text="<b>Volume</b>", xref="paper", yref="paper",
        x=0.008, y=0.36, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(family="Consolas, monospace", size=10, color=COLOR_TEXT_DIM),
    )
    fig.add_annotation(
        text="<b>MACD (6, 20, 9)</b>", xref="paper", yref="paper",
        x=0.008, y=0.22, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(family="Consolas, monospace", size=10, color=COLOR_TEXT_DIM),
    )

    spike_args = dict(showspikes=True, spikecolor=COLOR_CYAN, spikethickness=1,
                      spikemode="across", spikesnap="cursor", spikedash="dot")

    # Skip weekends + common US market holidays so candles render contiguously
    # like TC2000 (calendar-day x-axis would leave Sat/Sun as blank gaps).
    rangebreaks = [dict(bounds=["sat", "mon"])]

    # 20 trading-day right-side padding so candles don't run to the edge
    first_date = pd.to_datetime(df["date"].iloc[0])
    last_date = pd.to_datetime(df["date"].iloc[-1])
    right_pad = last_date + pd.Timedelta(days=30)
    xrange = [first_date, right_pad]

    fig.update_layout(
        paper_bgcolor=COLOR_BG, plot_bgcolor=COLOR_BG,
        font=dict(family="Consolas, monospace", size=11, color=COLOR_TEXT_DIM),
        margin=dict(l=10, r=58, t=2, b=18),
        height=640,
        showlegend=False,
        hovermode="x unified",
        dragmode="pan",
        bargap=0.15,
        xaxis=dict(rangeslider=dict(visible=False), gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED, range=xrange, rangebreaks=rangebreaks, **spike_args),
        xaxis2=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED, range=xrange, rangebreaks=rangebreaks, **spike_args),
        xaxis3=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED, range=xrange, rangebreaks=rangebreaks, **spike_args),
        yaxis=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                   tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10)),
        yaxis2=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                    tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10),
                    showticklabels=True),
        yaxis3=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                    tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10)),
    )

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    chg = last["close"] - prev["close"]
    chg_pct = (last["close"] / prev["close"] - 1.0) * 100.0 if prev["close"] > 0 else 0.0
    aptr = atr14[-1] if not np.isnan(atr14[-1]) else 0.0
    vol_last = last["volume"]

    return fig, {
        "date": str(last["date"])[:10] if not pd.isna(last["date"]) else "",
        "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"],
        "chg": chg, "chg_pct": chg_pct,
        "vol": vol_last, "aptr": aptr,
        "sma5": float(sma5[-1]) if not np.isnan(sma5[-1]) else None,
        "sma10": float(sma10[-1]) if not np.isnan(sma10[-1]) else None,
        "sma20": float(sma20[-1]) if not np.isnan(sma20[-1]) else None,
        "sma50": float(sma50[-1]) if not np.isnan(sma50[-1]) else None,
        "sma200": float(sma200[-1]) if not np.isnan(sma200[-1]) else None,
    }


def build_ticker_layout_template():
    """Build a Plotly layout dict for the per-ticker chart: 4 panels stacked
    (candles + volume + MACD + ext50). JS clones this for every per-ticker
    chart so the layout / colors / spike-lines / axis formatting stay
    consistent across the dashboard. JS fills in the data traces, the
    ticker label annotation, the date range, and any trendline overlays.

    The ext50 panel (bottom) shows the 50-SMA-extension series — same
    indicator your TC2000 "X ADR to 50sma" panel does — with the
    Extension Peek descending trendlines drawn on top when the ticker
    has a snapshot.
    """
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        row_heights=[0.50, 0.10, 0.18, 0.22],
        vertical_spacing=0.010,
    )
    spike_args = dict(showspikes=True, spikecolor=COLOR_CYAN, spikethickness=1,
                      spikemode="across", spikesnap="cursor", spikedash="dot")
    rangebreaks = [dict(bounds=["sat", "mon"])]
    fig.update_layout(
        paper_bgcolor=COLOR_BG, plot_bgcolor=COLOR_BG,
        font=dict(family="Consolas, monospace", size=11, color=COLOR_TEXT_DIM),
        margin=dict(l=10, r=58, t=2, b=18),
        height=720,
        showlegend=False, hovermode="x unified", dragmode="pan", bargap=0.15,
        xaxis=dict(rangeslider=dict(visible=False), gridcolor=COLOR_GRID,
                   color=COLOR_TEXT_MUTED, rangebreaks=rangebreaks, **spike_args),
        xaxis2=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED,
                    rangebreaks=rangebreaks, **spike_args),
        xaxis3=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED,
                    rangebreaks=rangebreaks, **spike_args),
        xaxis4=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_MUTED,
                    rangebreaks=rangebreaks, **spike_args),
        yaxis=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                   tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10)),
        yaxis2=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                    tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10),
                    showticklabels=True),
        yaxis3=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                    tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10)),
        yaxis4=dict(gridcolor=COLOR_GRID, color=COLOR_TEXT_DIM, side="right",
                    tickfont=dict(color=COLOR_TEXT_DIM, family="Consolas, monospace", size=10),
                    zeroline=True, zerolinecolor="#555", zerolinewidth=1),
    )
    # Panel labels — y positions reflect the new 4-panel row heights.
    fig.add_annotation(text="<b>Volume</b>", xref="paper", yref="paper",
                       x=0.008, y=0.49, xanchor="left", yanchor="top",
                       showarrow=False,
                       font=dict(family="Consolas, monospace", size=10, color=COLOR_TEXT_DIM))
    fig.add_annotation(text="<b>MACD (6, 20, 9)</b>", xref="paper", yref="paper",
                       x=0.008, y=0.38, xanchor="left", yanchor="top",
                       showarrow=False,
                       font=dict(family="Consolas, monospace", size=10, color=COLOR_TEXT_DIM))
    fig.add_annotation(text="<b>X ADR to 50sma</b>", xref="paper", yref="paper",
                       x=0.008, y=0.20, xanchor="left", yanchor="top",
                       showarrow=False,
                       font=dict(family="Consolas, monospace", size=10, color=COLOR_TEXT_DIM))
    return fig.to_dict()["layout"]


# ════════════════════════════════════════════════════════════
# MEMBER MINI-CHART (hand-built SVG)
# ════════════════════════════════════════════════════════════

def build_mini_svg(df, ticker, n_bars=100, width=300, height=210, meta=None):
    """Render a per-ticker mini-chart SVG.

    When ``meta`` is a dict containing ``longName`` and/or ``longBusinessSummary``,
    the header grows to two lines (ticker on line 1, truncated longName on line 2)
    and an SVG ``<title>`` element is added so mouse-hover surfaces the first 1-2
    sentences of the business summary as a native browser tooltip. When ``meta`` is
    None or empty, the card falls back to the original single-line header layout.
    """
    if df is None or len(df) < 2:
        return f'<svg width="{width}" height="{height}" style="background:#000;display:block"></svg>'
    momo = is_momo(df)   # >=30% low-to-high run over the last ~50 bars (computed on full df, before tailing)
    # SMAs need full history — a 100-bar window alone can't seed SMA50/200 —
    # so compute on the full close series first, then tail to the visible window.
    full_close = df["close"].values.astype(np.float64)
    sma_specs = [(5, "#ff8800", 0.9), (10, "#5fc8ff", 0.9), (20, "#e8c890", 0.9),
                 (50, "#ffcc00", 0.9), (200, "#ffffff", 1.1)]
    full_smas = {p: sma_2d(full_close.reshape(1, -1), p)[0] for p, _, _ in sma_specs}
    df = df.tail(n_bars).reset_index(drop=True)
    if len(df) < 2:
        return f'<svg width="{width}" height="{height}" style="background:#000;display:block"></svg>'
    smas = {p: arr[-len(df):] for p, arr in full_smas.items()}

    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)

    last_close = c[-1]
    prev_close = c[-2]
    pct = (last_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    pct_color = COLOR_UP if pct >= 0 else COLOR_DOWN

    # ── Header sizing — grows when company-meta is available ──
    long_name = (meta or {}).get("longName") if isinstance(meta, dict) else None
    long_summary = (meta or {}).get("longBusinessSummary") if isinstance(meta, dict) else None
    has_meta_line = bool(long_name)
    header_h = 38 if has_meta_line else 24
    chart_top = header_h + 1
    chart_bot = height - 2
    chart_h = chart_bot - chart_top

    y_max = float(np.nanmax(h))
    y_min = float(np.nanmin(l))
    if y_max <= y_min:
        y_max = y_min + 1.0
    y_pad = (y_max - y_min) * 0.06
    y_max += y_pad
    y_min -= y_pad

    def y_px(v):
        return chart_top + (1 - (v - y_min) / (y_max - y_min)) * chart_h

    n = len(df)
    # Reserve ~18% of width on the right as empty padding (TC2000-style breathing room)
    chart_width = (width - 4) * 0.82
    bar_w = chart_width / n
    body_w = max(1.4, bar_w * 0.72)
    x_off = 2.0

    # SVG <title> child becomes the native browser hover tooltip for the entire card.
    tooltip_text = _xml_escape(_first_sentences(long_summary, n=2)) if long_summary else ""
    tooltip_el = f'<title>{tooltip_text}</title>' if tooltip_text else ''

    parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="background:#000;display:block">',
        tooltip_el,
        # Header strip — gray gradient (grows to 38px when longName is available)
        f'<defs><linearGradient id="g_{ticker}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{COLOR_CHROME_TOP}"/>'
        f'<stop offset="100%" stop-color="{COLOR_CHROME_BOT}"/>'
        f'</linearGradient>'
        f'<clipPath id="clip_{ticker}"><rect x="0" y="{chart_top:.2f}" width="{width}" height="{chart_h:.2f}"/></clipPath>'
        f'</defs>',
        f'<rect x="0" y="0" width="{width}" height="{header_h}" fill="url(#g_{ticker})"/>',
        f'<line x1="0" y1="{header_h-0.5}" x2="{width}" y2="{header_h-0.5}" stroke="#000" stroke-width="1"/>',
        # Ticker (left, line 1)
        f'<text x="8" y="16" font-family="Segoe UI,Tahoma,sans-serif" font-size="12" '
        f'font-weight="700" fill="#ffffff" letter-spacing="0.04em" style="text-shadow:0 1px 0 rgba(0,0,0,0.6)">{ticker}</text>',
        # Price + chg% (right, line 1)
        f'<text x="{width-8}" y="16" text-anchor="end" font-family="Consolas,monospace" '
        f'font-size="11" fill="{COLOR_TEXT}" style="text-shadow:0 1px 0 rgba(0,0,0,0.6)">'
        f'<tspan>{last_close:.2f}</tspan>'
        f'<tspan> </tspan>'
        f'<tspan fill="{pct_color}" font-weight="700">{pct:+.1f}%</tspan></text>',
    ]

    # ── Momo badge — a little green circle by the ticker when the stock had a
    # 30%+ low-to-high run in the last 50 bars. <title> gives a native tooltip. ──
    if momo:
        momo_x = min(width - 72, 8.0 + len(ticker) * 8.0 + 8.0)
        parts.append(
            f'<circle cx="{momo_x:.1f}" cy="11" r="3.6" fill="#27e83a" stroke="#0a0a0a" stroke-width="0.7">'
            f'<title>Momo · 30%+ low→high in 50d</title></circle>'
        )

    # ── Optional second header line — truncated longName when meta is present ──
    if has_meta_line:
        truncated_name = _xml_escape(_truncate(long_name, 38))
        parts.append(
            f'<text x="8" y="32" font-family="Segoe UI,Tahoma,sans-serif" font-size="10" '
            f'font-weight="400" fill="{COLOR_TEXT_DIM}" letter-spacing="0.02em" '
            f'style="text-shadow:0 1px 0 rgba(0,0,0,0.5)">{truncated_name}</text>'
        )

    for i in range(n):
        cx = x_off + bar_w * i + bar_w / 2
        col = COLOR_UP if c[i] >= o[i] else COLOR_DOWN
        # Wick
        parts.append(f'<line x1="{cx:.2f}" y1="{y_px(h[i]):.2f}" x2="{cx:.2f}" y2="{y_px(l[i]):.2f}" stroke="{col}" stroke-width="1"/>')
        # Body
        bt = y_px(max(o[i], c[i]))
        bb = y_px(min(o[i], c[i]))
        bh = max(1.0, bb - bt)
        parts.append(f'<rect x="{cx - body_w/2:.2f}" y="{bt:.2f}" width="{body_w:.2f}" height="{bh:.2f}" fill="{col}"/>')

    # ── SMA overlay — same palette as the composite chart, drawn over the candles ──
    # Clipped to the chart rect: a long MA that runs off the bottom in a strong
    # trend simply disappears; the candles keep their full price-based height.
    parts.append(f'<g clip-path="url(#clip_{ticker})" fill="none">')
    for p, color, lw in sma_specs:
        vals = smas[p]
        seg = []
        for i in range(n):
            v = vals[i]
            if np.isnan(v):
                if len(seg) >= 2:
                    pts = " ".join(seg)
                    parts.append(f'<polyline points="{pts}" stroke="{color}" stroke-width="{lw}" stroke-linejoin="round"/>')
                seg = []
                continue
            cx = x_off + bar_w * i + bar_w / 2
            seg.append(f'{cx:.2f},{y_px(v):.2f}')
        if len(seg) >= 2:
            pts = " ".join(seg)
            parts.append(f'<polyline points="{pts}" stroke="{color}" stroke-width="{lw}" stroke-linejoin="round"/>')
    parts.append('</g>')

    # ── Per-ticker flag — top-right of the chart, in the empty right padding so it
    #    scales with the thumbnail and never covers the header price/%. The <g>
    #    carries data-flag-ticker; a delegated click handler toggles it, and the
    #    is-tflagged class (set in JS) fills it magenta. Transparent rect = hit area.
    fx = width - 21.0
    fy = header_h + 5.0
    parts.append(
        f'<g class="tflag-icon" data-flag-ticker="{ticker}" style="cursor:pointer">'
        f'<title>Flag / unflag {ticker}</title>'
        f'<rect x="{fx-4:.1f}" y="{fy-4:.1f}" width="22" height="20" fill="transparent"/>'
        f'<polygon points="{fx:.1f},{fy:.1f} {fx+11:.1f},{fy+3.5:.1f} {fx:.1f},{fy+7:.1f}"/>'
        f'</g>'
    )

    parts.append("</svg>")
    return "".join(parts)


# ════════════════════════════════════════════════════════════
# HTML ASSEMBLY
# ════════════════════════════════════════════════════════════

CSS = """
:root {
  --bg-canvas: #000000; --bg-panel: #000000;
  --bg-elevated: #6a6f78; --bg-overlay: #4a4f58;
  --bg-row-hover: #0d0d0f; --bg-row-selected: #1c2030;
  --bg-title-grad: linear-gradient(180deg, #6e737d 0%, #535860 100%);
  --border-faint: #1a1a1c; --border: #000000;
  --border-soft: #2a2c30; --border-strong: #7a8088;
  --fg-primary: #ffffff; --fg-secondary: #c8ccd2; --fg-tertiary: #888c92;
  --accent: #ffcc00; --accent-info: #5fc8ff;
  --up: #1eff1e; --down: #ff3030; --warn: #ffcc00; --info: #5fc8ff;
  --font-sans: 'Segoe UI', Tahoma, 'MS Sans Serif', Verdana, sans-serif;
  --font-mono: 'Consolas', 'Lucida Console', 'JetBrains Mono', monospace;
}
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg-canvas); color: var(--fg-primary);
  font-family: var(--font-sans); font-size: 12px; line-height: 1.35;
  -webkit-font-smoothing: antialiased;
}
.app { display: flex; flex-direction: column; min-height: 100vh; }

/* ---------- top chrome ---------- */
.rm-fn-bar {
  display: flex; align-items: stretch; min-height: 48px;
  background: var(--bg-title-grad);
  border-top: 1px solid rgba(255,255,255,0.12);
  border-bottom: 1px solid #000;
  position: sticky; top: 0; z-index: 50;
}
.rm-fn-bar > * {
  display: flex; flex-direction: column; justify-content: center;
  gap: 2px; padding: 6px 14px;
  border-right: 1px solid #000;
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.12);
  min-width: 0; white-space: nowrap;
}
.rm-fn-bar > *:last-child { border-right: 0; box-shadow: none; }

.rm-fn-brand {
  flex-direction: row !important;
  align-items: center;
  gap: 10px;
  padding-left: 14px; padding-right: 16px;
}
.rm-fn-title {
  font-size: 13px; font-weight: 700; letter-spacing: 0.05em;
  color: var(--fg-primary); text-shadow: 0 1px 0 rgba(0,0,0,0.6);
  text-transform: uppercase;
}

.rm-label {
  font-size: 9px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; color: #ffffff;
  text-shadow: 0 1px 0 rgba(0,0,0,0.5);
}
.rm-val {
  font-size: 15px; font-weight: 700; letter-spacing: 0.01em;
  color: #ffffff; font-family: var(--font-mono);
  text-shadow: 0 1px 0 rgba(0,0,0,0.6);
}
.rm-val.accent { color: var(--accent-info); }
.rm-val.up { color: var(--up); }
.rm-val.down { color: var(--down); }
.rm-val.intraday { color: var(--accent); }
.intraday-marker {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent);
  margin-left: 4px;
  letter-spacing: 0.02em;
}
.rm-h-sub { font-size: 10px; color: #e0e4ea; letter-spacing: 0.02em; text-shadow: 0 1px 0 rgba(0,0,0,0.4); }
.rm-fn-grow { flex: 1; }

.rm-status-dot {
  display: inline-block; width: 7px; height: 7px;
  background: var(--up); margin-right: 8px;
  box-shadow: 0 0 4px rgba(30,255,30,0.6);
}

/* ---------- layout: sidebar + main ---------- */
.body-grid {
  display: grid;
  /* Sidebar holds the watchlist table — sized to fit all columns
     (Flag + Theme/Ticker + 0D/1d/5d/20d/65d/130d + Comp + N). */
  grid-template-columns: 600px 1fr;
  flex: 1; min-height: 0;
}
aside.sidebar {
  background: #000;
  border-right: 1px solid var(--border-faint);
  padding: 10px 0;
  position: sticky; top: 48px;
  align-self: start;
  max-height: calc(100vh - 48px - 22px);
  overflow-y: auto;
}
.sidebar-link {
  display: block;
  font-family: var(--font-sans);
  font-size: 11px; font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--fg-secondary);
  padding: 6px 14px;
  text-decoration: none;
  border-left: 2px solid transparent;
  transition: color 80ms ease-out, border-left-color 80ms ease-out, background 80ms ease-out;
  cursor: pointer;
}
.sidebar-link:hover {
  color: var(--up);
  background: var(--bg-row-hover);
  border-left-color: var(--up);
}
.sidebar-link.is-active {
  color: var(--accent);
  background: var(--bg-row-selected);
  border-left-color: var(--accent);
}
.sidebar-link .count {
  color: var(--fg-tertiary);
  font-family: var(--font-mono);
  margin-left: 4px;
  font-size: 10px;
}

main {
  background: #000;
  padding: 0 0 24px 0;
  min-width: 0;
}

/* ---------- theme section ---------- */
section.theme {
  border-bottom: 2px solid var(--border-soft);
  padding: 0;
  margin: 0;
  display: none;
}
section.theme.is-active { display: block; }

/* TC2000-style chart info bar — pure black, monospace, dense */
.chart-info-bar {
  display: flex; align-items: center; gap: 0;
  background: #000;
  padding: 5px 12px 5px 12px;
  border-bottom: 1px solid var(--border-faint);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'tnum', 'zero';
  font-size: 11.5px;
  color: var(--fg-primary);
  white-space: nowrap;
  overflow-x: auto;
}
.chart-info-bar .cluster { display: inline-flex; align-items: center; gap: 4px; }
.chart-info-bar .date { color: var(--fg-tertiary); margin-right: 8px; }
.chart-info-bar .lbl { color: var(--fg-tertiary); margin-left: 6px; margin-right: 2px; font-weight: 600; }
.chart-info-bar .lbl:first-child { margin-left: 0; }
.chart-info-bar .val { color: var(--fg-primary); font-weight: 600; }
.chart-info-bar .up { color: var(--up); font-weight: 700; }
.chart-info-bar .down { color: var(--down); font-weight: 700; }
.chart-info-bar .sep { color: var(--border-strong); margin: 0 10px; }
.chart-info-bar .sma-5  { color: #ff8800; margin-left: 4px; font-weight: 600; }
.chart-info-bar .sma-10 { color: #5fc8ff; margin-left: 10px; font-weight: 600; }
.chart-info-bar .sma-20 { color: #e8c890; margin-left: 10px; font-weight: 600; }
.chart-info-bar .sma-50 { color: #ffcc00; margin-left: 10px; font-weight: 600; }
.chart-info-bar .sma-200 { color: #ffffff; margin-left: 10px; font-weight: 600; }

/* ---------- vs-200-day cell + sidebar chip ---------- */
.pos200-cluster {
  padding: 2px 8px;
  border: 1px solid currentColor;
  margin-right: 4px;
}
.pos200-cluster .lbl { color: inherit !important; opacity: 0.75; }
.pos200-cluster .pos200-pct { font-weight: 700; font-size: 12px; margin-left: 4px; font-family: var(--font-mono); }
.pos200-cluster .pos200-label { margin-left: 8px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; font-size: 11px; }

.pos-chip {
  display: inline-block;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  margin-left: 8px;
  border: 1px solid currentColor;
}

.pos-above { color: var(--up); }
.pos-below { color: var(--down); }
.pos-na    { color: var(--fg-tertiary); }

/* RS-vs-Universe cell in info bar */
.rs-cluster {
  padding: 2px 8px;
  border: 1px solid currentColor;
  margin-right: 4px;
}
.rs-cluster .lbl { color: inherit !important; opacity: 0.75; }
.rs-cluster .rs-val { font-weight: 700; font-size: 12px; margin-left: 4px; font-family: var(--font-mono); }
.rs-cluster .rs-theme { font-weight: 600; font-size: 11px; margin-left: 4px; font-family: var(--font-mono); color: var(--fg-secondary); }
.rs-up   { color: var(--up); }
.rs-down { color: var(--down); }

/* RS chip in sidebar */
.rs-chip {
  display: inline-block;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  margin-left: 8px;
  border: 1px solid currentColor;
}

/* Bold highlight for sidebar links whose composite is below its 200-day SMA */
.sidebar-link.sidebar-below200 {
  color: var(--down);
  border-left-color: var(--down);
  background: rgba(255, 48, 48, 0.08);
}
.sidebar-link.sidebar-below200:hover {
  color: var(--down);
  background: rgba(255, 48, 48, 0.18);
  border-left-color: var(--down);
}
.sidebar-link.sidebar-below200.is-active {
  background: rgba(255, 48, 48, 0.28);
  color: #ffffff;
  border-left-color: var(--down);
}

/* MACD divergence styles (div-tag / sidebar-bull-under-200 / sidebar-div-chip)
   were removed 2026-05-22 along with the divergence feature itself. */
.chart-info-bar .spacer { flex: 1; }
.chart-info-bar .muted-meta { color: var(--fg-tertiary); }
.chart-info-bar .muted-meta .val { color: var(--fg-secondary); }

.composite-chart { background: #000; padding: 0; }
.composite-chart .plotly-graph-div { background: #000 !important; }

.theme-foot {
  display: flex; align-items: center;
  padding: 4px 12px;
  background: #050507;
  border-top: 1px solid var(--border-faint);
  font-family: var(--font-sans);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--fg-secondary);
}
.theme-foot-name { font-weight: 600; color: #ffffff; }
.theme-foot-meta { margin-left: auto; font-family: var(--font-mono); color: var(--fg-tertiary); }

.ungrouped-bar { padding: 10px 12px; }
.ungrouped-bar .val { color: var(--warn); }
.sidebar-ungrouped { color: var(--warn) !important; border-left: 2px solid var(--warn) !important; }
.sidebar-ungrouped:hover { background: var(--bg-row-hover); }

.member-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0;
  background: #000;
  border-top: 1px solid var(--border-faint);
}
.member-card {
  border-right: 1px solid var(--border-faint);
  border-bottom: 1px solid var(--border-faint);
  background: #000;
  overflow: hidden;
  position: relative;
}
.member-card svg { width: 100%; height: auto; display: block; }
/* Per-ticker flag — independent of the gold theme flag. Magenta when set, and it
   shows on every place that ticker appears (thumbnails, Tickers rows, Setups rows). */
.tflag-icon { cursor: pointer; }
.tflag-icon polygon { fill: none; stroke: #9aa0a6; stroke-width: 1.3; }
.tflag-icon.is-tflagged polygon { fill: #ff3fa0; stroke: #ff3fa0; }
/* Card flag is a <g> inside the thumbnail SVG (scales with the chart). */
.member-card .tflag-icon { opacity: 0.45; }
.member-card:hover .tflag-icon, .member-card .tflag-icon.is-tflagged { opacity: 1; }
.member-card .tflag-icon:hover polygon { stroke: #ff7fc4; }
/* Row flag is a standalone inline SVG in the ticker-symbol cell. */
.tflag-row {
  width: 11px; height: 11px; margin-right: 5px; vertical-align: -1px;
  opacity: 0.4; flex-shrink: 0;
}
.tflag-row:hover { opacity: 0.85; }
.tflag-row.is-tflagged { opacity: 1; }
.ticker-symbol-cell { white-space: nowrap; }

/* ---------- status bar ---------- */
.rm-statusbar {
  height: 22px;
  background: var(--bg-title-grad);
  border-top: 1px solid #000;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
  display: flex; align-items: center; gap: 14px;
  padding: 0 12px;
  font-size: 11px;
  font-family: var(--font-sans);
  color: #ffffff;
  letter-spacing: 0.02em;
  position: sticky; bottom: 0; z-index: 40;
  text-shadow: 0 1px 0 rgba(0,0,0,0.5);
}
.rm-statusbar .mono { font-family: var(--font-mono); }

/* ─────────────────── WATCHLIST TABLE (sidebar) ─────────────────── */
.body-grid { grid-template-columns: 600px 1fr; }   /* widen sidebar — needs to fit Flag/Theme + 0D/1d/5d/20d/65d/130d/Comp/N */

.watchlist-controls {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; row-gap: 4px;
  padding: 6px 10px;
  background: #060608;
  border-bottom: 1px solid var(--border-faint);
  font-family: var(--font-sans); font-size: 11px;
  color: var(--fg-secondary);
}
.watchlist-controls label { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; user-select: none; }
.watchlist-controls input[type="checkbox"] { accent-color: var(--accent); }
.watchlist-controls .wl-count { margin-left: auto; color: var(--fg-tertiary); font-family: var(--font-mono); }
/* Rotation-quadrant filter (Chart-view theme rows) — labels colored to match
   the RRG quadrants, doubling as a legend. */
.watchlist-controls .wl-quad-group { display: inline-flex; align-items: center; gap: 8px; }
.watchlist-controls .wl-quad-lbl.quad-improving { color: #5fc8ff; }
.watchlist-controls .wl-quad-lbl.quad-leading   { color: #1eff1e; }
.watchlist-controls .wl-quad-lbl.quad-weakening { color: #ffcc00; }
.watchlist-controls .wl-quad-lbl.quad-lagging   { color: #ff3030; }

.watchlist-table {
  width: 100%; border-collapse: collapse;
  font-family: var(--font-sans); font-size: 11px;
  color: var(--fg-primary);
  background: #000;
}
.watchlist-table th {
  background: var(--bg-title-grad);
  color: #ffffff;
  font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 5px 8px;
  text-align: right;
  border-right: 1px solid #000;
  cursor: pointer; user-select: none;
  position: sticky; top: 0; z-index: 5;
  text-shadow: 0 1px 0 rgba(0,0,0,0.6);
}
.watchlist-table th:first-child { text-align: left; }
.watchlist-table th:last-child { border-right: 0; }
.watchlist-table th:hover { background: var(--bg-overlay); }
.watchlist-table th.sort-active::after {
  content: " ▾"; opacity: 0.85;
}
.watchlist-table th.sort-active.sort-asc::after {
  content: " ▴"; opacity: 0.85;
}

.watchlist-table td {
  padding: 4px 8px; text-align: right;
  border-bottom: 1px solid var(--border-faint);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.watchlist-table td.theme-name {
  font-family: var(--font-sans);
  font-weight: 500;
  text-align: left;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 11px;
  color: var(--fg-secondary);
}
.watchlist-table tr { cursor: pointer; }
.watchlist-table tr:hover { background: var(--bg-row-hover); }
.watchlist-table tr.is-active { background: var(--bg-row-selected); }
.watchlist-table tr.is-active td.theme-name { color: var(--accent); font-weight: 700; }
.watchlist-table tbody tr.below-200 td.theme-name { color: var(--down); }
/* Theme-name colored by its current RRG quadrant (set by JS from ROTATION_DATA).
   Sits on the .theme-label span so it overrides the below-200 red; the active
   row keeps its bold + selected background. */
.watchlist-table td.theme-name .theme-label.quad-leading   { color: #1eff1e; }
.watchlist-table td.theme-name .theme-label.quad-improving { color: #5fc8ff; }
.watchlist-table td.theme-name .theme-label.quad-weakening { color: #ffcc00; }
.watchlist-table td.theme-name .theme-label.quad-lagging   { color: #ff3030; }

.watchlist-table .pos { color: var(--up); }
.watchlist-table .neg { color: var(--down); }
.watchlist-table .nul { color: var(--fg-tertiary); }
.watchlist-table .div-cell .bull { color: var(--up); font-weight: 700; }
.watchlist-table .div-cell .bear { color: var(--down); font-weight: 700; }

.watchlist-table tbody.hide-below tr.below-200 { display: none; }

/* Number-column sizing for the wider sidebar (~600px). All 8 num
   columns (0D / 1d / 5d / 20d / 65d / 130d / Comp / N) get a
   comfortable fixed width without crowding. */
.watchlist-table th { padding: 4px 4px; font-size: 10px; }
.watchlist-table td { padding: 3px 4px; }
.watchlist-table th[data-sort-type="num"],
.watchlist-table td.num {
  font-size: 10px;
  min-width: 42px;
  text-align: right;
  padding-right: 6px;
}
.watchlist-table th[data-sort-key="label"],
.watchlist-table th[data-sort-key="theme-label"],
.watchlist-table td.theme-name,
.watchlist-table td.ticker-symbol-cell,
.watchlist-table td.theme-membership-cell { text-align: left; }

/* Flag column (leftmost). Theme rows get a clickable flag SVG;
   ticker child rows leave the cell empty (flags live at theme level). */
.watchlist-table th.flag-col,
.watchlist-table td.flag-cell {
  width: 18px; min-width: 18px; max-width: 18px;
  padding: 0; text-align: center;
}
.watchlist-table td.flag-cell .flag-icon {
  display: inline-block;
  width: 12px; height: 12px;
  cursor: pointer;
  vertical-align: middle;
  opacity: 0.55;
  transition: opacity 0.08s ease;
}
.watchlist-table td.flag-cell .flag-icon:hover { opacity: 1.0; }
.watchlist-table td.flag-cell .flag-icon polygon {
  fill: none;
  stroke: var(--fg-tertiary, #888);
  stroke-width: 1.5;
}
.watchlist-table td.flag-cell .flag-icon.is-flagged {
  opacity: 1.0;
}
.watchlist-table td.flag-cell .flag-icon.is-flagged polygon {
  fill: var(--accent, #5fc8ff);
  stroke: var(--accent, #5fc8ff);
}
/* Header flag column — non-interactive icon */
.watchlist-table th.flag-col .flag-icon-static {
  display: inline-block; width: 10px; height: 10px;
  vertical-align: middle; opacity: 0.5;
}
.watchlist-table th.flag-col .flag-icon-static polygon {
  fill: none; stroke: var(--fg-tertiary, #888); stroke-width: 1.5;
}

/* Right-click context menu for flag operations */
.flag-context-menu {
  position: fixed;
  z-index: 9999;
  background: var(--bg-secondary, #0a0a0c);
  border: 1px solid var(--border-soft, #2a2a2c);
  font-family: var(--font-chrome, Segoe UI, sans-serif);
  font-size: 11px;
  color: var(--fg-primary);
  min-width: 130px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.6);
}
.flag-context-menu-item {
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
}
.flag-context-menu-item:hover {
  background: var(--bg-row-hover, #1a1a1c);
}

/* Tree-view expansion: theme caret + ticker child rows */
.watchlist-table tr.theme-row td.theme-name {
  display: flex; align-items: center; gap: 4px;
}
.watchlist-table tr.theme-row .tree-caret {
  display: inline-block;
  width: 12px;
  color: var(--fg-tertiary);
  font-size: 10px;
  transition: transform 0.12s ease;
  user-select: none;
}
.watchlist-table tr.theme-row[data-expanded="1"] .tree-caret {
  color: var(--accent);
  transform: rotate(90deg);
}
.watchlist-table tr.theme-row[data-expanded="1"] .tree-caret::before {
  content: "▸";
}
.watchlist-table tr.theme-row .theme-label { flex: 1; }
.watchlist-table tr.ticker-row td.theme-name {
  display: flex; align-items: center; gap: 4px;
  padding-left: 6px;
}
.watchlist-table tr.ticker-row .tree-indent {
  display: inline-block;
  width: 16px;
  border-left: 1px solid var(--border-soft);
  align-self: stretch;
}
.watchlist-table tr.ticker-row .tree-bullet {
  color: var(--fg-tertiary);
  font-size: 9px;
}
.watchlist-table tr.ticker-row .ticker-symbol {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--fg-primary);
}
.watchlist-table tr.ticker-row.below-200 .ticker-symbol { color: var(--down); }
.watchlist-table tr.ticker-row.is-active .ticker-symbol { color: var(--accent); font-weight: 700; }
.watchlist-table tr.ticker-row.child-collapsed { display: none; }
.watchlist-table tr.ungrouped-row .theme-label { color: var(--accent-gold, #ffcc00); }

/* Tickers-view flat table — all rows visible by default; JS applyFilter()
   sets inline `display: none` when Tight D1 / Hot / hide-below filters fire. */
.tickers-table tbody.hide-below tr.tickers-row.below-200 { display: none; }
.tickers-table td.ticker-symbol-cell { padding-left: 10px; }
.tickers-table td.ticker-symbol-cell .ticker-symbol {
  font-family: var(--font-mono);
  font-weight: 700;
  color: var(--fg-primary);
}
.tickers-table tr.tickers-row.below-200 .ticker-symbol { color: var(--down); }
.tickers-table tr.tickers-row.is-active { background: var(--bg-row-selected); }
.tickers-table tr.tickers-row.is-active .ticker-symbol { color: var(--accent); font-weight: 700; }
.tickers-table td.theme-membership-cell {
  font-family: var(--font-chrome);
  font-size: 11px;
  color: var(--fg-secondary);
  max-width: 130px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tickers-table tr.ungrouped-ticker td.theme-membership-cell { color: var(--accent-gold, #ffcc00); }
.tickers-table td.num.adr { color: var(--fg-primary); font-weight: 600; }
.tickers-table td.nul { color: var(--fg-tertiary); }

.tickers-empty {
  padding: 20px 12px;
  font-size: 12px;
  color: var(--fg-tertiary);
  text-align: center;
  font-style: italic;
  display: none;
}

/* Generated cell → refresh button (native-window mode only) */
.rm-refresh-cell {
  position: relative;
  cursor: pointer;
  user-select: none;
}
.rm-refresh-cell:hover { background: var(--bg-overlay, rgba(255,255,255,0.05)); }
.rm-refresh-cell.is-active { background: var(--bg-row-selected, rgba(255,255,255,0.10)); }
.rm-refresh-cell.is-refreshing { color: var(--accent); cursor: progress; }
.rm-refresh-cell.is-disabled {
  cursor: default;
}
.rm-refresh-cell.is-disabled:hover { background: transparent; }
.rm-refresh-spinner {
  display: none;
  margin-left: 6px;
  width: 10px; height: 10px;
  border: 1.5px solid rgba(255,204,0,0.25);
  border-top-color: var(--accent-gold, #ffcc00);
  border-radius: 50%;
  animation: rm-spin 0.7s linear infinite;
  vertical-align: middle;
}
.rm-refresh-cell.is-refreshing .rm-refresh-spinner { display: inline-block; }
.rm-refresh-cell.is-refreshing .rm-val { color: var(--accent-gold, #ffcc00); }
@keyframes rm-spin { to { transform: rotate(360deg); } }

.rm-refresh-toast {
  position: fixed;
  top: 50px; right: 20px;
  z-index: 2000;
  background: var(--bg-secondary, #0a0a0c);
  color: var(--down);
  border: 1px solid var(--down);
  padding: 8px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  display: none;
}
.rm-refresh-toast.is-visible { display: block; }

/* Filter icon button (lives in the watchlist controls bar) */
.filter-icon-btn {
  position: relative;
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 22px;
  margin-left: auto;
  padding: 0;
  background: transparent;
  border: 1px solid transparent;
  color: var(--fg-secondary);
  cursor: pointer;
}
.filter-icon-btn:hover {
  color: var(--accent);
  background: var(--bg-overlay, rgba(255,255,255,0.05));
  border-color: var(--border-soft);
}
.filter-icon-btn.is-open {
  color: var(--accent);
  background: var(--bg-row-selected, rgba(255,255,255,0.10));
  border-color: var(--accent);
}
.filter-icon-btn .filter-icon-dot {
  display: none;
  position: absolute;
  top: 2px; right: 2px;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--accent-gold, #ffcc00);
  box-shadow: 0 0 4px rgba(255,204,0,0.6);
}
.filter-icon-btn.has-exclusions .filter-icon-dot { display: block; }

.filter-panel {
  position: fixed;
  /* Anchored below the watchlist controls; left edge aligned with sidebar */
  top: 80px;
  left: 12px;
  width: 540px;
  max-height: 70vh;
  background: var(--bg-primary, #000);
  border: 1px solid var(--border-strong, #7a8088);
  box-shadow: 4px 4px 12px rgba(0,0,0,0.6);
  z-index: 1000;
  display: flex; flex-direction: column;
  font-family: var(--font-chrome);
  font-size: 12px;
}
.filter-panel-head {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 10px;
  background: linear-gradient(to bottom, #2a2c30, #1a1c20);
  border-bottom: 1px solid var(--border-strong);
}
.filter-panel-title {
  font-family: var(--font-chrome);
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--fg-primary);
}
.filter-reset-link, .filter-close-link, .filter-link {
  cursor: pointer;
  color: var(--accent);
  font-size: 11px;
  text-decoration: none;
}
.filter-reset-link:hover, .filter-close-link:hover, .filter-link:hover { text-decoration: underline; }
.filter-close-link { margin-left: auto; color: var(--fg-secondary); }
.filter-panel-body {
  display: flex; gap: 0;
  overflow: hidden;
  flex: 1;
}
.filter-section {
  flex: 1; min-width: 0;
  display: flex; flex-direction: column;
  border-right: 1px solid var(--border-soft);
}
.filter-section:last-child { border-right: 0; }
.filter-section-head {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px;
  background: var(--bg-secondary, #0a0a0c);
  border-bottom: 1px solid var(--border-soft);
}
.filter-section-title {
  flex: 1;
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--fg-secondary);
}
.filter-search {
  margin: 6px 10px;
  padding: 4px 8px;
  background: var(--bg-secondary, #0a0a0c);
  border: 1px solid var(--border-soft);
  color: var(--fg-primary);
  font-family: var(--font-mono);
  font-size: 11px;
}
.filter-search:focus {
  outline: 0;
  border-color: var(--accent);
}
.filter-section-list {
  flex: 1; overflow-y: auto;
  padding: 4px 0;
}
.filter-section-list label {
  display: flex; align-items: center; gap: 6px;
  padding: 3px 10px;
  cursor: pointer;
  user-select: none;
  font-size: 11px;
  color: var(--fg-primary);
}
.filter-section-list label:hover { background: var(--bg-row-hover); }
.filter-section-list label.hidden-by-search { display: none; }
.filter-section-list input[type="checkbox"] {
  accent-color: var(--accent, #5fc8ff);
}
.filter-section-list .filter-item-label {
  flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.filter-section-list .filter-item-sector {
  font-size: 10px;
  color: var(--fg-tertiary);
  font-family: var(--font-mono);
}

/* Header brand toggle (Themes/Tickers) */
.rm-fn-brand {
  cursor: pointer;
  user-select: none;
}
.rm-fn-brand:hover { background: var(--bg-overlay, rgba(255,255,255,0.05)); }
.rm-fn-brand:active { background: var(--bg-row-selected, rgba(255,255,255,0.10)); }
.rm-fn-brand .rm-fn-title { transition: color 0.12s ease; }
.rm-fn-brand .rm-status-dot { transition: background 0.12s ease; }
body.view-tickers .rm-fn-brand .rm-status-dot { background: var(--accent-gold, #ffcc00); }
body.view-setups  .rm-fn-brand .rm-status-dot { background: var(--accent, #4dd0ff); }
/* Three panes; only the active one shows. Use !important to override
   the inline style="display:none" baked into the markup. */
body.view-themes  .watchlist-pane.themes-pane  { display: block; }
body.view-themes  .watchlist-pane.tickers-pane { display: none !important; }
body.view-themes  .watchlist-pane.setups-pane  { display: none !important; }
body.view-tickers .watchlist-pane.themes-pane  { display: none; }
body.view-tickers .watchlist-pane.tickers-pane { display: block !important; }
body.view-tickers .watchlist-pane.setups-pane  { display: none !important; }
body.view-setups  .watchlist-pane.themes-pane  { display: none; }
body.view-setups  .watchlist-pane.tickers-pane { display: none !important; }
body.view-setups  .watchlist-pane.setups-pane  { display: block !important; }
body.view-candidates .watchlist-pane.themes-pane     { display: none; }
body.view-candidates .watchlist-pane.tickers-pane    { display: none !important; }
body.view-candidates .watchlist-pane.setups-pane     { display: none !important; }
body.view-candidates .watchlist-pane.candidates-pane { display: flex !important; flex-direction: column; height: 100%; }
body.view-candidates .rm-fn-brand .rm-status-dot     { background: #cc88ff; }
/* ── Themes sub-views: Chart / Heatmap / History ─────────── */
body.view-themes.tv-heatmap .body-grid { display: none; }
body.view-themes.tv-heatmap .heatmap-page { display: flex; }
.heatmap-page { display: none; flex: 1; min-height: 0; flex-direction: column; background: #000; }
.heatmap-controls {
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
  padding: 8px 14px; border-bottom: 1px solid var(--border-faint);
  background: var(--bg-title-grad);
}
.heatmap-controls .hm-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
  text-transform: uppercase; color: #fff; margin-right: 4px;
}
.hm-win-btn {
  font-family: var(--font-mono); font-size: 12px; font-weight: 700;
  color: var(--fg-secondary); background: #111;
  border: 1px solid var(--border-faint); padding: 4px 12px; cursor: pointer;
}
.hm-win-btn:hover { color: var(--up); }
.hm-win-btn.is-active { color: #000; background: var(--accent); border-color: var(--accent); }
.heatmap-grid {
  flex: 1; min-height: 0; overflow-y: auto;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
  gap: 6px; padding: 12px; align-content: start;
}
.hm-tile {
  display: flex; flex-direction: column; justify-content: center; gap: 4px;
  min-height: 86px; padding: 9px 11px; color: #fff;
  border: 1px solid rgba(255,255,255,0.10); cursor: pointer; overflow: hidden;
}
.hm-tile:hover { outline: 1px solid var(--accent); outline-offset: -1px; }
.hm-tile .hm-name {
  font-family: var(--font-sans); font-size: 12px; font-weight: 600;
  line-height: 1.18; max-height: 2.4em; overflow: hidden;
  text-shadow: 0 1px 2px rgba(0,0,0,0.7);
}
.hm-tile .hm-meta { display: flex; justify-content: space-between; align-items: baseline; }
.hm-tile .hm-rs {
  font-family: var(--font-mono); font-size: 16px; font-weight: 700;
  text-shadow: 0 1px 2px rgba(0,0,0,0.7);
}
.hm-tile .hm-n { font-family: var(--font-mono); font-size: 10px; opacity: 0.75; }
.hm-tile.is-null { background: #161618 !important; color: var(--fg-tertiary); }
/* expanded theme → member thumbnails */
.heatmap-page.is-expanded .heatmap-controls,
.heatmap-page.is-expanded .heatmap-grid { display: none; }
.heatmap-expand { display: none; flex: 1; min-height: 0; flex-direction: column; }
.heatmap-page.is-expanded .heatmap-expand { display: flex; }
.hm-expand-head {
  display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  padding: 8px 14px; border-bottom: 1px solid var(--border-faint);
  background: var(--bg-title-grad);
}
.hm-expand-title {
  font-family: var(--font-sans); font-size: 14px; font-weight: 700;
  color: var(--fg-primary); letter-spacing: 0.03em;
}
.hm-back-btn, .hm-viewchart-btn {
  font-family: var(--font-mono); font-size: 12px; font-weight: 700;
  color: var(--accent); background: #111;
  border: 1px solid var(--border-faint); padding: 4px 12px; cursor: pointer;
}
.hm-back-btn:hover, .hm-viewchart-btn:hover { color: #000; background: var(--accent); }
.hm-viewchart-btn { margin-left: auto; }
.hm-expand-body { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; }
.hm-expand-body .member-card { cursor: pointer; }
.hm-expand-body .member-card:hover { outline: 1px solid var(--accent); outline-offset: -1px; }
/* History view (RS lines of flagged themes) — lives in <main>, sidebar stays */
body.view-themes.tv-history main > section.theme,
body.view-themes.tv-history #__ticker_view__ { display: none !important; }
body.view-themes.tv-history #history-page { display: flex; }
.history-page { display: none; flex-direction: column; min-width: 0; background: #000; height: calc(100vh - 70px); }
.history-head {
  display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  padding: 8px 14px; border-bottom: 1px solid var(--border-faint);
  background: var(--bg-title-grad);
}
.history-head .history-title { font-family: var(--font-sans); font-size: 14px; font-weight: 700; color: var(--fg-primary); white-space: nowrap; }
.history-head .history-hint { font-family: var(--font-sans); font-size: 11px; color: var(--fg-tertiary); }
.hist-controls { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-left: 8px; }
.hist-grp { display: flex; align-items: center; }
.hist-grp .hist-lbl { font-size: 9px; font-weight: 700; text-transform: uppercase; color: #fff; letter-spacing: 0.04em; margin-right: 6px; }
.hist-btn {
  font-family: var(--font-mono); font-size: 11px; font-weight: 700;
  color: var(--fg-secondary); background: #111;
  border: 1px solid var(--border-faint); border-right: 0; padding: 3px 9px; cursor: pointer;
}
.hist-grp .hist-btn:last-of-type { border-right: 1px solid var(--border-faint); }
.hist-btn:hover { color: var(--up); }
.hist-btn.is-active { color: #000; background: var(--accent); border-color: var(--accent); }
.hist-smooth { width: 110px; accent-color: var(--accent); cursor: pointer; }
.hist-smooth-val { font-family: var(--font-mono); font-size: 11px; color: var(--accent); margin-left: 6px; min-width: 30px; }
.history-head .history-hint { margin-left: auto; }
.history-chart { flex: 1; min-height: 0; }
.history-empty {
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--fg-tertiary); font-family: var(--font-sans); font-size: 14px; text-align: center; padding: 0 24px;
}
/* View toggle in the header (replaces the old SORT cell) */
.rm-view-btns { display: flex; gap: 0; margin-top: 2px; }
.rm-view-btn {
  font-family: var(--font-mono); font-size: 11px; font-weight: 700;
  color: var(--fg-secondary); background: #111;
  border: 1px solid var(--border-faint); border-right: 0;
  padding: 2px 9px; cursor: pointer;
}
.rm-view-btn:last-child { border-right: 1px solid var(--border-faint); }
.rm-view-btn:hover { color: var(--up); }
.rm-view-btn.is-active { color: #000; background: var(--accent); border-color: var(--accent); }
/* ── Narrative Map view — full-width, sidebar hidden ─────────── */
body.view-themes.tv-map .body-grid { display: none; }
body.view-themes.tv-map .map-page { display: flex; }
.map-page { display: none; flex: 1; min-height: 0; flex-direction: column; background: #000; }
.map-controls {
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
  padding: 8px 14px; border-bottom: 1px solid var(--border-faint); background: var(--bg-title-grad);
}
.map-controls .map-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
  color: #fff; margin-right: 4px;
}
.map-win { display: flex; align-items: center; }
.map-win .map-win-lbl { font-size: 9px; font-weight: 700; text-transform: uppercase; color: #fff; letter-spacing: 0.04em; margin-right: 6px; }
.map-win-btn {
  font-family: var(--font-mono); font-size: 12px; font-weight: 700; color: var(--fg-secondary);
  background: #111; border: 1px solid var(--border-faint); border-right: 0; padding: 4px 12px; cursor: pointer;
}
.map-win-btn:last-of-type { border-right: 1px solid var(--border-faint); }
.map-win-btn:hover { color: var(--up); }
.map-win-btn.is-active { color: #000; background: var(--accent); border-color: var(--accent); }
.map-body { flex: 1; min-height: 0; overflow: hidden; padding: 0; position: relative; }
.map-graph { display: block; width: 100%; height: 100%; }
.map-graph text { font-family: var(--font-sans); fill: #fff; }
.map-graph text.mono { font-family: var(--font-mono); }
.map-graph .map-node { cursor: pointer; }
.map-graph .map-node:hover circle { stroke: var(--accent); stroke-width: 2.5; }
/* macro band */
.map-macro { margin-bottom: 16px; border: 1px solid var(--border-faint); background: #0a0a0b; }
.map-macro.macro-buildout { border-color: #1f5f80; }
.map-macro.macro-output   { border-color: #1f6b4a; }
.map-macro.macro-noise    { opacity: 0.72; border-style: dashed; }
.map-macro-head {
  display: flex; align-items: center; gap: 12px; padding: 7px 12px;
  background: var(--bg-title-grad); border-bottom: 1px solid var(--border-faint);
}
.map-macro-title { font-family: var(--font-sans); font-size: 13px; font-weight: 800; letter-spacing: 0.06em; color: var(--fg-primary); }
.map-flow { font-family: var(--font-mono); font-size: 11px; font-weight: 700; display: flex; align-items: center; gap: 6px; margin-left: auto; }
.map-flow .flow-in  { color: var(--up); }
.map-flow .flow-out { color: var(--down); }
.map-flow .flow-flat { color: var(--fg-tertiary); }
.map-flow .map-rsnums { color: var(--fg-tertiary); font-weight: 400; }
/* zone sub-band */
.map-zone { padding: 9px 12px 12px; border-top: 1px solid #141416; }
.map-zone:first-of-type { border-top: 0; }
.map-zone-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.map-zone-name { font-family: var(--font-sans); font-size: 11px; font-weight: 700; color: var(--fg-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
.map-zone-strength { font-family: var(--font-mono); font-size: 11px; font-weight: 700; }
.map-chain-arrow { text-align: center; color: #2f7fa6; font-size: 13px; line-height: 1; margin: 0 0 4px; letter-spacing: 0.3em; }
.map-straddle-note { font-family: var(--font-mono); font-size: 10px; color: var(--accent); padding: 4px 12px 0; }
/* theme nodes */
.map-nodes { display: flex; flex-wrap: wrap; gap: 6px; }
.map-node {
  position: relative; display: flex; flex-direction: column; justify-content: center; gap: 2px;
  padding: 7px 10px; min-width: 92px; max-width: 210px; color: #fff;
  border: 1px solid rgba(255,255,255,0.12); cursor: pointer; overflow: hidden;
}
.map-node:hover { outline: 1px solid var(--accent); outline-offset: -1px; }
.map-node .mn-name { font-family: var(--font-sans); font-size: 11px; font-weight: 600; line-height: 1.15; max-height: 2.3em; overflow: hidden; text-shadow: 0 1px 2px rgba(0,0,0,0.7); }
.map-node .mn-rs { font-family: var(--font-mono); font-size: 12px; font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,0.7); }
.map-node.mn-null { background: #161618 !important; color: var(--fg-tertiary); }
.map-node.mn-straddle { border-style: dashed; border-color: var(--accent); }
.map-node.mn-drift { box-shadow: inset 0 0 0 2px var(--accent); }
/* expand overlay (member charts) — mirrors heatmap expand */
.map-page.is-expanded .map-controls, .map-page.is-expanded .map-body { display: none; }
.map-expand { display: none; flex: 1; min-height: 0; flex-direction: column; }
.map-page.is-expanded .map-expand { display: flex; }
.map-expand-head { display: flex; align-items: center; gap: 12px; flex-shrink: 0; padding: 8px 14px; border-bottom: 1px solid var(--border-faint); background: var(--bg-title-grad); }
.map-expand-title { font-family: var(--font-sans); font-size: 14px; font-weight: 700; color: var(--fg-primary); }
.map-expand-narr { font-family: var(--font-sans); font-size: 11px; color: var(--accent); font-style: italic; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.map-back-btn, .map-viewchart-btn { font-family: var(--font-mono); font-size: 12px; font-weight: 700; color: var(--accent); background: #111; border: 1px solid var(--border-faint); padding: 4px 12px; cursor: pointer; }
.map-back-btn:hover, .map-viewchart-btn:hover { color: #000; background: var(--accent); }
.map-expand-body { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; }
.map-expand-body .member-card { cursor: pointer; }
.map-expand-body .member-card:hover { outline: 1px solid var(--accent); outline-offset: -1px; }
/* Rotation (RRG) view — full-width, sidebar hidden */
body.view-themes.tv-rotation .body-grid { display: none; }
body.view-themes.tv-rotation .rotation-page { display: flex; }
.rotation-page { display: none; flex: 1; min-height: 0; flex-direction: column; background: #000; }
.rotation-controls {
  display: flex; flex-wrap: wrap; align-items: center; gap: 16px; flex-shrink: 0;
  padding: 8px 14px; border-bottom: 1px solid var(--border-faint); background: var(--bg-title-grad);
}
.rot-grp { display: flex; align-items: center; }
.rot-grp .rot-lbl { font-size: 9px; font-weight: 700; text-transform: uppercase; color: #fff; letter-spacing: 0.04em; margin-right: 6px; }
.rot-thrust { width: 120px; accent-color: var(--accent); cursor: pointer; }
.rot-thrust-val { font-family: var(--font-mono); font-size: 11px; color: var(--accent); margin-left: 6px; min-width: 54px; }
.rot-btn {
  font-family: var(--font-mono); font-size: 11px; font-weight: 700;
  color: var(--fg-secondary); background: #111;
  border: 1px solid var(--border-faint); border-right: 0; padding: 3px 9px; cursor: pointer;
}
.rot-grp .rot-btn:last-of-type, .rot-toggle { border-right: 1px solid var(--border-faint); }
.rot-btn:hover { color: var(--up); }
.rot-btn.is-active { color: #000; background: var(--accent); border-color: var(--accent); }
.rotation-body { flex: 1; min-height: 0; display: flex; position: relative; }
.rotation-chart { flex: 1; min-width: 0; min-height: 0; position: relative; z-index: 1; }
#rotation-trail-canvas { position: absolute; top: 0; left: 0; pointer-events: auto; z-index: 3; cursor: default; }
.rot-tooltip {
  position: absolute; z-index: 4; pointer-events: none; display: none;
  background: rgba(8,8,10,0.94); border: 1px solid var(--border-faint);
  padding: 5px 8px; font-family: var(--font-sans); font-size: 11px; color: var(--fg-secondary);
  white-space: nowrap; max-width: 260px;
}
.rot-tooltip b { color: #fff; }
.rotation-side { width: 250px; flex-shrink: 0; border-left: 1px solid var(--border-faint); display: flex; flex-direction: column; min-height: 0; }
.rotation-side-head { display: flex; flex-shrink: 0; border-bottom: 1px solid var(--border-faint); }
.rot-side-btn {
  flex: 1; font-family: var(--font-sans); font-size: 11px; font-weight: 700;
  color: var(--fg-secondary); background: #111; border: 0; border-right: 1px solid var(--border-faint);
  padding: 6px 4px; cursor: pointer;
}
.rot-side-btn:last-child { border-right: 0; }
.rot-side-btn.is-active { color: var(--accent); background: var(--bg-row-selected); }
.rotation-side-list { flex: 1; min-height: 0; overflow-y: auto; }
.rot-item {
  display: flex; align-items: center; gap: 7px; padding: 5px 10px;
  border-bottom: 1px solid var(--border-faint); cursor: pointer;
  font-family: var(--font-sans); font-size: 12px; color: var(--fg-primary);
}
.rot-item:hover { background: var(--bg-row-hover); }
.rot-item .rot-dot { width: 9px; height: 9px; flex-shrink: 0; }
.rot-item .rot-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rot-item .rot-bd { font-family: var(--font-mono); font-size: 10px; color: var(--fg-tertiary); }
.rotation-scrub {
  display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  padding: 9px 16px; border-top: 1px solid var(--border-faint); background: #0a0a0b;
}
.rotation-scrub .rot-scrub-lbl {
  font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--fg-tertiary); white-space: nowrap;
}
.rotation-scrub input[type="range"] {
  flex: 1; -webkit-appearance: none; appearance: none; height: 4px;
  background: #333; border-radius: 2px; cursor: pointer; outline: none;
}
.rotation-scrub input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none; width: 15px; height: 15px;
  border-radius: 50%; background: var(--accent); cursor: pointer; border: 0;
}
.rotation-scrub input[type="range"]::-moz-range-thumb {
  width: 15px; height: 15px; border-radius: 50%; background: var(--accent); cursor: pointer; border: 0;
}
.rotation-scrub .rot-scrub-date {
  font-family: var(--font-mono); font-size: 11px; color: var(--fg-secondary);
  min-width: 82px; text-align: right;
}
/* Click-a-dot overlay: the theme's synthetic chart + member thumbnails over the map */
.rot-overlay {
  position: fixed; left: 0; right: 0; top: 48px; bottom: 22px; z-index: 200;
  background: rgba(0,0,0,0.8); display: flex; align-items: center; justify-content: center;
}
.rot-overlay-panel {
  width: 92%; max-width: 1150px; height: 90%;
  background: #000; border: 1px solid var(--border-faint);
  display: flex; flex-direction: column; box-shadow: 0 10px 50px rgba(0,0,0,0.75);
}
.rot-overlay-head {
  display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  padding: 8px 14px; background: var(--bg-title-grad); border-bottom: 1px solid var(--border-faint);
}
.rot-overlay-title { font-family: var(--font-sans); font-size: 14px; font-weight: 700; color: var(--fg-primary); }
.rot-overlay-close { background: transparent; border: 0; color: var(--fg-secondary); font-size: 15px; cursor: pointer; padding: 2px 8px; }
.rot-overlay-close:hover { color: var(--up); }
.rot-overlay-body { flex: 1; min-height: 0; overflow-y: auto; }
.rot-overlay-body section.theme { display: block !important; }
/* Ball-overlay filter toolbar: replaces the per-theme stats strip (which we
   hide inside the overlay only — the Themes page keeps it). */
.rot-ovf-bar { display: flex; align-items: center; gap: 0; flex-wrap: wrap; }
.rot-ovf-sep { width: 12px; flex-shrink: 0; }
.rot-overlay-body .chart-info-bar { display: none; }
.rot-overlay-body.ovf-no-synth .composite-chart { display: none; }
.rot-overlay-body .member-card.ovf-hidden { display: none; }
.member-sub-panel {
  display: block; width: 100%; height: 54px; background: #000;
  border-top: 1px solid #1c1c1c;
}
/* ── Candidates pane ────────────────────────────────────── */
.candidates-controls {
  display: flex; align-items: center; gap: 6px; flex-shrink: 0;
  padding: 6px 8px; border-bottom: 1px solid var(--border-faint);
  background: var(--bg-title-grad);
}
#candidates-input {
  flex: 1; background: rgba(255,255,255,0.07);
  border: 1px solid var(--border); border-radius: 3px;
  color: var(--fg-primary); font-family: var(--font-mono); font-size: 12px;
  padding: 3px 7px; outline: none; text-transform: uppercase;
}
#candidates-input:focus { border-color: var(--accent); }
#candidates-add-btn {
  appearance: none; border: 1px solid var(--border); border-radius: 3px;
  background: rgba(255,255,255,0.07); color: var(--fg-secondary);
  font-family: var(--font-sans); font-size: 11px; font-weight: 700;
  padding: 3px 10px; cursor: pointer; letter-spacing: 0.04em; white-space: nowrap;
}
#candidates-add-btn:hover { background: var(--accent); color: #000; border-color: var(--accent); }
.candidates-scroll { flex: 1; overflow-y: auto; }
.candidates-table { width: 100%; border-collapse: collapse; }
.candidates-table th {
  text-align: left; font-size: 10px; font-family: var(--font-chrome);
  font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--fg-tertiary); border-bottom: 1px solid var(--border);
  padding: 4px 8px; position: sticky; top: 0; background: #000;
}
.candidates-table td {
  padding: 5px 8px; border-bottom: 1px solid var(--border-faint);
  font-size: 12px; vertical-align: middle;
}
.candidates-table tr { cursor: pointer; }
.candidates-table tr:hover td { background: var(--bg-row-hover); }
.candidates-table tr.cand-active td { background: var(--bg-row-selected); }
.candidates-table tr.cand-active .cand-ticker { color: var(--accent); font-weight: 700; }
.cand-ticker { font-family: var(--font-mono); font-weight: 600; font-size: 13px; color: var(--fg-primary); }
.cand-name   { font-size: 11px; font-family: var(--font-sans); color: var(--fg-secondary);
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
.cand-sector { font-size: 10px; font-family: var(--font-sans); color: var(--fg-tertiary); }
.cand-price  { font-family: var(--font-mono); font-size: 11px; color: var(--fg-primary); text-align: right; }
.cand-chg-pos { font-family: var(--font-mono); font-size: 11px; color: var(--up); text-align: right; }
.cand-chg-neg { font-family: var(--font-mono); font-size: 11px; color: var(--down); text-align: right; }
.cand-unknown { color: var(--fg-tertiary); font-style: italic; font-size: 11px; }
.cand-remove {
  background: none; border: none; cursor: pointer; color: var(--fg-tertiary);
  font-size: 15px; line-height: 1; padding: 0 4px; opacity: 0.4; display: block;
}
.cand-remove:hover { color: var(--down); opacity: 1; }
.theme-chip {
  display: inline-block; padding: 1px 5px; border-radius: 3px;
  font-size: 10px; font-family: var(--font-sans); font-weight: 600;
  margin: 1px 2px 1px 0; white-space: nowrap;
}
.theme-chip-hot  { background: rgba(30,255,30,0.14); color: var(--up);   border: 1px solid rgba(30,255,30,0.28); }
.theme-chip-warm { background: rgba(255,200,0,0.12); color: #ffcc00;      border: 1px solid rgba(255,200,0,0.25); }
.theme-chip-cold { background: rgba(255,255,255,0.05); color: var(--fg-tertiary); border: 1px solid var(--border-faint); }
/* ── Hot-theme confluence (Tickers view) ───────────────── */
.tickers-table th.hot-col, .tickers-table td.hot-cell {
  text-align: center; width: 34px;
}
.tickers-table td.hot-cell {
  font-variant-numeric: tabular-nums; font-weight: 700; color: var(--fg-tertiary);
}
.tickers-table td.hot-cell.hot-1 { color: #1eff1e; }
.tickers-table td.hot-cell.hot-2 { color: #1eff1e; }
.tickers-table td.hot-cell.hot-3plus { color: #000; background: #1eff1e; border-radius: 3px; }
/* Row highlight when a ticker sits in 2+ flagged themes */
.tickers-table tr.tickers-row.hot-confluence td { background: rgba(30,255,30,0.06); }
.tickers-table tr.tickers-row.hot-confluence:hover td { background: rgba(30,255,30,0.12); }
/* Individual hot themes greened inside the membership cell */
.theme-membership-cell .mem-theme.is-hot { color: #1eff1e; font-weight: 700; }
.theme-membership-cell .mem-theme { color: inherit; }
.setups-meta {
  padding: 4px 10px; font-size: 11px; color: var(--fg-tertiary);
  font-family: var(--font-sans); border-bottom: 1px solid var(--border-faint);
}
/* Setups page tab bar — click a tab to switch setup type (single click). */
.setups-tabs {
  display: flex; gap: 0; background: var(--bg-title-grad);
  border-bottom: 1px solid var(--border);
}
.setups-tab {
  appearance: none; border: 0; cursor: pointer;
  padding: 6px 14px; font-family: var(--font-sans);
  font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--fg-secondary);
  background: transparent; border-right: 1px solid #000;
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.10);
}
.setups-tab:hover { color: #ffffff; background: rgba(255,255,255,0.06); }
.setups-tab.is-active {
  color: var(--accent-info); background: var(--bg-canvas);
  box-shadow: inset 0 -2px 0 var(--accent-info);
}
/* Tightening Range D/W/M sub-toggle — smaller, secondary. */
.tighten-tf-tabs {
  display: flex; gap: 6px; padding: 4px 10px;
  border-bottom: 1px solid var(--border-faint);
}
.tighten-tf-tab {
  appearance: none; cursor: pointer; padding: 2px 12px;
  font-family: var(--font-sans); font-size: 10px; font-weight: 700;
  letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--fg-tertiary); background: var(--bg-elevated);
  border: 1px solid var(--border); border-radius: 0;
}
.tighten-tf-tab:hover { color: #ffffff; }
.tighten-tf-tab.is-active { color: #000; background: var(--accent-info); }

/* Per-ticker chart section */
.ticker-view {
  display: block;
  background: var(--bg-primary, #000);
  padding: 0;
}
.ticker-view .ticker-strip {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  padding: 8px 12px;
  background: linear-gradient(to bottom, #2a2c30, #1a1c20);
  border-bottom: 1px solid var(--border-strong);
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--fg-primary);
}
.ticker-view .ticker-strip .ticker-name {
  font-family: var(--font-chrome);
  font-size: 18px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 0.04em;
}
.ticker-view .ticker-strip .long-name { color: var(--fg-secondary); font-size: 11px; }
.ticker-view .ticker-strip .lbl { color: var(--fg-tertiary); margin-right: 3px; }
.ticker-view .ticker-strip .val { color: var(--fg-primary); }
.ticker-view .ticker-strip .pos { color: var(--up); }
.ticker-view .ticker-strip .neg { color: var(--down); }
.ticker-view .ticker-strip .sep { color: var(--border-strong); padding: 0 4px; }
.ticker-view .ticker-chart { width: 100%; }
.ticker-view .ticker-summary {
  padding: 12px;
  font-size: 12px;
  color: var(--fg-secondary);
  line-height: 1.5;
  background: var(--bg-secondary, #0a0a0c);
  border-top: 1px solid var(--border-soft);
}
.ticker-view .ticker-summary:empty { display: none; }

/* Narrow-window fallback. Sidebar still needs to fit the full
   column set so it scales modestly rather than dropping to 240px. */
@media (max-width: 1200px) {
  .body-grid { grid-template-columns: 540px 1fr; }
}
"""


def _fmt_vol(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


def _sma_val(v):
    return f"{v:.2f}" if v is not None else "—"


def _ohlc_strip_html(vals, bar_count, pct_200, pos_label, pos_css, divergences, rs_val, theme_5d, theme_adr):
    # `divergences` is retained in the signature so existing callers don't
    # break, but the BULL DIV / BEAR DIV tags were removed 2026-05-22.
    chg_cls = "up" if vals["chg"] >= 0 else "down"
    pct_str = f"{pct_200:+.1f}%" if pct_200 is not None else "—"
    rs_str = f"{rs_val:+.2f}" if rs_val is not None and rs_val > -1e8 else "—"
    t5d_str = f"{theme_5d:+.2f}%" if theme_5d is not None else "—"
    adr_str = f"{theme_adr:.2f}%" if theme_adr is not None else "—"
    rs_cls = "rs-up" if (rs_val is not None and rs_val >= 0) else "rs-down"
    return (
        f'<div class="chart-info-bar">'
        f'<span class="cluster rs-cluster {rs_cls}">'
        f'<span class="lbl">TC2000 RS / Universe</span>'
        f'<span class="rs-val">{rs_str}x</span>'
        f'<span class="lbl">5d</span><span class="rs-theme">{t5d_str}</span>'
        f'<span class="lbl">ADR</span><span class="rs-theme">{adr_str}</span>'
        f'</span>'
        f'<span class="cluster pos200-cluster {pos_css}">'
        f'<span class="lbl">vs 200D</span>'
        f'<span class="pos200-pct">{pct_str}</span>'
        f'<span class="pos200-label">{pos_label}</span>'
        f'</span>'
        f'<span class="sep">|</span>'
        f'<span class="cluster">'
        f'<span class="date">{vals["date"]}</span>'
        f'<span class="lbl">O</span><span class="val">{vals["open"]:.2f}</span>'
        f'<span class="lbl">H</span><span class="val">{vals["high"]:.2f}</span>'
        f'<span class="lbl">L</span><span class="val">{vals["low"]:.2f}</span>'
        f'<span class="lbl">C</span><span class="val">{vals["close"]:.2f}</span>'
        f'<span class="lbl">Chg</span><span class="{chg_cls}">{vals["chg"]:+.2f}</span>'
        f'<span class="lbl">Chg%</span><span class="{chg_cls}">{vals["chg_pct"]:+.2f}%</span>'
        f'<span class="lbl">Vol</span><span class="val">{_fmt_vol(vals["vol"])}</span>'
        f'<span class="lbl">APTR</span><span class="val">{vals["aptr"]:.2f}</span>'
        f'</span>'
        f'<span class="sep">|</span>'
        f'<span class="cluster">'
        f'<span class="lbl">SMAs</span>'
        f'<span class="sma-5">SMA 5 {_sma_val(vals["sma5"])}</span>'
        f'<span class="sma-10">SMA 10 {_sma_val(vals["sma10"])}</span>'
        f'<span class="sma-20">SMA 20 {_sma_val(vals["sma20"])}</span>'
        f'<span class="sma-50">SMA 50 {_sma_val(vals["sma50"])}</span>'
        f'<span class="sma-200">SMA 200 {_sma_val(vals["sma200"])}</span>'
        f'</span>'
        f'<span class="spacer"></span>'
        f'<span class="cluster muted-meta">'
        f'<span class="lbl">Bars</span><span class="val">{bar_count}</span>'
        f'</span>'
        f'</div>'
    )


def build_dashboard(theme_keys, cache, n_bars, company_meta=None, source_meta=None,
                    fundamentals=None):
    sections_html = []
    sidebar_links = []
    skipped = []
    all_missing = []
    company_meta = company_meta or {}
    source_meta = source_meta or {"source": "main", "last_bar_date": "?", "label": ""}

    # ── Compute Universe benchmark: equal-weight composite of all UNIVERSE
    # tickers, then Dan's TC2000 RS PCF (1, 3, 5, 20, 65, 130 bars). Replaces
    # the cap-weighted SPY benchmark so themes are scored against the same
    # equal-weight construction they themselves use. Self-contribution from
    # each theme's own members into the universe denominator is accepted —
    # tiny themes (<2%) are noise-level; biggest themes (~3%) damp contrast
    # by a fraction that's easier to reason about than 72 sliding denoms.
    print(f"\nBuilding equal-weight Universe composite from {len(UNIVERSE)} tickers...")
    bench_comp_df, bench_used, bench_missing = build_composite(UNIVERSE, cache, n_bars)
    if bench_comp_df is None or len(bench_comp_df) < 51:
        raise RuntimeError(
            f"Universe composite has insufficient data ({0 if bench_comp_df is None else len(bench_comp_df)} bars, {len(bench_used)} used members). Aborting."
        )
    print(f"  Universe composite: {len(bench_used)} members, {len(bench_comp_df)} bars, "
          f"last bar {str(bench_comp_df['date'].iloc[-1])[:10]}")
    if bench_missing:
        print(f"  ({len(bench_missing)} UNIVERSE tickers missing from cache, excluded from benchmark)")

    bench_o = bench_comp_df["open"].values
    bench_h = bench_comp_df["high"].values
    bench_l = bench_comp_df["low"].values
    bench_c = bench_comp_df["close"].values
    bench_rs_0d  = tc2000_rs_intraday(bench_o, bench_h, bench_l, bench_c)
    bench_rs_1   = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=1)
    bench_rs_3   = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=3)
    bench_rs_5   = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=5)
    bench_rs_10  = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=10)
    bench_rs_20  = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=20)
    bench_rs_65  = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=65)
    bench_rs_130 = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=130)
    bench_5d  = n_period_return(bench_c, 5)
    bench_adr = adr_pct(bench_h, bench_l, 20)
    for name in ("bench_rs_0d", "bench_rs_1", "bench_rs_3", "bench_rs_5", "bench_rs_10", "bench_rs_20", "bench_rs_65", "bench_rs_130"):
        if locals().get(name) is None or locals().get(name) == 0:
            print(f"\nWARNING: Universe {name} could not be computed; using 1.0 as fallback.")
    bench_rs_0d  = bench_rs_0d  if (bench_rs_0d  is not None and bench_rs_0d  != 0) else 1.0
    bench_rs_1   = bench_rs_1   if (bench_rs_1   is not None and bench_rs_1   != 0) else 1.0
    bench_rs_3   = bench_rs_3   if (bench_rs_3   is not None and bench_rs_3   != 0) else 1.0
    bench_rs_5   = bench_rs_5   if (bench_rs_5   is not None and bench_rs_5   != 0) else 1.0
    bench_rs_10  = bench_rs_10  if (bench_rs_10  is not None and bench_rs_10  != 0) else 1.0
    bench_rs_20  = bench_rs_20  if (bench_rs_20  is not None and bench_rs_20  != 0) else 1.0
    bench_rs_65  = bench_rs_65  if (bench_rs_65  is not None and bench_rs_65  != 0) else 1.0
    bench_rs_130 = bench_rs_130 if (bench_rs_130 is not None and bench_rs_130 != 0) else 1.0
    print(f"Universe TC2000 RS  0D={bench_rs_0d:+.4f}  1d={bench_rs_1:+.4f}  3d={bench_rs_3:+.4f}  "
          f"5d={bench_rs_5:+.4f}  20d={bench_rs_20:+.4f}  65d={bench_rs_65:+.4f}  130d={bench_rs_130:+.4f}  "
          f"5d return={bench_5d:+.2f}%  ADR%={bench_adr:.2f}%")

    # ── Synthetic narrative themes: one equal-weight composite per story group
    # (each zone + the Buildout roll-up), folded into the SAME pipeline as real
    # themes so they are flaggable + graphable everywhere (watchlist / History /
    # Rotation / Heatmap). In these shared views they score vs the universe like
    # every theme; the Map scores them vs SPY. Member set = the group's deduped
    # tickers (THEME_CHAIN_POSITION + TICKER_ZONE_OVERRIDE straddlers in each).
    NARRATIVE_THEME_IDS = []
    _synth_members = {}
    if NARRATIVE_ZONES and THEME_CHAIN_POSITION:
        def _prio(z):
            try:
                return NARRATIVE_ZONE_PRIORITY.index(z)
            except ValueError:
                return 999
        _tk_zones = {}
        for _th, _mem in THEMES.items():
            _z0 = THEME_CHAIN_POSITION.get(_th)
            if not _z0:
                continue
            for _tk in _mem:
                _tk_zones.setdefault(_tk, set()).add(_z0)
        _tk_final = {}
        for _tk, _zs in _tk_zones.items():
            if _tk in TICKER_ZONE_OVERRIDE:
                _tk_final[_tk] = [z for z in TICKER_ZONE_OVERRIDE[_tk] if z in NARRATIVE_ZONES]
            else:
                _tk_final[_tk] = [min(_zs, key=_prio)]
        for _tk in TICKER_ZONE_OVERRIDE:
            if _tk not in _tk_final:
                _zz = [z for z in TICKER_ZONE_OVERRIDE[_tk] if z in NARRATIVE_ZONES]
                if _zz:
                    _tk_final[_tk] = _zz
        _zlabel = {"hub": "Hub", "infrastructure": "Infrastructure", "power": "Power",
                   "materials": "Materials", "output": "Output", "adjacent": "Adjacent",
                   "crypto": "Crypto", "noise": "Noise"}
        for _z in NARRATIVE_ZONES:
            _ms = sorted({tk for tk, zs in _tk_final.items() if _z in zs})
            if len(_ms) >= 3:
                _sid = "nm_" + _z
                _synth_members[_sid] = _ms
                THEME_LABELS.setdefault(_sid, "✦ " + _zlabel.get(_z, _z.title()))
                NARRATIVE_THEME_IDS.append(_sid)
        _bo = sorted({tk for tk, zs in _tk_final.items()
                      if any(z in ("hub", "infrastructure", "power", "materials") for z in zs)})
        if len(_bo) >= 3:
            _synth_members["nm_buildout"] = _bo
            THEME_LABELS.setdefault("nm_buildout", "✦ Buildout (all)")
            NARRATIVE_THEME_IDS.append("nm_buildout")
        for _sid in NARRATIVE_THEME_IDS:
            THEME_NARRATIVES.setdefault(_sid, "Synthetic narrative composite — equal-weight blend of every stock in this story group. Flag and graph it like any theme.")
    NARRATIVE_THEME_SET = set(NARRATIVE_THEME_IDS)
    themes_all = {**THEMES, **_synth_members}
    theme_keys = list(theme_keys) + NARRATIVE_THEME_IDS

    # ── First pass: build composites, compute 1d + 5d + 20d RS ratios ──
    print("\nBuilding composites and computing TC2000 RS ratios (theme / Universe) for 1d, 5d, 20d...")
    print(f"  (+{len(NARRATIVE_THEME_IDS)} synthetic narrative themes)")
    theme_pack = {}
    for tk_theme in theme_keys:
        members = themes_all[tk_theme]
        composite_df, used, missing = build_composite(members, cache, n_bars)
        if missing:
            all_missing.extend([(tk_theme, m) for m in missing])
        if composite_df is None or len(composite_df) < 51:
            skipped.append((tk_theme, len(used)))
            print(f"  SKIP {tk_theme}: insufficient data ({len(used)} usable members)")
            continue
        theme_rs_0d  = tc2000_rs_intraday(composite_df["open"].values, composite_df["high"].values,
                                           composite_df["low"].values,  composite_df["close"].values)
        theme_rs_1   = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=1)
        theme_rs_3   = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=3)
        theme_rs_5   = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=5)
        theme_rs_10  = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=10)
        theme_rs_20  = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=20)
        theme_rs_65  = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=65)
        theme_rs_130 = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=130)
        theme_5d  = n_period_return(composite_df["close"].values, 5)
        theme_adr = adr_pct(composite_df["high"].values, composite_df["low"].values, 20)
        # abs(bench) so a negative-benchmark window doesn't flip the sign
        # of every theme's ratio (see compute_ticker_pack for the same fix).
        rs0d_ratio  = (theme_rs_0d  / abs(bench_rs_0d))  if theme_rs_0d  is not None else -1e9
        rs1_ratio   = (theme_rs_1   / abs(bench_rs_1))   if theme_rs_1   is not None else -1e9
        rs3_ratio   = (theme_rs_3   / abs(bench_rs_3))   if theme_rs_3   is not None else -1e9
        rs5_ratio   = (theme_rs_5   / abs(bench_rs_5))   if theme_rs_5   is not None else -1e9
        rs10_ratio  = (theme_rs_10  / abs(bench_rs_10))  if theme_rs_10  is not None else -1e9
        rs20_ratio  = (theme_rs_20  / abs(bench_rs_20))  if theme_rs_20  is not None else -1e9
        rs65_ratio  = (theme_rs_65  / abs(bench_rs_65))  if theme_rs_65  is not None else -1e9
        rs130_ratio = (theme_rs_130 / abs(bench_rs_130)) if theme_rs_130 is not None else -1e9
        comp_h = composite_df["high"].values
        comp_l = composite_df["low"].values
        theme_comp3  = compression_n(comp_h, comp_l, 3,  theme_adr)
        theme_comp5  = compression_n(comp_h, comp_l, 5,  theme_adr)
        theme_comp10 = compression_n(comp_h, comp_l, 10, theme_adr)
        theme_comp20 = compression_n(comp_h, comp_l, 20, theme_adr)
        theme_comp30 = compression_n(comp_h, comp_l, 30, theme_adr)
        theme_pack[tk_theme] = dict(
            composite_df=composite_df, used=used,
            theme_5d=theme_5d, theme_adr=theme_adr,
            theme_rs_0d=theme_rs_0d,
            theme_rs_1=theme_rs_1, theme_rs_3=theme_rs_3, theme_rs_5=theme_rs_5,
            theme_rs_10=theme_rs_10, theme_rs_20=theme_rs_20,
            theme_rs_65=theme_rs_65, theme_rs_130=theme_rs_130,
            rs0d_ratio=rs0d_ratio,
            rs1_ratio=rs1_ratio, rs3_ratio=rs3_ratio, rs5_ratio=rs5_ratio,
            rs10_ratio=rs10_ratio, rs20_ratio=rs20_ratio,
            rs65_ratio=rs65_ratio, rs130_ratio=rs130_ratio,
            comp3=theme_comp3, comp5=theme_comp5, comp10=theme_comp10,
            comp20=theme_comp20, comp30=theme_comp30,
        )

    # ── Initial sort: by 5d RS ratio desc (user can re-sort interactively) ──
    sorted_keys = sorted(theme_pack.keys(), key=lambda k: -theme_pack[k]["rs5_ratio"])

    # ── Second pass: emit HTML in sorted order ──
    print("\nEmitting themes sorted by 5-day RS vs Universe (descending)...")
    watchlist_rows = []  # collected for the sortable watchlist table

    for tk_theme in sorted_keys:
        pack = theme_pack[tk_theme]
        composite_df = pack["composite_df"]
        used = pack["used"]
        rs0d_val  = pack["rs0d_ratio"]
        rs1_val   = pack["rs1_ratio"]
        rs5_val   = pack["rs5_ratio"]
        rs20_val  = pack["rs20_ratio"]
        rs65_val  = pack["rs65_ratio"]
        rs130_val = pack["rs130_ratio"]
        theme_5d = pack["theme_5d"]
        theme_adr = pack["theme_adr"]
        label = THEME_LABELS.get(tk_theme, tk_theme.replace("_", " ").title())

        # Divergences were removed 2026-05-22 — pass an empty dict to keep
        # the figure builder's optional divergence parameter happy without
        # actually drawing any overlays.
        narrative = THEME_NARRATIVES.get(tk_theme)
        fig, last_vals = build_composite_figure(composite_df, label, used,
                                                divergences=None, narrative=narrative)
        pct_200, pos_label, pos_css = position_vs_200d(composite_df)

        # Theme-composite Today/ADR ratio — feeds the "Tight D1 only"
        # filter on the watchlist. Same convention as per-ticker:
        # today's range % / 20-day ADR%.
        theme_today_adr = None
        if theme_adr is not None and theme_adr > 0:
            h_last = float(composite_df["high"].iloc[-1])
            l_last = float(composite_df["low"].iloc[-1])
            if l_last and l_last > 0:
                theme_today_adr = ((h_last / l_last - 1.0) * 100.0) / theme_adr

        chart_div = fig.to_html(
            include_plotlyjs=False, full_html=False,
            div_id=f"chart_{tk_theme}",
            config={"displayModeBar": False, "scrollZoom": True, "doubleClick": "reset"},
        )

        # Synthetic narrative themes can span hundreds of tickers; cap their
        # member grid so the HTML doesn't balloon (their value is the composite).
        _grid_used = used[:30] if tk_theme in NARRATIVE_THEME_SET else used
        member_svgs = "".join(
            f'<div class="member-card" data-ticker="{tk}" '
            f'data-momo="{1 if is_momo(cache[tk]) else 0}" '
            f'data-tight="{1 if is_tight_d1(cache[tk]) else 0}">'
            f'{build_mini_svg(cache[tk], tk, meta=company_meta.get(tk))}</div>'
            for tk in _grid_used
        )

        is_below_200 = (pos_css == "pos-below")

        section_html = (
            f'<section class="theme" id="{tk_theme}">'
            f'{_ohlc_strip_html(last_vals, len(composite_df), pct_200, pos_label, pos_css, None, rs5_val, theme_5d, theme_adr)}'
            f'<div class="composite-chart">{chart_div}</div>'
            f'<div class="member-grid">{member_svgs}</div>'
            f'<div class="theme-foot"><span class="theme-foot-name">{label}</span>'
            f'<span class="theme-foot-meta">n={len(used)}</span></div>'
            f'</section>'
        )
        sections_html.append(section_html)
        watchlist_rows.append(dict(
            theme_id=tk_theme,
            label=label,
            comp3=pack.get("comp3"), comp5=pack.get("comp5"),
            comp10=pack.get("comp10"), comp20=pack.get("comp20"),
            comp30=pack.get("comp30"),
            rs0d=rs0d_val if rs0d_val is not None and rs0d_val > -1e8 else None,
            rs1=rs1_val   if rs1_val   is not None and rs1_val   > -1e8 else None,
            rs5=rs5_val   if rs5_val   is not None and rs5_val   > -1e8 else None,
            rs20=rs20_val if rs20_val  is not None and rs20_val  > -1e8 else None,
            rs65=rs65_val if rs65_val  is not None and rs65_val  > -1e8 else None,
            rs130=rs130_val if rs130_val is not None and rs130_val > -1e8 else None,
            pct_200=pct_200,
            count=len(used),
            below_200=is_below_200,
            today_adr_ratio=theme_today_adr,
        ))

        pct_str  = f"{pct_200:+.1f}%" if pct_200 is not None else "n/a"
        rs0d_str = f"{rs0d_val:+.2f}" if rs0d_val is not None and rs0d_val > -1e8 else "n/a"
        rs1_str  = f"{rs1_val:+.2f}"  if rs1_val  is not None and rs1_val  > -1e8 else "n/a"
        rs5_str  = f"{rs5_val:+.2f}"  if rs5_val  is not None and rs5_val  > -1e8 else "n/a"
        rs20_str = f"{rs20_val:+.2f}" if rs20_val is not None and rs20_val > -1e8 else "n/a"
        t5d_str = f"{theme_5d:+.2f}%" if theme_5d is not None else "n/a"
        adr_str = f"{theme_adr:.2f}%" if theme_adr is not None else "n/a"
        print(f"  0D={rs0d_str:>7}x  1d={rs1_str:>7}x  5d={rs5_str:>7}x  20d={rs20_str:>7}x  {tk_theme:35s} "
              f"5dret={t5d_str:>7}  ADR={adr_str:>6}  200D={pct_str:>7}")

    if all_missing:
        print(f"\n{len(all_missing)} ticker references missing from OHLCV cache:")
        for theme, tk in all_missing[:20]:
            print(f"  {theme}: {tk}")
        if len(all_missing) > 20:
            print(f"  ... +{len(all_missing) - 20} more")

    # ── Ungrouped section: UNIVERSE tickers not in any theme ──
    tickers_in_themes = {tk for tlist in THEMES.values() for tk in tlist}
    universe_dedup = list(dict.fromkeys(UNIVERSE))  # preserve order, drop dups
    ungrouped_in_cache = [tk for tk in universe_dedup if tk not in tickers_in_themes and tk in cache]
    ungrouped_missing = [tk for tk in universe_dedup if tk not in tickers_in_themes and tk not in cache]

    if ungrouped_in_cache:
        member_svgs = "".join(
            f'<div class="member-card" data-ticker="{tk}" '
            f'data-momo="{1 if is_momo(cache[tk]) else 0}" '
            f'data-tight="{1 if is_tight_d1(cache[tk]) else 0}">'
            f'{build_mini_svg(cache[tk], tk, meta=company_meta.get(tk))}</div>'
            for tk in ungrouped_in_cache
        )
        ungrouped_section = (
            f'<section class="theme ungrouped" id="ungrouped">'
            f'<div class="chart-info-bar ungrouped-bar">'
            f'<span class="cluster"><span class="lbl">UNGROUPED</span>'
            f'<span class="val">{len(ungrouped_in_cache)} tickers</span></span>'
            f'<span class="sep">|</span>'
            f'<span class="cluster"><span class="lbl">No theme assigned · review and add to theme_map.py</span></span>'
            f'</div>'
            f'<div class="member-grid">{member_svgs}</div>'
            f'<div class="theme-foot"><span class="theme-foot-name">Ungrouped</span>'
            f'<span class="theme-foot-meta">n={len(ungrouped_in_cache)}</span></div>'
            f'</section>'
        )
        sections_html.append(ungrouped_section)
        sidebar_links.append(
            f'<a class="sidebar-link sidebar-ungrouped" href="#ungrouped">UNGROUPED<span class="count"> {len(ungrouped_in_cache)}</span></a>'
        )
        print(f"\nUngrouped (in cache, no theme): {len(ungrouped_in_cache)}")
        print(f"  {', '.join(ungrouped_in_cache)}")

    if ungrouped_missing:
        print(f"\nUngrouped AND missing from cache: {len(ungrouped_missing)}")
        print(f"  {', '.join(ungrouped_missing)}")

    # ── Per-ticker packs powering the tree-view sidebar expansion ──
    # For every theme member and every ungrouped ticker, build a pack that
    # carries the per-row stats (1d / 5d / 20d RS vs Universe, position-vs-200D)
    # and the per-ticker chart arrays (date axis + OHLCV + divergence
    # pairs). JS lazy-renders the chart on first focus by feeding these
    # arrays into a layout template; SMAs / MACD are computed in JS to
    # keep the embedded payload lean.
    print("\nComputing per-ticker packs for tree-view expansion...")
    ticker_packs = {}
    members_by_theme = {}     # theme_id -> ordered list of tickers with packs
    for tk_theme in sorted_keys:
        ordered = []
        for tk in theme_pack[tk_theme]["used"]:
            if tk in ticker_packs:
                ordered.append(tk)
                continue
            pack = compute_ticker_pack(tk, cache[tk], bench_rs_0d, bench_rs_1, bench_rs_3, bench_rs_5, bench_rs_20,
                                       bench_rs_65, bench_rs_130,
                                       n_bars, company_meta, fundamentals)
            if pack is not None:
                ticker_packs[tk] = pack
                ordered.append(tk)
        members_by_theme[tk_theme] = ordered
    ungrouped_with_packs = []
    for tk in ungrouped_in_cache:
        if tk in ticker_packs:
            ungrouped_with_packs.append(tk)
            continue
        pack = compute_ticker_pack(tk, cache[tk], bench_rs_0d, bench_rs_1, bench_rs_3, bench_rs_5, bench_rs_20,
                                       bench_rs_65, bench_rs_130,
                                   n_bars, company_meta, fundamentals=fundamentals)
        if pack is not None:
            ticker_packs[tk] = pack
            ungrouped_with_packs.append(tk)
    print(f"  Built {len(ticker_packs)} ticker packs "
          f"({sum(len(v) for v in members_by_theme.values())} across themes, "
          f"{len(ungrouped_with_packs)} ungrouped).")

    # ── Extension Peek scan (Setups page) ──────────────────────────────
    # Reads the JSON snapshot built by ext50_trendline_snapshot_builder.py
    # (intended to run at 7:30 AM after EOD bar lands), projects each
    # ticker's u1/u2/u3 descending lines forward to today's bar, and
    # surfaces tickers whose live ext50 just crossed above a clean line.
    print("\nLoading Extension Peek snapshot...")
    peek_snapshot = load_extension_peek_snapshot()
    if peek_snapshot is None:
        print("  WARNING: ext50_trendline_snapshots.json not found.")
        print("  Run: python local_runner/ext50_trendline_snapshot_builder.py")
        extension_peeks = []
        peek_asof_date = "?"
    else:
        # asof_date lives per-ticker; all entries share the same value
        # (the builder uses one cache + one drop-last-bar rule). Pull one.
        sample = next(iter(peek_snapshot.get("tickers", {}).values()), None)
        peek_asof_date = (sample or {}).get("asof_date") or "?"
        built = peek_snapshot.get("built_at") or "?"
        n_snap = peek_snapshot.get("n_snapshots") or 0
        print(f"  snapshot asof={peek_asof_date}  built_at={built[:19]}  n_snapshots={n_snap}")
        extension_peeks = compute_extension_peeks(peek_snapshot, cache, UNIVERSE)
        print(f"  {len(extension_peeks)} Extension Peek matches in UNIVERSE today")

    # ── First Flags scan (Setups page, second setup type) ──────────────
    # Reads first_flags_snapshots.json: a real bullish MACD 6/20 divergence
    # below the 200-SMA, then the 10/20/50 SMAs stacked (the trend) and still
    # stacked, in its first pullback (<=2 swing highs since the stack) below the
    # post-stack high and riding the fast MA. Here we only refresh today's
    # pullback — no divergence re-detection; pivots are fixed for the day.
    print("\nLoading First Flags snapshot...")
    ff_snapshot = load_first_flags_snapshot()
    if ff_snapshot is None:
        print("  WARNING: first_flags_snapshots.json not found.")
        print("  Run: python local_runner/first_flags_snapshot_builder.py")
        first_flags = []
        ff_asof_date = "?"
    else:
        first_flags, ff_asof_date = compute_first_flags(ff_snapshot, cache)
        built = ff_snapshot.get("built_at") or "?"
        print(f"  snapshot asof={ff_asof_date}  built_at={built[:19]}  "
              f"n_matches={ff_snapshot.get('n_matches', 0)}")
        print(f"  {len(first_flags)} First Flags matches in UNIVERSE")

    # ── Tightening Range scan (Setups page, third setup type) ───────────
    # Reads tightening_range_snapshots.json: a converging wedge (descending
    # resistance + rising support, price inside, band contracting, apex not
    # pointing down) on daily / weekly / monthly. Lines are fixed for the day.
    print("\nLoading Tightening Range snapshot...")
    tr_snapshot = load_tightening_range_snapshot()
    if tr_snapshot is None:
        print("  WARNING: tightening_range_snapshots.json not found.")
        print("  Run: python local_runner/tightening_range_snapshot_builder.py")
        tightening_ranges = []
        tr_asof_date = "?"
    else:
        tightening_ranges, tr_asof_date = compute_tightening_ranges(tr_snapshot)
        by_tf = tr_snapshot.get("n_matches_by_tf", {})
        print(f"  asof={tr_asof_date}  by_tf={by_tf}  "
              f"{len(tightening_ranges)} Tightening Range rows (D/W/M)")

    # Reverse index: for the Tickers view, each ticker row carries the
    # list of theme labels it belongs to so the Theme cell can name them.
    # Built from the sorted theme order so the visible label is stable.
    themes_by_ticker = {}
    theme_ids_by_ticker = {}   # ticker -> [theme_id, ...] (raw keys for filter JS)
    for tk_theme in sorted_keys:
        label = THEME_LABELS.get(tk_theme, tk_theme.replace("_", " ").title())
        for tk in members_by_theme.get(tk_theme, []):
            themes_by_ticker.setdefault(tk, []).append(label)
            theme_ids_by_ticker.setdefault(tk, []).append(tk_theme)
    # Ungrouped tickers get a sentinel — rendered in gold in the cell.
    for tk in ungrouped_with_packs:
        themes_by_ticker.setdefault(tk, []).append("Ungrouped")
        theme_ids_by_ticker.setdefault(tk, []).append("ungrouped")

    # Dominant sector + industry per theme — most common values among the
    # theme's members. Powers the "filter by sector / industry" effect on
    # the Themes view.
    from collections import Counter
    theme_dominant_sector = {}
    theme_dominant_industry = {}
    for tk_theme in sorted_keys:
        sectors = []
        industries = []
        for tk in members_by_theme.get(tk_theme, []):
            sectors.append(ticker_packs[tk].get("sector") or "Unknown")
            industries.append(ticker_packs[tk].get("industry") or "Unknown")
        if sectors:
            theme_dominant_sector[tk_theme] = Counter(sectors).most_common(1)[0][0]
        else:
            theme_dominant_sector[tk_theme] = "Unknown"
        if industries:
            theme_dominant_industry[tk_theme] = Counter(industries).most_common(1)[0][0]
        else:
            theme_dominant_industry[tk_theme] = "Unknown"
    # Ungrouped pseudo-theme has no dominant sector / industry.
    theme_dominant_sector["ungrouped"] = "Unknown"
    theme_dominant_industry["ungrouped"] = "Unknown"

    # Sectors available for the filter panel — union over all ticker packs.
    available_sectors = sorted({(p.get("sector") or "Unknown") for p in ticker_packs.values()})
    # Industries surfaced in the filter alongside sectors. Each entry maps
    # a display label to the Yahoo Finance industry string we'll match on.
    # Only Biotech for now; easy to add more sub-sector rollups later.
    available_industries = [
        {"label": "Biotech", "match": "Biotechnology"},
    ]

    # Add an 'ungrouped' pseudo-theme row to the watchlist so it can be
    # expanded the same way real themes can. RS columns stay blank because
    # there is no Ungrouped composite to compute against.
    if ungrouped_with_packs:
        watchlist_rows.append(dict(
            theme_id="ungrouped",
            label="UNGROUPED",
            rs0d=None, rs1=None, rs5=None, rs20=None, rs65=None, rs130=None,
            comp3=None, comp5=None, comp10=None, comp20=None, comp30=None,
            pct_200=None,
            count=len(ungrouped_with_packs),
            below_200=False,
            today_adr_ratio=None,
            is_ungrouped=True,
        ))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_themes = len([s for s in sections_html if 'id="ungrouped"' not in s])
    n_universe = len(universe_dedup)
    n_in_themes = len([tk for tk in universe_dedup if tk in tickers_in_themes])
    n_ungrouped = len(ungrouped_in_cache) + len(ungrouped_missing)
    universe_summary = f"{n_universe} total · {n_in_themes} in themes · {n_ungrouped} ungrouped"
    bench_5d_str = f"{bench_5d:+.2f}%"
    bench_5d_cls = "up" if bench_5d >= 0 else "down"

    # SPY last date for the status bar
    spy_last = "?"
    if "SPY" in cache:
        try:
            spy_last = str(cache["SPY"]["date"].iloc[-1])[:10]
        except Exception:
            pass

    # Intraday source marker — appended to the "Cache Last Bar" cell when
    # the dashboard is reading universe_ohlcv_daily_intraday.pkl. Gold-tinted
    # accent so it's unambiguous whether the bars are official MOC or
    # synthetic 4:20 PM ET snapshot.
    is_intraday = source_meta.get("source") == "intraday"
    intraday_label = source_meta.get("label", "")
    last_bar_value_cls = "rm-val intraday" if is_intraday else "rm-val accent"
    last_bar_inner = spy_last
    if is_intraday and intraday_label:
        last_bar_inner = f'{spy_last} <span class="intraday-marker">{intraday_label}</span>'

    # ── Build the sortable watchlist HTML ──
    def _num_cell(val, fmt="{:+.2f}"):
        if val is None:
            return '<td class="num nul">—</td>'
        cls = "pos" if val >= 0 else "neg"
        return f'<td class="num {cls}">{fmt.format(val)}</td>'

    def _ticker_child_html(tk_theme, tk):
        """Build one child <tr> for a ticker under its parent theme.

        Initially hidden via the `child-collapsed` class; the JS strips that
        class when the parent is expanded.
        """
        p = ticker_packs[tk]
        below_cls = " below-200" if p["below_200"] else ""
        rs0d = p["rs0d"]; rs1 = p["rs1"]; rs5 = p["rs5"]; rs20 = p["rs20"]
        rs65 = p["rs65"]; rs130 = p["rs130"]
        adr_ratio = p.get("today_adr_ratio")
        comp3 = p.get("comp3"); comp5 = p.get("comp5"); comp10 = p.get("comp10")
        comp20 = p.get("comp20"); comp30 = p.get("comp30")
        rs0d_attr  = f"{rs0d:.4f}"  if rs0d  is not None else "-1e9"
        rs1_attr   = f"{rs1:.4f}"   if rs1   is not None else "-1e9"
        rs5_attr   = f"{rs5:.4f}"   if rs5   is not None else "-1e9"
        rs20_attr  = f"{rs20:.4f}"  if rs20  is not None else "-1e9"
        rs65_attr  = f"{rs65:.4f}"  if rs65  is not None else "-1e9"
        rs130_attr = f"{rs130:.4f}" if rs130 is not None else "-1e9"
        adr_attr   = f"{adr_ratio:.4f}" if adr_ratio is not None else "1e9"
        # Compression: lower = tighter; missing values get sentinel 1e9 so
        # they sort to the bottom on ascending (tightest-first) sort.
        comp3_attr  = f"{comp3:.4f}"  if comp3  is not None else "1e9"
        comp5_attr  = f"{comp5:.4f}"  if comp5  is not None else "1e9"
        comp10_attr = f"{comp10:.4f}" if comp10 is not None else "1e9"
        comp20_attr = f"{comp20:.4f}" if comp20 is not None else "1e9"
        comp30_attr = f"{comp30:.4f}" if comp30 is not None else "1e9"
        row_id = f"{tk_theme}__{tk}"
        return (
            f'<tr class="watchlist-row ticker-row child-collapsed{below_cls}"'
            f' data-row-id="{row_id}" data-row-kind="ticker"'
            f' data-theme-id="{tk_theme}" data-ticker="{tk}"'
            f' data-label="{tk}"'
            f' data-rs0d="{rs0d_attr}" data-rs1="{rs1_attr}" data-rs5="{rs5_attr}" data-rs20="{rs20_attr}"'
            f' data-rs65="{rs65_attr}" data-rs130="{rs130_attr}"'
            f' data-comp3="{comp3_attr}" data-comp5="{comp5_attr}" data-comp10="{comp10_attr}"'
            f' data-comp20="{comp20_attr}" data-comp30="{comp30_attr}"'
            f' data-adr="{adr_attr}" data-n="-1">'
            f'<td class="flag-cell"></td>'
            f'<td class="theme-name"><span class="tree-indent"></span>'
            f'<span class="tree-bullet">▪</span>'
            f'<span class="ticker-symbol">{tk}</span></td>'
            f'{_num_cell(rs0d)}'
            f'{_num_cell(rs1)}'
            f'{_num_cell(rs5)}'
            f'{_num_cell(rs20)}'
            f'{_num_cell(rs65)}'
            f'{_num_cell(rs130)}'
            f'<td class="num comp-cell"></td>'
            f'<td class="num count">—</td>'
            f'</tr>'
        )

    watchlist_body_rows = []
    for r in watchlist_rows:
        below_cls = " below-200" if r["below_200"] else ""
        is_ungrouped_row = bool(r.get("is_ungrouped"))
        rs0d = r.get("rs0d"); rs1 = r["rs1"]; rs5 = r["rs5"]; rs20 = r["rs20"]
        rs65 = r.get("rs65"); rs130 = r.get("rs130")
        adr_ratio = r.get("today_adr_ratio")
        comp3 = r.get("comp3"); comp5 = r.get("comp5"); comp10 = r.get("comp10")
        comp20 = r.get("comp20"); comp30 = r.get("comp30")
        rs0d_attr  = f"{rs0d:.4f}"  if rs0d  is not None else "-1e9"
        rs1_attr   = f"{rs1:.4f}"   if rs1   is not None else "-1e9"
        rs5_attr   = f"{rs5:.4f}"   if rs5   is not None else "-1e9"
        rs20_attr  = f"{rs20:.4f}"  if rs20  is not None else "-1e9"
        rs65_attr  = f"{rs65:.4f}"  if rs65  is not None else "-1e9"
        rs130_attr = f"{rs130:.4f}" if rs130 is not None else "-1e9"
        adr_attr   = f"{adr_ratio:.4f}" if adr_ratio is not None else "1e9"
        comp3_attr  = f"{comp3:.4f}"  if comp3  is not None else "1e9"
        comp5_attr  = f"{comp5:.4f}"  if comp5  is not None else "1e9"
        comp10_attr = f"{comp10:.4f}" if comp10 is not None else "1e9"
        comp20_attr = f"{comp20:.4f}" if comp20 is not None else "1e9"
        comp30_attr = f"{comp30:.4f}" if comp30 is not None else "1e9"
        # Theme row gets a caret cell. The ▸/▾ glyph is the toggle target,
        # but pressing → / ← on the row also fires it (JS handles both).
        extra_classes = " theme-row"
        if is_ungrouped_row:
            extra_classes += " ungrouped-row"
        watchlist_body_rows.append(
            f'<tr class="watchlist-row{extra_classes}{below_cls}"'
            f' data-row-id="{r["theme_id"]}" data-row-kind="theme"'
            f' data-theme-id="{r["theme_id"]}"'
            f' data-label="{r["label"]}"'
            f' data-rs0d="{rs0d_attr}" data-rs1="{rs1_attr}" data-rs5="{rs5_attr}" data-rs20="{rs20_attr}"'
            f' data-rs65="{rs65_attr}" data-rs130="{rs130_attr}"'
            f' data-comp3="{comp3_attr}" data-comp5="{comp5_attr}" data-comp10="{comp10_attr}"'
            f' data-comp20="{comp20_attr}" data-comp30="{comp30_attr}"'
            f' data-adr="{adr_attr}"'
            f' data-n="{r["count"]}" data-expanded="0">'
            f'<td class="flag-cell">'
            f'<svg class="flag-icon" viewBox="0 0 12 12" data-flag-theme="{r["theme_id"]}">'
            f'<polygon points="2,1 10,4 2,7"/></svg>'
            f'</td>'
            f'<td class="theme-name">'
            f'<span class="tree-caret">▸</span>'
            f'<span class="theme-label">{r["label"]}</span></td>'
            f'{_num_cell(rs0d)}'
            f'{_num_cell(rs1)}'
            f'{_num_cell(rs5)}'
            f'{_num_cell(rs20)}'
            f'{_num_cell(rs65)}'
            f'{_num_cell(rs130)}'
            f'<td class="num comp-cell"></td>'
            f'<td class="num count">{r["count"]}</td>'
            f'</tr>'
        )
        # Emit child ticker rows in the parent theme's declared order.
        # JS re-sorts them whenever the user changes the sort column.
        if is_ungrouped_row:
            children = ungrouped_with_packs
        else:
            children = members_by_theme.get(r["theme_id"], [])
        for tk in children:
            watchlist_body_rows.append(_ticker_child_html(r["theme_id"], tk))

    # ── Tickers-view flat table — all rows visible by default ──
    # One row per ticker that has a pack. ADR-tight + Hot N filters are
    # applied client-side via JS inline display styles based on checkbox
    # state; the `tight-adr` class is kept as a row marker so sorting
    # doesn't lose the filter state. We also stash theme membership as a
    # comma-separated label string per row for the Theme column.
    ADR_TIGHT_THRESHOLD = 1.10
    tickers_body_rows = []
    for tk in sorted(ticker_packs.keys()):
        p = ticker_packs[tk]
        theme_labels_for_tk = themes_by_ticker.get(tk, ["Ungrouped"])
        theme_cell_text = ", ".join(theme_labels_for_tk)
        theme_ids_for_tk = theme_ids_by_ticker.get(tk, ["ungrouped"])
        theme_ids_attr = ",".join(theme_ids_for_tk)
        is_ungrouped_tk = (theme_labels_for_tk == ["Ungrouped"])
        # Per-ticker stats
        rs0d = p["rs0d"]; rs1 = p["rs1"]; rs3 = p["rs3"]; rs5 = p["rs5"]; rs20 = p["rs20"]
        rs65 = p["rs65"]; rs130 = p["rs130"]
        comp3  = p.get("comp3"); comp5  = p.get("comp5"); comp10 = p.get("comp10")
        comp20 = p.get("comp20"); comp30 = p.get("comp30")
        adr_ratio = p["today_adr_ratio"]
        ext50 = p.get("ext50")
        is_tight = (adr_ratio is not None and adr_ratio < ADR_TIGHT_THRESHOLD)
        below_cls = " below-200" if p["below_200"] else ""
        tight_cls = " tight-adr" if is_tight else ""
        ungrouped_cls = " ungrouped-ticker" if is_ungrouped_tk else ""
        rs0d_attr  = f"{rs0d:.4f}"  if rs0d  is not None else "-1e9"
        rs1_attr   = f"{rs1:.4f}"   if rs1   is not None else "-1e9"
        rs3_attr   = f"{rs3:.4f}"   if rs3   is not None else "-1e9"
        rs5_attr   = f"{rs5:.4f}"   if rs5   is not None else "-1e9"
        rs20_attr  = f"{rs20:.4f}"  if rs20  is not None else "-1e9"
        rs65_attr  = f"{rs65:.4f}"  if rs65  is not None else "-1e9"
        rs130_attr = f"{rs130:.4f}" if rs130 is not None else "-1e9"
        adr_attr   = f"{adr_ratio:.4f}" if adr_ratio is not None else "1e9"
        comp3_attr  = f"{comp3:.4f}"  if comp3  is not None else "1e9"
        comp5_attr  = f"{comp5:.4f}"  if comp5  is not None else "1e9"
        comp10_attr = f"{comp10:.4f}" if comp10 is not None else "1e9"
        comp20_attr = f"{comp20:.4f}" if comp20 is not None else "1e9"
        comp30_attr = f"{comp30:.4f}" if comp30 is not None else "1e9"
        # ext50 attr: real value when computable; "nan" sentinel for < 50-bar
        # IPOs so the Near 50SMA filter hides what it can't confirm.
        ext50_attr = f"{ext50:.4f}" if ext50 is not None else "nan"
        rs0d_str  = f"{rs0d:+.2f}"  if rs0d  is not None else "—"
        rs1_str   = f"{rs1:+.2f}"   if rs1   is not None else "—"
        rs3_str   = f"{rs3:+.2f}"   if rs3   is not None else "—"
        rs65_str  = f"{rs65:+.2f}"  if rs65  is not None else "—"
        rs130_str = f"{rs130:+.2f}" if rs130 is not None else "—"
        adr_str   = f"{adr_ratio:.2f}" if adr_ratio is not None else "—"
        def _rs_cls(v):
            return "pos" if (v is not None and v >= 0) else ("neg" if v is not None else "nul")
        tickers_body_rows.append(
            f'<tr class="tickers-row{below_cls}{tight_cls}{ungrouped_cls}"'
            f' data-row-id="tk__{tk}" data-row-kind="ticker-flat"'
            f' data-ticker="{tk}" data-label="{tk}" data-theme-label="{theme_cell_text}"'
            f' data-theme-ids="{theme_ids_attr}"'
            f' data-rs0d="{rs0d_attr}" data-rs1="{rs1_attr}" data-rs3="{rs3_attr}"'
            f' data-rs5="{rs5_attr}" data-rs20="{rs20_attr}"'
            f' data-rs65="{rs65_attr}" data-rs130="{rs130_attr}"'
            f' data-comp3="{comp3_attr}" data-comp5="{comp5_attr}" data-comp10="{comp10_attr}"'
            f' data-comp20="{comp20_attr}" data-comp30="{comp30_attr}"'
            f' data-adr="{adr_attr}" data-ext50="{ext50_attr}" data-momo="{1 if p.get("momo") else 0}" data-hot="0">'
            f'<td class="ticker-symbol-cell"><svg class="tflag-icon tflag-row" viewBox="0 0 12 12" data-flag-ticker="{tk}" title="Flag / unflag {tk}"><polygon points="2,1 10,4 2,7"/></svg><span class="ticker-symbol">{tk}</span></td>'
            f'<td class="theme-membership-cell" title="{theme_cell_text}">{theme_cell_text}</td>'
            f'<td class="num hot-cell"></td>'
            f'<td class="num {_rs_cls(rs0d)}">{rs0d_str}</td>'
            f'<td class="num {_rs_cls(rs1)}">{rs1_str}</td>'
            f'<td class="num {_rs_cls(rs3)}">{rs3_str}</td>'
            f'<td class="num {_rs_cls(rs65)}">{rs65_str}</td>'
            f'<td class="num {_rs_cls(rs130)}">{rs130_str}</td>'
            f'<td class="num comp-cell"></td>'
            f'<td class="num adr">{adr_str}</td>'
            f'</tr>'
        )

    tickers_table_html = (
        '<table class="watchlist-table tickers-table" id="tickers-watchlist">'
        '<thead><tr>'
        '<th data-sort-key="label"        data-sort-type="text">Ticker</th>'
        '<th data-sort-key="theme-label"  data-sort-type="text">Theme</th>'
        '<th data-sort-key="hot"          data-sort-type="num" class="hot-col" title="How many of your flagged (hot) themes this ticker belongs to. Flag themes in the Themes view; click to sort confluence names to the top.">Hot</th>'
        '<th data-sort-key="rs0d"         data-sort-type="num" class="sort-active">0D</th>'
        '<th data-sort-key="rs1"          data-sort-type="num">1d</th>'
        '<th data-sort-key="rs3"          data-sort-type="num">3d</th>'
        '<th data-sort-key="rs65"         data-sort-type="num">65d</th>'
        '<th data-sort-key="rs130"        data-sort-type="num">130d</th>'
        '<th data-sort-key="comp"         data-sort-type="num" class="comp-header" title="Left-click to sort; right-click to pick period">Comp <span class="comp-period">10</span></th>'
        '<th data-sort-key="adr"          data-sort-type="num">ADR</th>'
        '</tr></thead>'
        f'<tbody id="tickers-watchlist-body">{"".join(tickers_body_rows)}</tbody>'
        '</table>'
    )

    # ── Setups view: Extension Peek table ────────────────────────────
    # Built from the extension_peeks list computed above. Sort default is
    # tightest-peek-first (smallest |today_sd|). Each row reuses the
    # existing ticker_packs data attributes so the JS click-to-render
    # pipeline works exactly like the Tickers view.
    setups_body_rows = []
    for m in extension_peeks:
        tk = m["ticker"]
        p = ticker_packs.get(tk)
        if p is None:
            continue
        # Reuse same per-ticker stats the Tickers view shows. Setups rows
        # must carry the SAME data attrs as ticker-flat rows so the existing
        # Hot N / Cold N / Tight / Hide < 200 / Sector / Theme / Flagged
        # filters all apply uniformly.
        rs0d = p["rs0d"]; rs1 = p["rs1"]
        rs5 = p["rs5"]; rs20 = p["rs20"]
        rs65 = p["rs65"]; rs130 = p["rs130"]
        comp10 = p.get("comp10")
        adr_pct_val = p.get("adr")
        today_adr_ratio = p.get("today_adr_ratio")
        sector = p.get("sector", "")
        below_200 = bool(p.get("below_200"))
        theme_labels = ", ".join(themes_by_ticker.get(tk, ["Ungrouped"]))
        theme_ids_for_tk = theme_ids_by_ticker.get(tk, ["ungrouped"])
        theme_ids_attr_val = ",".join(theme_ids_for_tk)

        def _f(v, fmt="{:+.2f}"):
            if v is None: return "—"
            try: return fmt.format(v)
            except Exception: return str(v)

        def _f_attr(v, sentinel="-1e9"):
            try: return f"{float(v):.4f}"
            except Exception: return sentinel

        peek_attr   = f"{m['today_sd_abs']:.4f}"
        yest_attr   = f"{m['yest_sd']:.4f}"
        drop_attr   = f"{m['line_drop']:.4f}"
        slope_attr  = f"{m['slope']:.6f}"
        rs0d_attr   = _f_attr(rs0d)
        rs1_attr    = _f_attr(rs1)
        rs5_attr    = _f_attr(rs5)
        rs20_attr   = _f_attr(rs20)
        rs65_attr   = _f_attr(rs65)
        rs130_attr  = _f_attr(rs130)
        comp10_attr = f"{comp10:.4f}" if comp10 is not None else "1e9"
        adr_attr_val = _f_attr(today_adr_ratio, sentinel="1e9")
        adr_pct_attr = _f_attr(adr_pct_val)
        cls_rs0d = "pos" if (rs0d is not None and rs0d >= 0) else ("neg" if rs0d is not None else "nul")
        cls_rs1  = "pos" if (rs1  is not None and rs1  >= 0) else ("neg" if rs1  is not None else "nul")
        below_cls = " below-200" if below_200 else ""

        setups_body_rows.append(
            f'<tr class="setups-row tickers-row{below_cls}"'
            f' data-row-id="setup__{tk}" data-row-kind="setup"'
            f' data-ticker="{tk}" data-label="{tk}" data-theme-label="{theme_labels}"'
            f' data-theme-ids="{theme_ids_attr_val}"'
            f' data-sector="{sector}"'
            f' data-peek="{peek_attr}" data-yest-sd="{yest_attr}"'
            f' data-drop="{drop_attr}" data-slope="{slope_attr}"'
            f' data-rs0d="{rs0d_attr}" data-rs1="{rs1_attr}"'
            f' data-rs5="{rs5_attr}" data-rs20="{rs20_attr}"'
            f' data-rs65="{rs65_attr}" data-rs130="{rs130_attr}"'
            f' data-comp10="{comp10_attr}" data-adr="{adr_attr_val}"'
            f' data-slot="u{m["slot"]}">'
            f'<td class="ticker-symbol-cell"><svg class="tflag-icon tflag-row" viewBox="0 0 12 12" data-flag-ticker="{tk}" title="Flag / unflag {tk}"><polygon points="2,1 10,4 2,7"/></svg><span class="ticker-symbol">{tk}</span></td>'
            f'<td class="theme-membership-cell" title="{theme_labels}">{theme_labels}</td>'
            f'<td class="num">u{m["slot"]}</td>'
            f'<td class="num pos">+{m["today_sd_abs"]:.3f}</td>'
            f'<td class="num">{m["yest_sd"]:+.3f}</td>'
            f'<td class="num">{m["line_drop"]:+.2f}</td>'
            f'<td class="num {cls_rs0d}">{_f(rs0d)}</td>'
            f'<td class="num {cls_rs1}">{_f(rs1)}</td>'
            f'<td class="num">{_f(comp10, "{:.2f}")}</td>'
            f'<td class="num adr">{_f(adr_pct_val, "{:.2f}")}</td>'
            f'</tr>'
        )

    setups_table_html = (
        '<table class="watchlist-table tickers-table setups-table" id="setups-watchlist">'
        '<thead><tr>'
        '<th data-sort-key="label"        data-sort-type="text">Ticker</th>'
        '<th data-sort-key="theme-label"  data-sort-type="text">Theme</th>'
        '<th data-sort-key="slot"         data-sort-type="text" title="Which descending line slot peeked (u1 = nearest)">Line</th>'
        '<th data-sort-key="peek"         data-sort-type="num" class="sort-active" title="ADRs above the descending trendline today">|Peek|</th>'
        '<th data-sort-key="yest-sd"      data-sort-type="num" title="ADRs the line was above price yesterday (larger = bigger setup)">Yest sd</th>'
        '<th data-sort-key="drop"         data-sort-type="num" title="ADRs the line dropped from anchor 0 to anchor 1">Drop</th>'
        '<th data-sort-key="rs0d"         data-sort-type="num">0D</th>'
        '<th data-sort-key="rs1"          data-sort-type="num">1d</th>'
        '<th data-sort-key="comp10"       data-sort-type="num">Comp10</th>'
        '<th data-sort-key="adr"          data-sort-type="num">ADR</th>'
        '</tr></thead>'
        f'<tbody id="setups-watchlist-body">{"".join(setups_body_rows)}</tbody>'
        '</table>'
    )

    # ── First Flags table (Setups page, second setup tab) ────────────
    # Each row reuses the SAME per-ticker data attrs as the Tickers /
    # Extension Peek rows so the existing Hide / Tight / Flagged / Sector /
    # Theme / Hot / Cold filters all apply uniformly. (Near 50SMA stays
    # Tickers-view-only, so no ext50 attr is needed here.)
    firstflags_body_rows = []
    for m in first_flags:
        tk = m["ticker"]
        p = ticker_packs.get(tk)
        if p is None:
            continue
        rs0d = p["rs0d"]; rs1 = p["rs1"]; rs5 = p["rs5"]; rs20 = p["rs20"]
        rs65 = p["rs65"]; rs130 = p["rs130"]
        comp10 = p.get("comp10")
        adr_pct_val = p.get("adr")
        today_adr_ratio = p.get("today_adr_ratio")
        sector = p.get("sector", "")
        below_200 = bool(p.get("below_200"))
        theme_labels = ", ".join(themes_by_ticker.get(tk, ["Ungrouped"]))
        theme_ids_attr_val = ",".join(theme_ids_by_ticker.get(tk, ["ungrouped"]))

        def _ffa(v, sentinel="-1e9"):
            try: return f"{float(v):.4f}"
            except Exception: return sentinel
        def _ffd(v, fmt="{:+.2f}"):
            if v is None: return "—"
            try: return fmt.format(v)
            except Exception: return str(v)

        below200 = m.get("below_200_pct")
        pole = m.get("pole_pct")
        days = m.get("bars_since_bottom")
        pullback = m.get("live_pullback_pct")
        bottom_date = m.get("bottom_date", "")

        below200_str  = f"{below200:.1f}" if below200 is not None else "—"
        pole_str      = f"+{pole:.0f}" if pole is not None else "—"
        days_str      = f"{int(days)}" if days is not None else "—"
        pullback_str  = f"{pullback:.1f}" if pullback is not None else "—"
        comp10_attr   = f"{comp10:.4f}" if comp10 is not None else "1e9"
        adr_attr_val  = _ffa(today_adr_ratio, sentinel="1e9")
        below_cls     = " below-200" if below_200 else ""
        cls_rs0d = "pos" if (rs0d is not None and rs0d >= 0) else ("neg" if rs0d is not None else "nul")
        cls_rs1  = "pos" if (rs1  is not None and rs1  >= 0) else ("neg" if rs1  is not None else "nul")

        firstflags_body_rows.append(
            f'<tr class="setups-row tickers-row{below_cls}"'
            f' data-row-id="firstflag__{tk}" data-row-kind="setup"'
            f' data-ticker="{tk}" data-label="{tk}" data-theme-label="{theme_labels}"'
            f' data-theme-ids="{theme_ids_attr_val}" data-sector="{sector}"'
            f' data-bottomdate="{bottom_date}"'
            f' data-below200="{_ffa(below200)}" data-pole="{_ffa(pole)}"'
            f' data-days="{_ffa(days)}" data-pullback="{_ffa(pullback)}"'
            f' data-rs0d="{_ffa(rs0d)}" data-rs1="{_ffa(rs1)}"'
            f' data-rs5="{_ffa(rs5)}" data-rs20="{_ffa(rs20)}"'
            f' data-rs65="{_ffa(rs65)}" data-rs130="{_ffa(rs130)}"'
            f' data-comp10="{comp10_attr}" data-adr="{adr_attr_val}">'
            f'<td class="ticker-symbol-cell"><svg class="tflag-icon tflag-row" viewBox="0 0 12 12" data-flag-ticker="{tk}" title="Flag / unflag {tk}"><polygon points="2,1 10,4 2,7"/></svg><span class="ticker-symbol">{tk}</span></td>'
            f'<td class="theme-membership-cell" title="{theme_labels}">{theme_labels}</td>'
            f'<td class="num">{bottom_date}</td>'
            f'<td class="num neg">{below200_str}</td>'
            f'<td class="num pos">{pole_str}</td>'
            f'<td class="num">{days_str}</td>'
            f'<td class="num">{pullback_str}</td>'
            f'<td class="num {cls_rs0d}">{_ffd(rs0d)}</td>'
            f'<td class="num {cls_rs1}">{_ffd(rs1)}</td>'
            f'<td class="num">{_ffd(comp10, "{:.2f}")}</td>'
            f'<td class="num adr">{_ffd(adr_pct_val, "{:.2f}")}</td>'
            f'</tr>'
        )

    firstflags_table_html = (
        '<table class="watchlist-table tickers-table setups-table" id="firstflags-watchlist">'
        '<thead><tr>'
        '<th data-sort-key="label"       data-sort-type="text">Ticker</th>'
        '<th data-sort-key="theme-label" data-sort-type="text">Theme</th>'
        '<th data-sort-key="bottomdate"  data-sort-type="text" title="Date of the divergence bottom">Bottom</th>'
        '<th data-sort-key="below200"    data-sort-type="num" title="How far below the 200-SMA the bottom closed (%)">B&lt;200%</th>'
        '<th data-sort-key="pole"        data-sort-type="num" title="Move from the bottom low to the highest high since (%)">Pole%</th>'
        '<th data-sort-key="days"        data-sort-type="num" class="sort-active sort-asc" title="Trading days since the bottom (freshest first)">Days</th>'
        '<th data-sort-key="pullback"    data-sort-type="num" title="How far the live close sits below the pole high (%)">Pullback%</th>'
        '<th data-sort-key="rs0d"        data-sort-type="num">0D</th>'
        '<th data-sort-key="rs1"         data-sort-type="num">1d</th>'
        '<th data-sort-key="comp10"      data-sort-type="num">Comp10</th>'
        '<th data-sort-key="adr"         data-sort-type="num">ADR</th>'
        '</tr></thead>'
        f'<tbody id="firstflags-watchlist-body">{"".join(firstflags_body_rows)}</tbody>'
        '</table>'
    )

    # ── Tightening Range table (Setups page, third tab) ────────────────
    # One table holds all D/W/M matches, each tagged data-tighttf; the
    # [D][W][M] buttons sub-filter by timeframe (reusing the filter machinery).
    # Same per-ticker data attrs as the other setups so shared filters apply.
    tighten_body_rows = []
    for m in sorted(tightening_ranges, key=lambda r: r.get("band_adr", 9e9)):
        tk = m["ticker"]; tf = m["tf"]
        p = ticker_packs.get(tk)
        if p is None:
            continue
        rs0d = p["rs0d"]; rs1 = p["rs1"]; rs5 = p["rs5"]; rs20 = p["rs20"]
        rs65 = p["rs65"]; rs130 = p["rs130"]
        comp10 = p.get("comp10"); adr_pct_val = p.get("adr")
        today_adr_ratio = p.get("today_adr_ratio")
        sector = p.get("sector", ""); below_200 = bool(p.get("below_200"))
        theme_labels = ", ".join(themes_by_ticker.get(tk, ["Ungrouped"]))
        theme_ids_attr_val = ",".join(theme_ids_by_ticker.get(tk, ["ungrouped"]))

        def _tra(v, sentinel="-1e9"):
            try: return f"{float(v):.4f}"
            except Exception: return sentinel
        def _trd(v, fmt="{:+.2f}"):
            if v is None: return "—"
            try: return fmt.format(v)
            except Exception: return str(v)

        band = m.get("band_adr"); apex = m.get("bars_to_apex"); span = m.get("wedge_span")
        hi = m.get("res"); lo = m.get("sup")
        comp10_attr = f"{comp10:.4f}" if comp10 is not None else "1e9"
        adr_attr_val = _tra(today_adr_ratio, sentinel="1e9")
        below_cls = " below-200" if below_200 else ""
        cls_rs0d = "pos" if (rs0d is not None and rs0d >= 0) else ("neg" if rs0d is not None else "nul")
        cls_rs1  = "pos" if (rs1  is not None and rs1  >= 0) else ("neg" if rs1  is not None else "nul")

        tighten_body_rows.append(
            f'<tr class="setups-row tickers-row{below_cls}"'
            f' data-row-id="tighten{tf}__{tk}" data-row-kind="setup"'
            f' data-ticker="{tk}" data-label="{tk}" data-theme-label="{theme_labels}"'
            f' data-theme-ids="{theme_ids_attr_val}" data-sector="{sector}"'
            f' data-tighttf="{tf}"'
            f' data-band="{_tra(band)}" data-apex="{_tra(apex)}" data-span="{_tra(span)}"'
            f' data-rangehi="{_tra(hi)}" data-rangelo="{_tra(lo)}"'
            f' data-rs0d="{_tra(rs0d)}" data-rs1="{_tra(rs1)}"'
            f' data-rs5="{_tra(rs5)}" data-rs20="{_tra(rs20)}"'
            f' data-rs65="{_tra(rs65)}" data-rs130="{_tra(rs130)}"'
            f' data-comp10="{comp10_attr}" data-adr="{adr_attr_val}">'
            f'<td class="ticker-symbol-cell"><svg class="tflag-icon tflag-row" viewBox="0 0 12 12" data-flag-ticker="{tk}" title="Flag / unflag {tk}"><polygon points="2,1 10,4 2,7"/></svg><span class="ticker-symbol">{tk}</span></td>'
            f'<td class="theme-membership-cell" title="{theme_labels}">{theme_labels}</td>'
            f'<td class="num">{tf}</td>'
            f'<td class="num">{_trd(lo, "{:.2f}")}</td>'
            f'<td class="num">{_trd(hi, "{:.2f}")}</td>'
            f'<td class="num">{_trd(band, "{:.1f}")}</td>'
            f'<td class="num">{_trd(apex, "{:.0f}")}</td>'
            f'<td class="num">{_trd(span, "{:.0f}")}</td>'
            f'<td class="num {cls_rs0d}">{_trd(rs0d)}</td>'
            f'<td class="num {cls_rs1}">{_trd(rs1)}</td>'
            f'<td class="num">{_trd(comp10, "{:.2f}")}</td>'
            f'<td class="num adr">{_trd(adr_pct_val, "{:.2f}")}</td>'
            f'</tr>'
        )

    tighten_table_html = (
        '<table class="watchlist-table tickers-table setups-table" id="tighten-watchlist">'
        '<thead><tr>'
        '<th data-sort-key="label"       data-sort-type="text">Ticker</th>'
        '<th data-sort-key="theme-label" data-sort-type="text">Theme</th>'
        '<th data-sort-key="tighttf"     data-sort-type="text">TF</th>'
        '<th data-sort-key="rangelo"     data-sort-type="num" title="Support line at the last bar">Range lo</th>'
        '<th data-sort-key="rangehi"     data-sort-type="num" title="Resistance line at the last bar">Range hi</th>'
        '<th data-sort-key="band"        data-sort-type="num" class="sort-active sort-asc" title="Current band width in ADRs (tightest first)">Width</th>'
        '<th data-sort-key="apex"        data-sort-type="num" title="Bars until the lines would meet">Apex</th>'
        '<th data-sort-key="span"        data-sort-type="num" title="Bars in the wedge">Span</th>'
        '<th data-sort-key="rs0d"        data-sort-type="num">0D</th>'
        '<th data-sort-key="rs1"         data-sort-type="num">1d</th>'
        '<th data-sort-key="comp10"      data-sort-type="num">Comp10</th>'
        '<th data-sort-key="adr"         data-sort-type="num">ADR</th>'
        '</tr></thead>'
        f'<tbody id="tighten-watchlist-body">{"".join(tighten_body_rows)}</tbody>'
        '</table>'
    )

    # Filter icon — the 3-horizontal-sliders glyph. Lives in the watchlist
    # controls bar so the toggle is right next to the visible-row counter,
    # not buried in the header chrome. A small gold dot sits in the corner
    # when any exclusions are active.
    filter_icon_svg = (
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="none"'
        ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
        ' stroke-linejoin="round">'
        '<line x1="4" y1="6"  x2="20" y2="6"/>'
        '<circle cx="9"  cy="6"  r="2" fill="currentColor"/>'
        '<line x1="4" y1="12" x2="20" y2="12"/>'
        '<circle cx="15" cy="12" r="2" fill="currentColor"/>'
        '<line x1="4" y1="18" x2="20" y2="18"/>'
        '<circle cx="7"  cy="18" r="2" fill="currentColor"/>'
        '</svg>'
    )
    watchlist_html = (
        '<div class="watchlist-controls">'
        '<label><input type="checkbox" id="toggle-hide-below" checked/> Hide &lt; 200D</label>'
        '<label title="Today candle range &lt; 1.10 × ADR"><input type="checkbox" id="toggle-tight-only"/> Tight D1</label>'
        '<label title="Tickers view only: price within -2.0 to +4.1 ADR of the 50-day SMA"><input type="checkbox" id="toggle-near-50sma"/> Near 50SMA</label>'
        '<label title="Tickers view only: a 30%+ low-to-high run in the last 50 days (momo)"><input type="checkbox" id="toggle-momo"/> Momo</label>'
        '<label title="Show only rows belonging to flagged themes"><input type="checkbox" id="toggle-flagged-only"/> Flagged</label>'
        '<span class="wl-quad-group" title="Hide rows by their rotation quadrant (Chart: theme rows; Tickers: stocks whose themes are all in hidden quadrants)">'
        '<label class="wl-quad-lbl quad-improving"><input type="checkbox" id="wl-quad-improving" data-quad="improving" checked/> Improving</label>'
        '<label class="wl-quad-lbl quad-leading"><input type="checkbox" id="wl-quad-leading" data-quad="leading" checked/> Leading</label>'
        '<label class="wl-quad-lbl quad-weakening"><input type="checkbox" id="wl-quad-weakening" data-quad="weakening" checked/> Weakening</label>'
        '<label class="wl-quad-lbl quad-lagging"><input type="checkbox" id="wl-quad-lagging" data-quad="lagging" checked/> Lagging</label>'
        '</span>'
        '<span class="wl-count" id="wl-visible-count"></span>'
        f'<button type="button" class="filter-icon-btn" id="filter-cell" title="Filter sectors and themes">{filter_icon_svg}'
        '<span class="filter-icon-dot" id="filter-badge"></span>'
        '</button>'
        '</div>'
        '<div class="watchlist-pane themes-pane" id="themes-pane">'
        '<table class="watchlist-table" id="watchlist">'
        '<thead><tr>'
        '<th class="flag-col" id="watchlist-flag-header" title="Right-click for flag options">'
        '<svg class="flag-icon-static" viewBox="0 0 12 12"><polygon points="2,1 10,4 2,7"/></svg>'
        '</th>'
        '<th data-sort-key="label" data-sort-type="text">Theme</th>'
        '<th data-sort-key="rs0d"  data-sort-type="num">0D</th>'
        '<th data-sort-key="rs1"   data-sort-type="num">1d</th>'
        '<th data-sort-key="rs5"   data-sort-type="num" class="sort-active">5d</th>'
        '<th data-sort-key="rs20"  data-sort-type="num">20d</th>'
        '<th data-sort-key="rs65"  data-sort-type="num">65d</th>'
        '<th data-sort-key="rs130" data-sort-type="num">130d</th>'
        '<th data-sort-key="comp"  data-sort-type="num" class="comp-header" title="Left-click to sort; right-click to pick period">Comp <span class="comp-period">10</span></th>'
        '<th data-sort-key="n"     data-sort-type="num">N</th>'
        '</tr></thead>'
        f'<tbody id="watchlist-body">{"".join(watchlist_body_rows)}</tbody>'
        '</table>'
        '</div>'
        '<div class="watchlist-pane tickers-pane" id="tickers-pane" style="display:none">'
        f'{tickers_table_html}'
        '<div class="tickers-empty" id="tickers-empty">No tickers under 1.10 ADR right now.</div>'
        '</div>'
        '<div class="watchlist-pane setups-pane" id="setups-pane" style="display:none">'
        '<div class="setups-tabs" role="tablist">'
        '<button type="button" class="setups-tab is-active" data-setup="extpeek">Extension Peek</button>'
        '<button type="button" class="setups-tab" data-setup="firstflags">First Flags</button>'
        '<button type="button" class="setups-tab" data-setup="tightenrange">Tightening Range</button>'
        '</div>'
        '<div class="setups-content" id="setups-content-extpeek">'
        f'<div class="setups-meta">Extension Peek — {len(extension_peeks)} matches  ·  snapshot asof {peek_asof_date}</div>'
        f'{setups_table_html}'
        '<div class="tickers-empty" id="setups-empty">No Extension Peek matches right now.</div>'
        '</div>'
        '<div class="setups-content" id="setups-content-firstflags" style="display:none">'
        f'<div class="setups-meta">First Flags — {len(first_flags)} matches  ·  snapshot asof {ff_asof_date}</div>'
        f'{firstflags_table_html}'
        '<div class="tickers-empty" id="firstflags-empty">No First Flags matches right now.</div>'
        '</div>'
        '<div class="setups-content" id="setups-content-tightenrange" style="display:none">'
        f'<div class="setups-meta">Tightening Range — converging wedge · {len(tightening_ranges)} across D/W/M · asof {tr_asof_date}</div>'
        '<div class="tighten-tf-tabs">'
        '<button type="button" class="tighten-tf-tab is-active" data-tf="D">Daily</button>'
        '<button type="button" class="tighten-tf-tab" data-tf="W">Weekly</button>'
        '<button type="button" class="tighten-tf-tab" data-tf="M">Monthly</button>'
        '</div>'
        f'{tighten_table_html}'
        '<div class="tickers-empty" id="tighten-empty">No tightening ranges on this timeframe right now.</div>'
        '</div>'
        '</div>'
        '<div class="watchlist-pane candidates-pane" id="candidates-pane" style="display:none">'
        '<div class="candidates-controls">'
        '<input type="text" id="candidates-input" placeholder="Add ticker…" autocomplete="off" spellcheck="false"/>'
        '<button type="button" id="candidates-add-btn">Add</button>'
        '<span class="wl-count" id="candidates-count"></span>'
        '</div>'
        '<div class="candidates-scroll">'
        '<table class="candidates-table" id="candidates-table">'
        '<thead><tr>'
        '<th style="width:24px"></th>'
        '<th>Ticker</th>'
        '<th>Name</th>'
        '<th>Themes</th>'
        '<th>Sector</th>'
        '<th style="text-align:right">Last</th>'
        '<th style="text-align:right">Day%</th>'
        '</tr></thead>'
        '<tbody id="candidates-tbody"></tbody>'
        '</table>'
        '</div>'
        '</div>'
    )

    # ── Append a single ticker-view section to <main>. ──
    # The chart, header strip, and ticker label are filled in by JS the
    # first time a ticker row is focused — we ship one shared container
    # rather than 900+ pre-rendered Plotly sections to keep the HTML
    # under a sane size.
    sections_html.append(
        '<section class="ticker-view" id="__ticker_view__" style="display:none">'
        '<div class="ticker-strip" id="ticker-strip"></div>'
        '<div class="ticker-chart" id="ticker-chart"></div>'
        '<div class="ticker-summary" id="ticker-summary"></div>'
        '</section>'
    )

    # ── Embed per-ticker data + Plotly layout template ──
    # TICKER_DATA carries OHLCV + dates + divergences for every ticker the
    # tree can show. SMAs and MACD are derived in JS to keep the embedded
    # JSON lean. TICKER_LAYOUT is the Plotly layout shared by all per-ticker
    # charts — same panels / spacing / colors as the composite chart.
    import json as _json

    # Pre-compute ext50 series + trendline overlay points per ticker.
    # The series powers the bottom panel; the trendline overlay endpoints
    # come from the snapshot's u1/u2/u3 (descending lines that survived
    # the strict break filter). All coordinates are in (date_string, value)
    # form so the JS can plot directly without further bar→date lookup.
    peek_snap_tickers = (peek_snapshot or {}).get("tickers", {})

    def _build_ext50_chart_data(tk, pack):
        """Return (ext50_series_aligned_to_pack_dates, [trendline_dicts]).

        The series uses the pack's already-truncated date window. Trendline
        endpoints span from anchor_i0 (clipped to the visible window if
        off-chart) through anchor_i1, today, and ~30 bars of forward
        projection so the line continues past the right edge.
        """
        df_full = cache.get(tk)
        if df_full is None:
            return [], []
        ext_full = _compute_ext50_series_for_chart(df_full)
        if ext_full is None:
            return [], []
        # Align to the pack's dates (the chart's visible window).
        n_pack = len(pack["dates"])
        offset = len(df_full) - n_pack
        if offset < 0:
            offset = 0
        ext_window = [
            (None if (i < 0 or i >= len(ext_full) or np.isnan(ext_full[i])) else float(ext_full[i]))
            for i in range(offset, offset + n_pack)
        ]
        # Look up snapshot for this ticker
        snap = peek_snap_tickers.get(tk)
        lines = []
        if snap:
            # Map absolute bar idx -> date string. Use the full df's dates.
            full_dates = [str(d)[:10] for d in df_full["date"].tolist()]
            today_idx = len(full_dates) - 1
            # Project past today by ~30 calendar days. Use the last available
            # date + 30 days as the right endpoint; JS just sees a date string.
            try:
                _last_dt = pd.to_datetime(full_dates[today_idx])
                _right_dt = _last_dt + pd.Timedelta(days=30)
                right_date = _right_dt.strftime("%Y-%m-%d")
                right_bar_offset = 22  # ~22 trading bars in 30 calendar days
            except Exception:
                right_date = full_dates[today_idx]
                right_bar_offset = 0
            for slot_name, slot_list in (("u", snap.get("u") or []), ("l", snap.get("l") or [])):
                for rank, u in enumerate(slot_list[:3], 1):
                    i0 = int(u["i0"]); v0 = float(u["v0"])
                    i1 = int(u["i1"]); v1 = float(u["v1"])
                    slope = float(u["slope"])
                    # Build a list of (date, val) endpoints. Clip i0 to the
                    # chart window start so the line stays on-screen.
                    points = []
                    win_start_idx = offset
                    if i0 < win_start_idx:
                        # Project line back to chart start
                        proj_at_winstart = v1 + slope * (win_start_idx - i1)
                        points.append([full_dates[win_start_idx], proj_at_winstart])
                    else:
                        points.append([full_dates[i0], v0])
                    points.append([full_dates[i1], v1])
                    # Today
                    points.append([full_dates[today_idx], v1 + slope * (today_idx - i1)])
                    # Forward projection past today
                    points.append([right_date, v1 + slope * (today_idx + right_bar_offset - i1)])
                    lines.append({
                        "name": f"{slot_name}{rank}",
                        "slot": slot_name,
                        "rank": rank,
                        "slope": slope,
                        "points": points,
                    })
        return ext_window, lines

    ticker_data_payload = {}
    for tk, p in ticker_packs.items():
        ext_window, lines = _build_ext50_chart_data(tk, p)
        ticker_data_payload[tk] = {
            "dates": p["dates"],
            "open":   p["open"],   "high":   p["high"],
            "low":    p["low"],    "close":  p["close"],
            "volume": p["volume"],
            "ext50":  ext_window,
            "trendlines": lines,
            "rs0d": p["rs0d"],
            "rs1": p["rs1"], "rs3": p["rs3"], "rs5": p["rs5"], "rs20": p["rs20"],
            "rs65": p["rs65"], "rs130": p["rs130"],
            "pct_200": p["pct_200"], "pos_label": p["pos_label"],
            "below_200": p["below_200"],
            "last_close": p["last_close"],
            "day_chg": p["day_chg"], "day_chg_pct": p["day_chg_pct"],
            "vol_last": p["vol_last"],
            "adr": p["adr"], "five_d_return": p["five_d_return"],
            "today_adr_ratio": p["today_adr_ratio"],
            "themes": themes_by_ticker.get(tk, []),
            "long_name": p["long_name"],
            "long_summary": p["long_summary"],
        }
    ticker_data_json = _json.dumps(
        ticker_data_payload,
        separators=(",", ":"), default=lambda o: None,
    )
    ticker_layout_json = _json.dumps(
        build_ticker_layout_template(), separators=(",", ":"), default=str
    )
    # FILTER_DATA powers the live filter panel. JS keeps a Set of unchecked
    # sectors and a Set of unchecked themes; on toggle it walks the rows
    # and hides anything that fails either criterion.
    # Themes sorted alphabetically by label for the filter list. Easier to
    # scan when toggling than the dashboard's 5d-RS sort order.
    _themes_for_filter = [
        {"id": k, "label": THEME_LABELS.get(k, k.replace("_", " ").title()),
         "sector": theme_dominant_sector.get(k, "Unknown")}
        for k in sorted_keys
    ]
    if ungrouped_with_packs:
        _themes_for_filter.append({"id": "ungrouped", "label": "Ungrouped",
                                   "sector": "Unknown"})
    _themes_for_filter.sort(key=lambda t: t["label"].lower())

    filter_data_json = _json.dumps({
        "sectors": available_sectors,
        "industries": available_industries,
        "themes": _themes_for_filter,
        "themeIdsByTicker": theme_ids_by_ticker,
        "tickerSector":   {tk: (p.get("sector")   or "Unknown") for tk, p in ticker_packs.items()},
        "tickerIndustry": {tk: (p.get("industry") or "Unknown") for tk, p in ticker_packs.items()},
        "themeDominantSector":   theme_dominant_sector,
        "themeDominantIndustry": theme_dominant_industry,
    }, separators=(",", ":"), default=str)

    _theme_rank_order = sorted_keys  # ordered best-to-worst by 5d RS
    _theme_labels_map = {k: THEME_LABELS.get(k, k.replace("_", " ").title()) for k in sorted_keys}
    _theme_rs5_map = {
        k: float(theme_pack[k]["rs5_ratio"])
        for k in sorted_keys
        if theme_pack[k]["rs5_ratio"] is not None and theme_pack[k]["rs5_ratio"] > -1e8
    }
    theme_rank_order_json = _json.dumps(_theme_rank_order, separators=(",", ":"))
    theme_labels_map_json = _json.dumps(_theme_labels_map, separators=(",", ":"))
    theme_rs5_map_json    = _json.dumps(_theme_rs5_map,    separators=(",", ":"), default=float)

    # Theme heatmap data: every theme with its RS-vs-Universe ratio in five
    # windows (0d/1d/3d/5d/10d). The Heatmap page colors + sorts by whichever
    # window is toggled; sentinel/None ratios become null (greyed tiles).
    def _hm_val(v):
        return float(v) if (v is not None and v > -1e8) else None
    _heatmap_data = [
        {
            "id": k,
            "label": _theme_labels_map[k],
            "n": len(theme_pack[k]["used"]),
            "rs": {
                "0d":  _hm_val(theme_pack[k]["rs0d_ratio"]),
                "1d":  _hm_val(theme_pack[k]["rs1_ratio"]),
                "3d":  _hm_val(theme_pack[k]["rs3_ratio"]),
                "5d":  _hm_val(theme_pack[k]["rs5_ratio"]),
                "10d": _hm_val(theme_pack[k]["rs10_ratio"]),
            },
        }
        for k in sorted_keys
    ]
    heatmap_data_json = _json.dumps(_heatmap_data, separators=(",", ":"), default=float)

    # ── Narrative Map data (MAP_DATA) — the "Map" sub-view ──────────────
    # Story-space arrangement: every theme placed in its zone, tinted by its
    # strength vs SPY (the research-validated benchmark, NOT the equal-weight
    # universe the other sub-views use). Region headers carry equal-weight-
    # composite strength + money in/out flow. See NARRATIVE_MAP.md.
    _spy_df_map = cache.get("SPY")
    _spy_series_map = None
    if _spy_df_map is not None and len(_spy_df_map):
        _sd = _spy_df_map.copy()
        _sd["date"] = pd.to_datetime(_sd["date"])
        _spy_series_map = _sd.sort_values("date").set_index("date")["close"].astype(float)
        _spy_series_map = _spy_series_map[~_spy_series_map.index.duplicated(keep="last")]

    def _rs_vs_spy(comp_df, n):
        """Simple cumulative-return RS of a composite vs SPY over the last n bars,
        in points (theme return minus SPY return). None if unavailable."""
        if comp_df is None or _spy_series_map is None or len(comp_df) < n + 1:
            return None
        closes = comp_df["close"].values
        c_now, c_then = float(closes[-1]), float(closes[-1 - n])
        if not (c_then > 0):
            return None
        d_now = pd.to_datetime(comp_df["date"].iloc[-1])
        d_then = pd.to_datetime(comp_df["date"].iloc[-1 - n])
        s_now = _spy_series_map.asof(d_now); s_then = _spy_series_map.asof(d_then)
        if s_now != s_now or s_then != s_then or not s_then or float(s_then) <= 0:
            return None
        return ((c_now / c_then) - (float(s_now) / float(s_then))) * 100.0

    # ── Drift-alarm: market-neutral co-movement of each theme vs the AI core,
    # flagging themes whose tape disagrees with their filed zone. Market-neutral
    # (residualize vs SPY first) because raw corr-to-AI tracks market beta and
    # false-flags big names. Threshold is DERIVED from the data — the midpoint
    # between the buildout cohort's median neutral-corr and the noise cohort's —
    # not picked by eye.
    _ai_core_comp, _, _ = build_composite(
        ["NVDA", "AVGO", "AMD", "MU", "CRWV", "NBIS", "VRT", "ANET", "AMAT", "LRCX"], cache, n_bars)
    def _ret_by_date(comp_df):
        if comp_df is None or len(comp_df) < 2:
            return None
        s = pd.Series(np.asarray(comp_df["close"].values, dtype=float),
                      index=pd.to_datetime(comp_df["date"]))
        return s[~s.index.duplicated(keep="last")].sort_index().pct_change()
    _ai_ret = _ret_by_date(_ai_core_comp)
    _spy_ret = _spy_series_map.pct_change() if _spy_series_map is not None else None
    def _neutral_corr(cdf):
        tr = _ret_by_date(cdf)
        if tr is None or _ai_ret is None or _spy_ret is None:
            return None
        d = pd.concat([tr, _ai_ret, _spy_ret], axis=1, keys=["t", "ai", "s"]).dropna()
        if len(d) < 30:
            return None
        d = d.iloc[-63:]
        s = d["s"].values; vs = float(np.var(s))
        if vs <= 0:
            return None
        def _resid(y):
            return y - (np.cov(y, s)[0, 1] / vs) * s
        et, ea = _resid(d["t"].values), _resid(d["ai"].values)
        if np.std(et) == 0 or np.std(ea) == 0:
            return None
        return float(np.corrcoef(et, ea)[0, 1])
    _neu = {}
    for k in sorted_keys:
        if k in NARRATIVE_THEME_SET:
            continue
        _neu[k] = _neutral_corr(theme_pack[k]["composite_df"])
    _bo_v = [v for k, v in _neu.items()
             if THEME_CHAIN_POSITION.get(k) in ("hub", "infrastructure", "power", "materials") and v is not None]
    _no_v = [v for k, v in _neu.items() if THEME_CHAIN_POSITION.get(k) == "noise" and v is not None]
    # Conservative, high-confidence flags only: a name must cross fully into the
    # OPPOSITE cohort's territory (the buildout median / the noise median), not
    # merely past a midpoint. Buildout/Hub are NOT flagged — the physical build
    # is definitionally the AI story, so co-movement can't disconfirm it. Output
    # is the "application-branch" claim the tape CAN disconfirm (SaaS disruption).
    _med_bo = float(np.median(_bo_v)) if _bo_v else 0.30
    _med_no = float(np.median(_no_v)) if _no_v else 0.05
    _drift_flags = {}
    for k, v in _neu.items():
        if v is None:
            continue
        macro = NARRATIVE_ZONES.get(THEME_CHAIN_POSITION.get(k), {}).get("macro", "noise")
        if macro == "output" and v < _med_no:
            _drift_flags[k] = "filed in Output but trades independent of the AI core (corr %.2f < noise median %.2f)" % (v, _med_no)
        elif macro in ("adjacent", "crypto", "noise") and v > _med_bo:
            _drift_flags[k] = "filed off-narrative but trades like the AI buildout (corr %.2f > buildout median %.2f)" % (v, _med_bo)
    if _drift_flags:
        print("\nDRIFT-ALARM (buildout median %.2f, noise median %.2f) — high-confidence zone discrepancies:" % (_med_bo, _med_no))
        for k in sorted(_drift_flags, key=lambda x: _theme_labels_map.get(x, x)):
            print("  [%-13s] %s: %s" % (THEME_CHAIN_POSITION.get(k), _theme_labels_map.get(k, k), _drift_flags[k]))
    else:
        print("\nDRIFT-ALARM — no high-confidence zone discrepancies flagged.")

    # theme nodes (the real themes), placed by zone + tinted vs SPY
    _map_themes = []
    for k in sorted_keys:
        zone = THEME_CHAIN_POSITION.get(k)
        if zone is None:
            continue
        cdf = theme_pack[k]["composite_df"]
        _map_themes.append({
            "id": k, "label": _theme_labels_map[k], "zone": zone,
            "macro": NARRATIVE_ZONES.get(zone, {}).get("macro", "noise"),
            "n": len(theme_pack[k]["used"]),
            "rs5": _rs_vs_spy(cdf, 5), "rs20": _rs_vs_spy(cdf, 20), "rs65": _rs_vs_spy(cdf, 65),
            "narrative": THEME_NARRATIVES.get(k, ""),
            "drift": _drift_flags.get(k),
        })

    # ticker -> zone(s): override straddles; else primary by narrative priority
    def _priority_idx(z):
        try:
            return NARRATIVE_ZONE_PRIORITY.index(z)
        except ValueError:
            return 999
    _ticker_zones = {}
    for tk, th_ids in theme_ids_by_ticker.items():
        if tk in TICKER_ZONE_OVERRIDE:
            zs = [z for z in TICKER_ZONE_OVERRIDE[tk] if z in NARRATIVE_ZONES]
        else:
            cand = sorted({THEME_CHAIN_POSITION[t] for t in th_ids if t in THEME_CHAIN_POSITION},
                          key=_priority_idx)
            zs = cand[:1]
        if zs:
            _ticker_zones[tk] = zs
    for tk in TICKER_ZONE_OVERRIDE:                       # ensure override-only names land
        if tk not in _ticker_zones:
            zs = [z for z in TICKER_ZONE_OVERRIDE[tk] if z in NARRATIVE_ZONES]
            if zs:
                _ticker_zones[tk] = zs
    _straddlers = sorted([tk for tk, zs in _ticker_zones.items() if len(set(zs)) > 1])

    def _tickers_in_zones(zone_set):
        return [tk for tk, zs in _ticker_zones.items() if any(z in zone_set for z in zs)]

    def _region_rs(tickers):
        comp, used, _ = build_composite(list(tickers), cache, n_bars)
        if comp is None:
            return {"rs5": None, "rs20": None, "rs65": None, "n": 0, "flow": "flat"}
        rs5 = _rs_vs_spy(comp, 5); rs20 = _rs_vs_spy(comp, 20); rs65 = _rs_vs_spy(comp, 65)
        flow = "flat"
        if rs5 is not None and rs20 is not None:
            if rs5 >= rs20 and rs5 > 0:
                flow = "in"
            elif rs5 < rs20:
                flow = "out"
        return {"rs5": rs5, "rs20": rs20, "rs65": rs65, "n": len(used), "flow": flow}

    _map_zones = []
    for z, meta in sorted(NARRATIVE_ZONES.items(), key=lambda kv: kv[1].get("order", 99)):
        agg = _region_rs(_tickers_in_zones({z}))
        _map_zones.append({"id": z, "label": meta["label"], "macro": meta["macro"],
                           "order": meta.get("order", 99), **agg})
    _map_macros = []
    for m, meta in sorted(MACROTHEMES.items(), key=lambda kv: kv[1].get("order", 99)):
        zset = {z for z, zm in NARRATIVE_ZONES.items() if zm.get("macro") == m}
        agg = _region_rs(_tickers_in_zones(zset))
        _map_macros.append({"id": m, "label": meta["label"], "order": meta.get("order", 99), **agg})

    _map_data = {"themes": _map_themes, "zones": _map_zones, "macros": _map_macros,
                 "straddlers": _straddlers}
    map_data_json = _json.dumps(_map_data, separators=(",", ":"), default=float)

    # Historical relative-strength series for the Themes "History" view: each
    # theme's composite close ÷ the Universe composite close, aligned by date
    # and normalized to 100 at the theme's first in-window bar. Rising = the
    # theme is outperforming the equal-weight universe. All themes share the
    # universe's date axis (null before a theme's first bar / where missing).
    _uni_close = {}
    for _d, _c in zip(bench_comp_df["date"], bench_comp_df["close"]):
        _c = float(_c)
        if _c == _c and _c > 0:
            _uni_close[str(_d)[:10]] = _c
    # History view embeds the last 130 trading days of FOUR per-theme series; the
    # browser slices to the chosen length (20/65/130), re-indexes the relative
    # ones, smooths, and picks which metric to draw. All four come off each
    # theme's equal-weight composite (the same composite used everywhere else):
    #   rs   = composite close ÷ universe composite close (raw ratio; JS indexes to 100)
    #   move = composite close (raw; JS turns into % change from the window start)
    #   ext  = composite distance from its 50-SMA in ADRs (same metric as ext50)
    #   rvol = composite volume ÷ its 20-bar average volume (1.0 = average)
    HISTORY_BARS = 130
    _history_dates = [str(_d)[:10] for _d in bench_comp_df["date"]][-HISTORY_BARS:]
    _history_series = {}
    for k in sorted_keys:
        _cdf = theme_pack[k]["composite_df"]
        _dates_k = [str(_d)[:10] for _d in _cdf["date"]]
        _idx_k = {ds: i for i, ds in enumerate(_dates_k)}
        _close_k = np.asarray(_cdf["close"].values, dtype=np.float64)
        _vol_k = np.asarray(_cdf["volume"].values, dtype=np.float64)
        _ext_full = _compute_ext50_series_for_chart(_cdf)        # ADR units; None if < 50 bars
        _volsma = sma_2d(_vol_k.reshape(1, -1), 20)[0]
        _rvol_full = _vol_k / np.where(_volsma > 0, _volsma, np.nan)
        _rs = []; _mv = []; _ex = []; _rv = []
        for _ds in _history_dates:
            _i = _idx_k.get(_ds)
            _tc = _close_k[_i] if _i is not None else np.nan
            _uc = _uni_close.get(_ds)
            _rs.append(round(_tc / _uc, 5) if (_i is not None and _uc and _tc == _tc and _tc > 0) else None)
            _mv.append(round(float(_tc), 4) if (_i is not None and _tc == _tc and _tc > 0) else None)
            _ev = _ext_full[_i] if (_ext_full is not None and _i is not None) else np.nan
            _ex.append(round(float(_ev), 3) if (_ev == _ev) else None)
            _rvv = _rvol_full[_i] if _i is not None else np.nan
            _rv.append(round(float(_rvv), 3) if (_rvv == _rvv) else None)
        _history_series[k] = {"rs": _rs, "move": _mv, "ext": _ex, "rvol": _rv}
    history_dates_json  = _json.dumps(_history_dates, separators=(",", ":"))
    history_series_json = _json.dumps(_history_series, separators=(",", ":"), default=float)

    # ── Rotation (RRG) data for the Themes "Rotation" view ──
    # Strength (x) and momentum (y) are standardized CROSS-SECTIONALLY: at each
    # day every theme is z-scored against the OTHER themes that day, not against
    # its own history. Strength = ~20-day relative outperformance vs the universe
    # (log of the theme's RS line today ÷ 20 bars ago — independent of the
    # composite's start-at-100 scale, so it compares across themes); momentum =
    # the 5-day change in that strength. This is what lets sustained leaders sit
    # to the right and volatile rippers land out where they're ripping, instead
    # of each theme normalizing its own move away. Per theme we also carry a
    # fixed-length daily tail (one (x,y) per path day so the scrubber/comets
    # interpolate), quadrant, "mover"/"turn" scores, and per-day breadth + rvol.
    RRG_RSWIN, RRG_MOMSPAN = 20, 5     # 20-bar relative outperformance; 5-bar change = momentum
    RRG_PATH_STEP, RRG_PATH_N, RRG_TURN_LB = 1, 60, 10  # daily path, last 60 days; 10-bar turn/mover lookback
    RRG_MIN_PEERS = 4                  # need ≥ this many themes present on a day to standardize it
    _rrg_dates = [str(_d)[:10] for _d in bench_comp_df["date"]]
    _ND = len(_rrg_dates)
    # Shared daily date axis for the Rotation "Play" animation — the dates at
    # the same bar offsets every theme's path is sampled at (oldest → newest).
    _rotation_dates = []
    for _pj in range(RRG_PATH_N - 1, -1, -1):
        _pidx = _ND - 1 - _pj * RRG_PATH_STEP
        if _pidx >= 0:
            _rotation_dates.append(_rrg_dates[_pidx])
    _rrg_uni = {}
    for _d, _c in zip(bench_comp_df["date"], bench_comp_df["close"]):
        _c = float(_c)
        if _c == _c and _c > 0:
            _rrg_uni[str(_d)[:10]] = _c

    # Pass 1 — per theme, raw (not-yet-standardized) strength + momentum series
    # aligned to _rrg_dates. Standardization is cross-sectional, done in pass 2.
    _themes_raw = []
    for k in sorted_keys:
        _cdf = theme_pack[k]["composite_df"]
        _tcl = {}
        for _d, _c in zip(_cdf["date"], _cdf["close"]):
            _c = float(_c)
            if _c == _c and _c > 0:
                _tcl[str(_d)[:10]] = _c
        _rs = np.array([(_tcl[ds] / _rrg_uni[ds]) if (ds in _tcl and ds in _rrg_uni) else np.nan
                        for ds in _rrg_dates], dtype=np.float64)
        if np.count_nonzero(~np.isnan(_rs)) < RRG_RSWIN + RRG_MOMSPAN + 1:
            continue
        _lrs = np.full(_ND, np.nan)
        _pos = _rs > 0
        _lrs[_pos] = np.log(_rs[_pos])
        _str = np.full(_ND, np.nan)     # 20-bar relative outperformance vs universe
        for i in range(RRG_RSWIN, _ND):
            a, b = _lrs[i], _lrs[i - RRG_RSWIN]
            if a == a and b == b:
                _str[i] = a - b
        _mom = np.full(_ND, np.nan)     # 5-bar change in that outperformance
        for i in range(RRG_MOMSPAN, _ND):
            a, b = _str[i], _str[i - RRG_MOMSPAN]
            if a == a and b == b:
                _mom[i] = a - b
        _themes_raw.append({"k": k, "cdf": _cdf, "str": _str, "mom": _mom})

    # Pass 2 — cross-sectional z per day: for each date, z-score the field of
    # themes that have a finite value (need ≥ RRG_MIN_PEERS) so the spread
    # reflects how a theme stacks up against its peers that day.
    _M = len(_themes_raw)
    _XZ = np.full((_M, _ND), np.nan)
    _YZ = np.full((_M, _ND), np.nan)
    if _M:
        _STR = np.vstack([t["str"] for t in _themes_raw])
        _MOM = np.vstack([t["mom"] for t in _themes_raw])

        def _xsec_z(col):
            out = np.full(len(col), np.nan)
            fin = ~np.isnan(col)
            if np.count_nonzero(fin) >= RRG_MIN_PEERS:
                sd = col[fin].std()
                if sd > 0:
                    out[fin] = (col[fin] - col[fin].mean()) / sd
            return out

        for _t in range(_ND):
            _XZ[:, _t] = _xsec_z(_STR[:, _t])
            _YZ[:, _t] = _xsec_z(_MOM[:, _t])

    # Pass 3 — assemble per-theme dots from the standardized series.
    _rotation_data = []
    for _mi, _tr in enumerate(_themes_raw):
        k = _tr["k"]
        _cdf = _tr["cdf"]
        _xs = _XZ[_mi]
        _ys = _YZ[_mi]
        if np.isnan(_xs[-1]) or np.isnan(_ys[-1]):
            continue
        _xc, _yc = float(_xs[-1]), float(_ys[-1])
        # Fixed-length tail (one slot per _rotation_dates entry; None where the
        # theme lacks a standardized value that far back) so animation frame j
        # aligns with date j across every theme.
        _tail = []
        for j in range(RRG_PATH_N - 1, -1, -1):
            idx = _ND - 1 - j * RRG_PATH_STEP
            if idx >= 0 and not (np.isnan(_xs[idx]) or np.isnan(_ys[idx])):
                _tail.append([round(float(_xs[idx]), 3), round(float(_ys[idx]), 3)])
            else:
                _tail.append(None)
        # mover ("already moving") = net displacement over the last RRG_TURN_LB
        # bars; turn ("just turning") = how much momentum has risen over the same
        # window. Net displacement avoids accumulating daily wiggle.
        _ix2 = _ND - 1 - RRG_TURN_LB
        if _ix2 >= 0 and not (np.isnan(_xs[_ix2]) or np.isnan(_ys[_ix2])):
            _mover = float(((_xc - _xs[_ix2]) ** 2 + (_yc - _ys[_ix2]) ** 2) ** 0.5)
            _turn = float(_yc - _ys[_ix2])
        else:
            _mover = 0.0
            _turn = 0.0
        _quad = ("leading" if (_xc >= 0 and _yc >= 0) else
                 "weakening" if (_xc >= 0 and _yc < 0) else
                 "lagging" if (_xc < 0 and _yc < 0) else "improving")
        # Per-day breadth: % of member stocks above their own 20-day SMA AS OF
        # each path day (participation can lead RS). Built per path day so the
        # Breadth colour/size track the scrubber, not just today.
        _used = theme_pack[k]["used"]
        _mem_series = []
        for _m in _used:
            _mdf = cache.get(_m)
            if _mdf is None or len(_mdf) < 20:
                continue
            _mem_series.append((
                {str(_md)[:10]: _ci for _ci, _md in enumerate(_mdf["date"])},
                np.asarray(_mdf["close"].values, dtype=np.float64),
            ))

        def _breadth_at(_ds):
            _above = _tot = 0
            for _dmap, _cl in _mem_series:
                _bi = _dmap.get(_ds)
                if _bi is None or _bi < 19:
                    continue
                _w = _cl[_bi - 19:_bi + 1]
                if np.any(np.isnan(_w)):
                    continue
                _sma = _w.mean()
                if _sma > 0:
                    _tot += 1
                    if _cl[_bi] > _sma:
                        _above += 1
            return round(_above / _tot, 3) if _tot else None

        _breadthp = [_breadth_at(_ds) for _ds in _rotation_dates]
        _breadth = _breadthp[-1] if _breadthp else None
        # Relative volume per path-day (conviction tracks the scrubber): rvol at a
        # bar = recent 5-day vs prior 25-day composite volume, using bars BEFORE
        # that day (never the partial intraday bar itself). >1 = volume hot.
        _cvol = np.asarray(_cdf["volume"].values, dtype=np.float64)
        _cdmap = {str(_cd2)[:10]: _ci for _ci, _cd2 in enumerate(_cdf["date"])}

        def _rvol_at(_idx):
            if _idx is None or _idx < 25:
                return None
            _r = np.nanmean(_cvol[_idx - 5:_idx])
            _bv = np.nanmean(_cvol[_idx - 25:_idx])
            return round(float(_r / _bv), 3) if (_bv == _bv and _bv > 0 and _r == _r) else None

        _rvolp = [_rvol_at(_cdmap.get(_ds)) for _ds in _rotation_dates]
        _rvol = _rvolp[-1] if _rvolp else None
        # Thrust: how smoothly the theme is climbing UP-AND-RIGHT (toward Leading)
        # over the last ~5 days. = net up-right displacement × path straightness.
        # A clean diagonal climb scores high; a wiggle, or a down/left drift, low.
        # (Δx+Δy is maximal for a balanced 45° move and negative for down-left;
        # straightness = net distance / total path travelled, 0..1, kills the noise.)
        _THR_W = 5
        _tw = [_p for _p in _tail[-(_THR_W + 1):] if _p is not None]
        if len(_tw) >= 2:
            _dxx = _tw[-1][0] - _tw[0][0]
            _dyy = _tw[-1][1] - _tw[0][1]
            _netd = (_dxx * _dxx + _dyy * _dyy) ** 0.5
            _plen = 0.0
            for _pi in range(len(_tw) - 1):
                _plen += ((_tw[_pi + 1][0] - _tw[_pi][0]) ** 2 + (_tw[_pi + 1][1] - _tw[_pi][1]) ** 2) ** 0.5
            _straight = (_netd / _plen) if _plen > 1e-9 else 0.0
            _thrust = (_dxx + _dyy) * _straight
        else:
            _thrust = -1e9
        _rotation_data.append({
            "id": k, "label": _theme_labels_map[k], "n": len(_used),
            "x": round(_xc, 3), "y": round(_yc, 3), "quad": _quad,
            "tail": _tail, "mover": round(_mover, 3), "turn": round(_turn, 3),
            "breadth": _breadth, "breadthp": _breadthp, "rvol": _rvol, "rvolp": _rvolp,
            "thrust": round(_thrust, 4),
        })
    # Rank-percentile every theme by thrust (0 = weakest climb, 1 = strongest) so
    # the top-bar slider can hide the bottom X% off the screen.
    if _rotation_data:
        _by_thr = sorted(range(len(_rotation_data)), key=lambda _j: _rotation_data[_j]["thrust"])
        _thr_denom = max(1, len(_rotation_data) - 1)
        for _ri, _j in enumerate(_by_thr):
            _rotation_data[_j]["thrustRank"] = round(_ri / _thr_denom, 4)
    rotation_data_json = _json.dumps(_rotation_data, separators=(",", ":"), default=float)
    rotation_dates_json = _json.dumps(_rotation_dates, separators=(",", ":"))

    embedded_data_script = (
        f'<script>window.TICKER_DATA = {ticker_data_json};'
        f'window.TICKER_LAYOUT = {ticker_layout_json};'
        f'window.FILTER_DATA = {filter_data_json};'
        f'window.THEME_RANK_ORDER = {theme_rank_order_json};'
        f'window.THEME_LABELS = {theme_labels_map_json};'
        f'window.THEME_RS5 = {theme_rs5_map_json};'
        f'window.HEATMAP_DATA = {heatmap_data_json};'
        f'window.HISTORY_DATES = {history_dates_json};'
        f'window.HISTORY_SERIES = {history_series_json};'
        f'window.ROTATION_DATA = {rotation_data_json};'
        f'window.ROTATION_DATES = {rotation_dates_json};'
        f'window.MAP_DATA = {map_data_json};</script>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Hot Theme Dashboard</title>
<style>{CSS}</style>
<script src="{PLOTLY_CDN}"></script>
<!-- Qt WebChannel bridge: loads only when the page is hosted by QWebEngineView -->
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
{embedded_data_script}
</head>
<body>
<div class="app">
  <header class="rm-fn-bar">
    <div class="rm-fn-brand">
      <span class="rm-status-dot"></span>
      <span class="rm-fn-title">Hot Theme Dashboard</span>
    </div>
    <div class="rm-refresh-cell" id="refresh-cell" role="button" tabindex="0" title="Refresh intraday data (native window only)">
      <span class="rm-label">Generated</span>
      <span class="rm-val" id="refresh-timestamp">{now}</span>
      <span class="rm-refresh-spinner" id="refresh-spinner"></span>
    </div>
    <div>
      <span class="rm-label">Bars</span>
      <span class="rm-val accent">{n_bars}</span>
    </div>
    <div>
      <span class="rm-label">Themes</span>
      <span class="rm-val accent">{n_themes}</span>
    </div>
    <div>
      <span class="rm-label">Cache Last Bar</span>
      <span class="{last_bar_value_cls}">{last_bar_inner}</span>
    </div>
    <div>
      <span class="rm-label">Universe 5d  ·  ADR</span>
      <span class="rm-val {bench_5d_cls}">{bench_5d_str}  ·  {bench_adr:.2f}%</span>
    </div>
    <div class="rm-view-cell" id="view-cell">
      <span class="rm-label">View</span>
      <div class="rm-view-btns">
        <button type="button" class="rm-view-btn" data-tv="chart">Chart</button>
        <button type="button" class="rm-view-btn" data-tv="heatmap">Heatmap</button>
        <button type="button" class="rm-view-btn" data-tv="history">History</button>
        <button type="button" class="rm-view-btn" data-tv="rotation">Rotation</button>
        <button type="button" class="rm-view-btn" data-tv="map">Map</button>
      </div>
    </div>
    <div>
      <span class="rm-label">Showing</span>
      <span class="rm-val accent" id="position-indicator">— / —</span>
    </div>
    <div>
      <span class="rm-label">Navigate</span>
      <span class="rm-h-sub mono">↑↓ rows · →← expand/collapse</span>
    </div>
    <div class="rm-fn-grow">
      <span class="rm-label">Universe</span>
      <span class="rm-h-sub">{universe_summary}</span>
    </div>
  </header>
  <div class="body-grid">
    <aside class="sidebar">
      {watchlist_html}
    </aside>
    <main>
      {''.join(sections_html)}
      <div class="history-page" id="history-page">
        <div class="history-head">
          <span class="history-title" id="history-title">Relative Strength · flagged themes</span>
          <span class="hist-controls">
            <span class="hist-grp">
              <button type="button" class="hist-btn" data-hmetric="rs" title="Composite vs universe, indexed to 100 at the window start">Rel strength</button>
              <button type="button" class="hist-btn" data-hmetric="ext" title="Composite distance from its 50-day SMA, in ADRs">ADR ext</button>
              <button type="button" class="hist-btn" data-hmetric="rvol" title="Composite volume ÷ its 20-bar average (1.0 = average)">RVOL</button>
              <button type="button" class="hist-btn" data-hmetric="move" title="Composite price change from the window start, in %">% move</button>
            </span>
            <span class="hist-grp"><span class="hist-lbl">Bars</span>
              <button type="button" class="hist-btn" data-hbars="20">20</button>
              <button type="button" class="hist-btn" data-hbars="65">65</button>
              <button type="button" class="hist-btn" data-hbars="130">130</button>
            </span>
            <span class="hist-grp" title="Smooth every line with a simple N-day moving average">
              <span class="hist-lbl">Smooth</span>
              <input type="range" class="hist-smooth" id="hist-smooth" min="1" max="10" value="1" step="1"/>
              <span class="hist-smooth-val" id="hist-smooth-val">raw</span>
            </span>
          </span>
          <span class="history-hint">flag themes in the watchlist</span>
        </div>
        <div class="history-chart" id="history-chart"></div>
      </div>
    </main>
  </div>
  <div class="heatmap-page" id="heatmap-page">
    <div class="heatmap-controls">
      <span class="hm-label">RS vs Universe</span>
      <button type="button" class="hm-win-btn" data-win="0d">0d</button>
      <button type="button" class="hm-win-btn" data-win="1d">1d</button>
      <button type="button" class="hm-win-btn" data-win="3d">3d</button>
      <button type="button" class="hm-win-btn" data-win="5d">5d</button>
      <button type="button" class="hm-win-btn" data-win="10d">10d</button>
      <span class="rm-h-sub" style="margin-left:auto">green = beating the universe · red = lagging · click a tile to open the theme</span>
    </div>
    <div class="heatmap-grid" id="heatmap-grid"></div>
    <div class="heatmap-expand" id="heatmap-expand">
      <div class="hm-expand-head">
        <button type="button" class="hm-back-btn" id="hm-back-btn">← Heatmap</button>
        <span class="hm-expand-title" id="hm-expand-title"></span>
        <button type="button" class="hm-viewchart-btn" id="hm-viewchart-btn">View theme chart →</button>
      </div>
      <div class="hm-expand-body" id="hm-expand-body"></div>
    </div>
  </div>
  <div class="rotation-page" id="rotation-page">
    <div class="rotation-controls">
      <span class="rot-grp"><span class="rot-lbl">Emphasis</span>
        <button type="button" class="rot-btn" data-rot="emph" data-val="none">None</button>
        <button type="button" class="rot-btn" data-rot="emph" data-val="movers">Already moving</button>
        <button type="button" class="rot-btn" data-rot="emph" data-val="turns">Just turning</button>
        <button type="button" class="rot-btn" data-rot="emph" data-val="leaders">Leaders</button>
      </span>
      <span class="rot-grp"><span class="rot-lbl">Color</span>
        <button type="button" class="rot-btn" data-rot="color" data-val="quad">Corner</button>
        <button type="button" class="rot-btn" data-rot="color" data-val="breadth">Breadth</button>
      </span>
      <span class="rot-grp"><span class="rot-lbl">Size</span>
        <button type="button" class="rot-btn" data-rot="size" data-val="uniform">Uniform</button>
        <button type="button" class="rot-btn" data-rot="size" data-val="breadth">Breadth</button>
        <button type="button" class="rot-btn" data-rot="size" data-val="n">Theme size</button>
        <button type="button" class="rot-btn" data-rot="size" data-val="rvol">RVOL</button>
      </span>
      <span class="rot-grp">
        <button type="button" class="rot-btn rot-toggle" data-rot="tails">Tails</button>
        <button type="button" class="rot-btn rot-toggle" data-rot="labels">Labels</button>
        <button type="button" class="rot-btn rot-toggle" data-rot="tophalf">Top half</button>
      </span>
      <span class="rot-grp" title="Hide the bottom % of themes by how smoothly they're climbing up-and-right over 5 days">
        <span class="rot-lbl">Thrust</span>
        <input type="range" class="rot-thrust" id="rot-thrust-slider" min="0" max="100" value="0" step="1"/>
        <span class="rot-thrust-val" id="rot-thrust-val">all</span>
      </span>
      <span class="rot-grp">
        <button type="button" class="rot-btn" id="rot-filter-btn" title="Filter out sectors / themes">⚙ Filter</button>
      </span>
    </div>
    <div class="rotation-body">
      <canvas id="rotation-trail-canvas"></canvas>
      <div class="rotation-chart" id="rotation-chart"></div>
      <div class="rot-tooltip" id="rot-tooltip"></div>
    </div>
    <div class="rotation-scrub">
      <span class="rot-scrub-lbl">scrub time</span>
      <input type="range" id="rot-scrub-input" min="0" max="10000" value="10000" step="1"/>
      <span class="rot-scrub-date" id="rot-scrub-date"></span>
    </div>
    <div class="rot-overlay" id="rot-overlay" style="display:none">
      <div class="rot-overlay-panel">
        <div class="rot-overlay-head">
          <span class="rot-overlay-title" id="rot-overlay-title"></span>
          <span class="rot-ovf-bar">
            <button type="button" class="rot-btn rot-toggle" data-ovf="synth" title="Show / hide the big composite chart">Synthetic chart</button>
            <span class="rot-ovf-sep"></span>
            <button type="button" class="rot-btn rot-toggle" data-ovf="momo" title="Show only thumbnails flagged momo (30%+ low&rarr;high run in 50 bars)">Momo</button>
            <button type="button" class="rot-btn rot-toggle" data-ovf="tight" title="Show only thumbnails whose today candle is tight (&lt; 1.10 &times; ADR)">Tight D1</button>
            <span class="rot-ovf-sep"></span>
            <button type="button" class="rot-btn rot-toggle" data-ovf="macd" title="Draw a MACD (6/20/9) panel under each thumbnail">MACD</button>
            <button type="button" class="rot-btn rot-toggle" data-ovf="ext" title="Draw a 50-SMA extension panel (ADR units) under each thumbnail">Extension</button>
          </span>
          <button type="button" class="rot-overlay-close" id="rot-overlay-close" title="close (Esc)">✕</button>
        </div>
        <div class="rot-overlay-body" id="rot-overlay-body"></div>
      </div>
    </div>
  </div>
  <div class="map-page" id="map-page">
    <div class="map-controls">
      <span class="map-label">Narrative Map · strength vs SPY</span>
      <span class="map-win">
        <span class="map-win-lbl">Window</span>
        <button type="button" class="map-win-btn" data-mwin="rs5">5d</button>
        <button type="button" class="map-win-btn" data-mwin="rs20">20d</button>
        <button type="button" class="map-win-btn" data-mwin="rs65">65d</button>
      </span>
      <span class="rm-h-sub" style="margin-left:auto">green = beating SPY · red = lagging · ▲ money in · ▼ out · click a theme for its charts</span>
    </div>
    <div class="map-body" id="map-body"></div>
    <div class="map-expand" id="map-expand">
      <div class="map-expand-head">
        <button type="button" class="map-back-btn" id="map-back-btn">← Map</button>
        <span class="map-expand-title" id="map-expand-title"></span>
        <span class="map-expand-narr" id="map-expand-narr"></span>
        <button type="button" class="map-viewchart-btn" id="map-viewchart-btn">View theme chart →</button>
      </div>
      <div class="map-expand-body" id="map-expand-body"></div>
    </div>
  </div>
  <footer class="rm-statusbar">
    <span><span class="rm-status-dot"></span>LIVE</span>
    <span class="mono">Cache: {spy_last}</span>
    <span class="mono" id="footer-current"></span>
    <span style="flex:1"></span>
    <span class="mono">↑↓ rows · →← tree · Home/End · ScanPerfect Theme Dashboard · {now}</span>
  </footer>
  <div class="filter-panel" id="filter-panel" style="display:none">
    <div class="filter-panel-head">
      <span class="filter-panel-title">Filter</span>
      <a class="filter-reset-link" id="filter-reset-link">Reset all</a>
      <a class="filter-close-link" id="filter-close-link">Close</a>
    </div>
    <div class="filter-panel-body">
      <div class="filter-section">
        <div class="filter-section-head">
          <span class="filter-section-title">Sectors</span>
          <a class="filter-link" data-filter-section="sector" data-filter-action="all">All</a>
          <a class="filter-link" data-filter-section="sector" data-filter-action="none">Clear</a>
        </div>
        <div class="filter-section-list" id="filter-sectors-list"></div>
      </div>
      <div class="filter-section">
        <div class="filter-section-head">
          <span class="filter-section-title">Themes</span>
          <a class="filter-link" data-filter-section="theme" data-filter-action="all">All</a>
          <a class="filter-link" data-filter-section="theme" data-filter-action="none">Clear</a>
        </div>
        <input type="text" class="filter-search" id="filter-themes-search" placeholder="Search themes..." />
        <div class="filter-section-list" id="filter-themes-list"></div>
      </div>
      <div class="filter-section">
        <div class="filter-section-head">
          <span class="filter-section-title">Strength</span>
          <a class="filter-link" data-filter-section="strength" data-filter-action="none">Clear</a>
        </div>
        <div class="filter-section-list" id="filter-strength-list">
          <label title="0D RS &gt;= 1.20"><input type="checkbox" id="toggle-hot-0d"/> Hot 0D</label>
          <label title="5d RS &gt;= 1.20"><input type="checkbox" id="toggle-hot-5"/> Hot 5</label>
          <label title="20d RS &gt;= 1.20"><input type="checkbox" id="toggle-hot-20"/> Hot 20</label>
          <label title="65d RS &gt;= 1.20"><input type="checkbox" id="toggle-hot-65"/> Hot 65</label>
          <label title="130d RS &gt;= 1.20"><input type="checkbox" id="toggle-hot-130"/> Hot 130</label>
          <label title="0D RS &lt; 1.20"><input type="checkbox" id="toggle-cold-0d"/> Cold 0D</label>
          <label title="5d RS &lt; 1.20"><input type="checkbox" id="toggle-cold-5"/> Cold 5</label>
          <label title="20d RS &lt; 1.20"><input type="checkbox" id="toggle-cold-20"/> Cold 20</label>
          <label title="65d RS &lt; 1.20"><input type="checkbox" id="toggle-cold-65"/> Cold 65</label>
          <label title="130d RS &lt; 1.20"><input type="checkbox" id="toggle-cold-130"/> Cold 130</label>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
(function() {{
  var indicator    = document.getElementById('position-indicator');
  var footerCurrent= document.getElementById('footer-current');
  var visibleCount = document.getElementById('wl-visible-count');
  var tbody        = document.getElementById('watchlist-body');
  var tickersBody  = document.getElementById('tickers-watchlist-body');
  var sidebar      = document.querySelector('aside.sidebar');
  var toggleHide   = document.getElementById('toggle-hide-below');
  var brandBtn     = document.querySelector('.rm-fn-brand');
  var brandTitle   = brandBtn && brandBtn.querySelector('.rm-fn-title');
  var viewBtns     = Array.prototype.slice.call(document.querySelectorAll('.rm-view-btn'));
  var tickersEmpty = document.getElementById('tickers-empty');
  var toggleTightOnly = document.getElementById('toggle-tight-only');
  var toggleNear50 = document.getElementById('toggle-near-50sma');
  var toggleMomo = document.getElementById('toggle-momo');
  var toggleFlaggedOnly = document.getElementById('toggle-flagged-only');
  // Rotation-quadrant filter (Chart-view theme rows). themeQuad maps theme id →
  // its current RRG quadrant (from the rotation computation), so the watchlist
  // can hide rows by quadrant and tint each theme name. wlQuadBoxes are the four
  // checkboxes; default all checked (all quadrants shown).
  var wlQuadBoxes = {{
    improving: document.getElementById('wl-quad-improving'),
    leading:   document.getElementById('wl-quad-leading'),
    weakening: document.getElementById('wl-quad-weakening'),
    lagging:   document.getElementById('wl-quad-lagging')
  }};
  var themeQuad = {{}};
  (window.ROTATION_DATA || []).forEach(function(d) {{ if (d && d.id && d.quad) themeQuad[d.id] = d.quad; }});
  // Flagged theme set — persisted in localStorage. Click a flag icon to
  // toggle. "Flagged" checkbox filters all views to rows that belong to
  // at least one flagged theme.
  var flaggedThemes = new Set();
  try {{
    var savedFlags = window.localStorage && window.localStorage.getItem('themeDashboard.flaggedThemes');
    if (savedFlags) JSON.parse(savedFlags).forEach(function(id) {{ flaggedThemes.add(id); }});
  }} catch(e) {{}}
  // Hot N filters live in the filter panel — one checkbox per RS window.
  // Each is independent; row passes if rsN >= 1.20 for AT LEAST ONE of
  // the checked windows (OR semantics). Theme rows have all 4 windows;
  // flat ticker rows only have rs65/rs130 (no rs5/rs20), so Hot 5 / Hot 20
  // simply skip ticker rows for those windows.
  var hotToggles = {{
    '0d': document.getElementById('toggle-hot-0d'),
    5:    document.getElementById('toggle-hot-5'),
    20:   document.getElementById('toggle-hot-20'),
    65:   document.getElementById('toggle-hot-65'),
    130:  document.getElementById('toggle-hot-130'),
  }};
  // Parallel "Cold" set — same windows, opposite condition. AND across all
  // checked Cold boxes, AND with the checked Hot boxes. Mixing is allowed:
  // Hot 0D + Cold 65 = intraday-strong, quarter-weak.
  var coldToggles = {{
    '0d': document.getElementById('toggle-cold-0d'),
    5:    document.getElementById('toggle-cold-5'),
    20:   document.getElementById('toggle-cold-20'),
    65:   document.getElementById('toggle-cold-65'),
    130:  document.getElementById('toggle-cold-130'),
  }};

  // Filter thresholds. "Hot N" = rsN >= 1.20 in the named window.
  // "Tight" = today_adr_ratio < 1.10. Persisted in localStorage.
  var HOT_RS_THRESHOLD   = 1.20;
  var TIGHT_ADR_CEILING  = 1.10;
  // "Near 50SMA" band: ext50 (ADRs from the 50-day SMA) within [lo, hi].
  var NEAR50_LO = -2.0;
  var NEAR50_HI = 4.1;
  try {{
    Object.keys(hotToggles).forEach(function(n) {{
      var key = 'themeDashboard.hot' + n;
      var saved = window.localStorage && window.localStorage.getItem(key);
      if (saved === '1' && hotToggles[n]) hotToggles[n].checked = true;
    }});
    Object.keys(coldToggles).forEach(function(n) {{
      var key = 'themeDashboard.cold' + n;
      var saved = window.localStorage && window.localStorage.getItem(key);
      if (saved === '1' && coldToggles[n]) coldToggles[n].checked = true;
    }});
    var savedTight = window.localStorage && window.localStorage.getItem('themeDashboard.tightOnly');
    if (savedTight === '1' && toggleTightOnly) toggleTightOnly.checked = true;
    var savedNear50 = window.localStorage && window.localStorage.getItem('themeDashboard.near50sma');
    if (savedNear50 === '1' && toggleNear50) toggleNear50.checked = true;
    var savedMomo = window.localStorage && window.localStorage.getItem('themeDashboard.momoOnly');
    if (savedMomo === '1' && toggleMomo) toggleMomo.checked = true;
    var savedFlagged = window.localStorage && window.localStorage.getItem('themeDashboard.flaggedOnly');
    if (savedFlagged === '1' && toggleFlaggedOnly) toggleFlaggedOnly.checked = true;
    Object.keys(wlQuadBoxes).forEach(function(q) {{
      var savedQ = window.localStorage && window.localStorage.getItem('themeDashboard.wlQuad.' + q);
      if (savedQ === '0' && wlQuadBoxes[q]) wlQuadBoxes[q].checked = false;  // default on
    }});
  }} catch(e) {{}}

  // View state: 'themes' (default tree) or 'tickers' (flat ADR-tight list).
  // Persisted in localStorage so refresh keeps the active view.
  var activeView = 'themes';
  var themesView = 'chart';
  try {{
    var savedTV = window.localStorage && window.localStorage.getItem('themeDashboard.themesView');
    if (savedTV === 'chart' || savedTV === 'heatmap' || savedTV === 'history' || savedTV === 'rotation' || savedTV === 'map') themesView = savedTV;
  }} catch(e) {{}}
  try {{
    var saved = window.localStorage && window.localStorage.getItem('themeDashboard.view');
    if (saved === 'tickers' || saved === 'themes' || saved === 'setups') activeView = saved;
  }} catch(e) {{}}

  // Sort state — applies to theme rows globally AND ticker children within each parent.
  // Tickers view keeps its own sort state independent of the tree.
  var sortKey  = 'rs5';
  var sortDir  = -1;  // -1 desc, 1 asc
  var sortType = 'num';
  var tkSortKey  = 'rs0d';
  var tkSortDir  = -1;
  var tkSortType = 'num';

  // Compression column: state is shared by both tables. Period (3/5/10/20/30)
  // selected via right-click on the Comp header; sort direction toggled via
  // left-click like every other column. Default = 10 bars.
  var currentCompPeriod = 10;
  try {{
    var savedComp = window.localStorage && window.localStorage.getItem('themeDashboard.compPeriod');
    if (savedComp && ['3','5','10','20','30'].indexOf(savedComp) >= 0) {{
      currentCompPeriod = parseInt(savedComp, 10);
    }}
  }} catch(e) {{}}

  // ── Row utilities ──────────────────────────────────────────
  function rows()        {{ return Array.prototype.slice.call(tbody.querySelectorAll('tr.watchlist-row')); }}
  function themeRows()   {{ return rows().filter(function(r) {{ return r.dataset.rowKind === 'theme'; }}); }}
  function tickerRows()  {{ return rows().filter(function(r) {{ return r.dataset.rowKind === 'ticker'; }}); }}
  function childrenOf(themeRow) {{
    return tickerRows().filter(function(r) {{ return r.dataset.themeId === themeRow.dataset.themeId; }});
  }}
  function visibleRows() {{
    if (activeView === 'tickers') {{
      return tickerFlatRows().filter(function(r) {{
        var s = window.getComputedStyle(r);
        return s.display !== 'none';
      }});
    }}
    if (activeView === 'setups') {{
      return setupsFlatRows().filter(function(r) {{
        var s = window.getComputedStyle(r);
        return s.display !== 'none';
      }});
    }}
    return rows().filter(function(r) {{
      if (r.style.display === 'none') return false;
      if (r.classList.contains('hidden-by-filter')) return false;
      if (r.classList.contains('child-collapsed')) return false;
      return true;
    }});
  }}
  function tickerFlatRows() {{
    if (!tickersBody) return [];
    return Array.prototype.slice.call(tickersBody.querySelectorAll('tr.tickers-row'));
  }}

  // ── Sort: theme rows globally, ticker children within their parent. ──
  function applyHeaderIndicators() {{
    document.querySelectorAll('#watchlist th').forEach(function(th) {{
      th.classList.remove('sort-active', 'sort-asc');
    }});
    var active = document.querySelector('#watchlist th[data-sort-key="' + sortKey + '"]');
    if (active) {{
      active.classList.add('sort-active');
      if (sortDir === 1) active.classList.add('sort-asc');
    }}
  }}

  function compareRows(a, b) {{
    var av, bv;
    if (sortType === 'num') {{
      // 'comp' is a virtual sort key — resolve to comp{{N}} for the active
      // period. Missing values use 1e9 so they sort to the bottom when
      // ascending (tightest first).
      var key = (sortKey === 'comp') ? ('comp' + currentCompPeriod) : sortKey;
      var miss = (sortKey === 'comp') ? '1e9' : '-1e9';
      av = parseFloat(a.dataset[key] || miss);
      bv = parseFloat(b.dataset[key] || miss);
      if (isNaN(av)) av = parseFloat(miss);
      if (isNaN(bv)) bv = parseFloat(miss);
      return (av - bv) * sortDir;
    }} else {{
      av = (a.dataset.label || '').toLowerCase();
      bv = (b.dataset.label || '').toLowerCase();
      return av.localeCompare(bv) * sortDir;
    }}
  }}

  function sortRows() {{
    // Sort themes globally; for each theme, sort its children, then
    // re-append theme + children in that order. Tickers always stay grouped
    // under their parent — sort never breaks the hierarchy.
    var themes = themeRows().slice();
    themes.sort(compareRows);
    themes.forEach(function(theme) {{
      tbody.appendChild(theme);
      var kids = childrenOf(theme).slice();
      kids.sort(compareRows);
      kids.forEach(function(kid) {{ tbody.appendChild(kid); }});
    }});
    applyHeaderIndicators();
  }}

  // ── Filter: hide rows below their own 200D. ────────────────
  // Applies independently to theme rows, ticker child rows (inside the
  // tree), and ticker-flat rows (Tickers view). Each row checks its own
  // below-200 flag. All show/hide is driven by inline `display` on the
  // row; no CSS default-hide. When a filter is unchecked, applyFilter()
  // clears the inline style so rows revert to their normal visibility.
  function rowFailsHotOrTight(r) {{
    // "Hot N" = rsN >= 1.20. AND semantics across checked windows: the
    // row must pass EVERY checked Hot N. If no Hot box is checked, this
    // branch is skipped entirely (no Hot filtering). Missing values use
    // -1e9 as sentinel, which always fails the threshold.
    // "Cold N" = rsN < 1.20. Same AND semantics, applied independently
    // and combined with the Hot set — mixing is allowed (e.g. Hot 0D +
    // Cold 65 picks intraday-strong, quarter-weak). Missing values use
    // -1e9 sentinel which trivially passes Cold (we'd rather show a row
    // with missing data than hide it from a weakness scan).
    var hotColdWindows = ['0d', 5, 20, 65, 130];
    for (var i = 0; i < hotColdWindows.length; i++) {{
      var n = hotColdWindows[i];
      var hotTog = hotToggles[n];
      if (hotTog && hotTog.checked) {{
        var hv = parseFloat(r.dataset['rs' + n] || '-1e9');
        if (isNaN(hv) || hv < HOT_RS_THRESHOLD) return true;
      }}
      var coldTog = coldToggles[n];
      if (coldTog && coldTog.checked) {{
        var cv = parseFloat(r.dataset['rs' + n]);
        if (!isNaN(cv) && cv >= HOT_RS_THRESHOLD) return true;
      }}
    }}
    // "Tight D1" = today's candle range / 20d ADR < 1.10. Missing data
    // (no ADR, fresh IPO, etc.) gets a sentinel of 1e9 which fails.
    if (toggleTightOnly && toggleTightOnly.checked) {{
      var adr = parseFloat(r.dataset.adr || '1e9');
      if (isNaN(adr) || adr >= TIGHT_ADR_CEILING) return true;
    }}
    // "Near 50SMA" = ext50 within [-2.0, +4.1]. Tickers view ONLY — the theme
    // page (theme rows + their tree children) and the Setups view are exempt.
    // Missing ext50 (< 50-bar IPO) is NaN, which fails — we hide what we can't
    // confirm is near the 50.
    if (toggleNear50 && toggleNear50.checked && r.dataset.rowKind === 'ticker-flat') {{
      var ext = parseFloat(r.dataset.ext50);
      if (isNaN(ext) || ext < NEAR50_LO || ext > NEAR50_HI) return true;
    }}
    // "Momo" = a >=30% low-to-high run in the last 50 bars. Tickers view ONLY
    // (the green-circle badge marks these on the theme/rotation thumbnails).
    if (toggleMomo && toggleMomo.checked && r.dataset.rowKind === 'ticker-flat') {{
      if (r.dataset.momo !== '1') return true;
    }}
    // Tightening Range timeframe sub-toggle: hide tighten rows whose timeframe
    // isn't the active one. Only tighten rows carry data-tighttf, so this is a
    // no-op for every other row.
    if (r.dataset.tighttf && r.dataset.tighttf !== activeTightenTF) return true;
    // "Flagged only" = row must belong to at least one flagged theme.
    // Theme rows use their own theme-id; ticker child rows use their parent
    // theme-id; flat ticker rows use their comma-separated theme-ids attr
    // (a flat ticker can belong to multiple themes — pass if ANY is flagged).
    if (toggleFlaggedOnly && toggleFlaggedOnly.checked) {{
      if (flaggedThemes.size === 0) return true;
      var ids = [];
      if (r.dataset.rowKind === 'theme')        ids = [r.dataset.themeId];
      else if (r.dataset.rowKind === 'ticker')  ids = [r.dataset.themeId];
      else if (r.dataset.themeIds)              ids = r.dataset.themeIds.split(',');
      var matched = false;
      for (var j = 0; j < ids.length; j++) {{
        if (flaggedThemes.has(ids[j])) {{ matched = true; break; }}
      }}
      if (!matched) return true;
    }}
    return false;
  }}

  function applyFilter() {{
    var hideBelow = toggleHide && toggleHide.checked;
    var applyTo = function(r) {{
      var below = r.classList.contains('below-200');
      var filteredOut = r.classList.contains('filtered-out');
      var hotTight   = rowFailsHotOrTight(r);
      var hide = filteredOut || (hideBelow && below) || hotTight;
      r.style.display = hide ? 'none' : '';
    }};
    rows().forEach(applyTo);
    tickerFlatRows().forEach(applyTo);
    setupsFlatRows().forEach(applyTo);
    if (visibleCount) visibleCount.textContent = visibleRows().length + ' visible';
    // Setups empty-state notice
    var setupsEmpty = document.getElementById(SETUP_TABLES[activeSetup].empty);
    if (setupsEmpty && activeView === 'setups') {{
      var anyVisible = setupsFlatRows().some(function(r) {{ return r.style.display !== 'none'; }});
      setupsEmpty.style.display = anyVisible ? 'none' : 'block';
    }}
  }}

  // ── Tree expand / collapse ─────────────────────────────────
  function isExpanded(themeRow)   {{ return themeRow.dataset.expanded === '1'; }}
  function setExpanded(themeRow, on) {{
    themeRow.dataset.expanded = on ? '1' : '0';
    var kids = childrenOf(themeRow);
    kids.forEach(function(kid) {{
      kid.classList.toggle('child-collapsed', !on);
    }});
    var caret = themeRow.querySelector('.tree-caret');
    if (caret) caret.textContent = on ? '▾' : '▸';
    if (visibleCount) visibleCount.textContent = visibleRows().length + ' visible';
  }}

  function expandTheme(themeRow)   {{ if (themeRow && themeRow.dataset.rowKind === 'theme' && !isExpanded(themeRow)) setExpanded(themeRow, true); }}
  function collapseTheme(themeRow) {{ if (themeRow && themeRow.dataset.rowKind === 'theme' &&  isExpanded(themeRow)) setExpanded(themeRow, false); }}

  // ── Per-ticker chart rendering ─────────────────────────────
  // SMA: NaN until `period` consecutive valid bars seen in the window;
  // matches Python sma_2d (mirrors pandas min_periods=period).
  function smaJS(arr, p) {{
    var out = new Array(arr.length);
    var sum = 0, count = 0;
    for (var i = 0; i < arr.length; i++) {{
      var v = arr[i];
      if (v !== null && v !== undefined && !isNaN(v)) {{ sum += v; count++; }}
      if (i >= p) {{
        var old = arr[i - p];
        if (old !== null && old !== undefined && !isNaN(old)) {{ sum -= old; count--; }}
      }}
      out[i] = (i >= p - 1 && count === p) ? sum / p : null;
    }}
    return out;
  }}
  // EMA: seed at first valid bar, alpha=2/(p+1); NaN until p valid bars seen.
  function emaJS(arr, p) {{
    var out = new Array(arr.length);
    var alpha = 2 / (p + 1);
    var ema = null;
    var count = 0;
    for (var i = 0; i < arr.length; i++) {{
      var v = arr[i];
      if (v === null || v === undefined || isNaN(v)) {{ out[i] = null; continue; }}
      ema = (ema === null) ? v : (alpha * v + (1 - alpha) * ema);
      count++;
      out[i] = (count >= p) ? ema : null;
    }}
    return out;
  }}
  function subArr(a, b) {{
    var out = new Array(a.length);
    for (var i = 0; i < a.length; i++) {{
      out[i] = (a[i] === null || b[i] === null) ? null : a[i] - b[i];
    }}
    return out;
  }}

  function fmtNum(v, dec, sign) {{
    if (v === null || v === undefined || isNaN(v)) return '—';
    dec = (dec === undefined) ? 2 : dec;
    return ((sign && v >= 0) ? '+' : '') + v.toFixed(dec);
  }}
  function fmtPct(v, dec, sign) {{
    if (v === null || v === undefined || isNaN(v)) return '—';
    dec = (dec === undefined) ? 2 : dec;
    return ((sign && v >= 0) ? '+' : '') + v.toFixed(dec) + '%';
  }}
  function fmtVol(v) {{
    if (v === null || v === undefined || isNaN(v)) return '—';
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K';
    return v.toFixed(0);
  }}
  function clsForVal(v) {{ return (v !== null && v >= 0) ? 'pos' : 'neg'; }}

  function buildTickerStrip(ticker) {{
    var d = window.TICKER_DATA[ticker];
    var strip = document.getElementById('ticker-strip');
    if (!d || !strip) return;
    var pos200cls = d.below_200 ? 'neg' : 'pos';
    var html = '';
    html += '<span class="ticker-name">' + ticker + '</span>';
    if (d.long_name) html += '<span class="long-name">' + d.long_name + '</span>';
    html += '<span class="sep">|</span>';
    html += '<span><span class="lbl">C</span><span class="val">' + fmtNum(d.last_close) + '</span></span>';
    html += '<span><span class="lbl">Chg</span><span class="' + clsForVal(d.day_chg) + '">' + fmtNum(d.day_chg, 2, true) + '</span> '
         +  '<span class="' + clsForVal(d.day_chg_pct) + '">' + fmtPct(d.day_chg_pct, 2, true) + '</span></span>';
    html += '<span><span class="lbl">Vol</span><span class="val">' + fmtVol(d.vol_last) + '</span></span>';
    html += '<span class="sep">|</span>';
    html += '<span><span class="lbl">vs 200D</span><span class="' + pos200cls + '">' + fmtPct(d.pct_200, 1, true) + '</span></span>';
    html += '<span><span class="lbl">5d ret</span><span class="' + clsForVal(d.five_d_return) + '">' + fmtPct(d.five_d_return, 2, true) + '</span></span>';
    html += '<span><span class="lbl">ADR</span><span class="val">' + fmtPct(d.adr) + '</span></span>';
    html += '<span class="sep">|</span>';
    html += '<span><span class="lbl">0D RS</span><span class="' + clsForVal(d.rs0d) + '">' + fmtNum(d.rs0d, 2, true) + 'x</span></span>';
    html += '<span><span class="lbl">1d RS</span><span class="' + clsForVal(d.rs1) + '">' + fmtNum(d.rs1, 2, true) + 'x</span></span>';
    html += '<span><span class="lbl">5d RS</span><span class="' + clsForVal(d.rs5) + '">' + fmtNum(d.rs5, 2, true) + 'x</span></span>';
    html += '<span><span class="lbl">20d RS</span><span class="' + clsForVal(d.rs20) + '">' + fmtNum(d.rs20, 2, true) + 'x</span></span>';
    // MACD divergence tags removed 2026-05-22.
    strip.innerHTML = html;
    var sumDiv = document.getElementById('ticker-summary');
    if (sumDiv) sumDiv.textContent = d.long_summary || '';
  }}

  var currentChartTicker = null;
  function renderTicker(ticker) {{
    var d = window.TICKER_DATA[ticker];
    var div = document.getElementById('ticker-chart');
    if (!d || !div || !window.Plotly) return;
    if (ticker === currentChartTicker) return;  // already drawn for this ticker — skip the redraw

    var dates = d.dates;
    var close = d.close;
    var sma5   = smaJS(close, 5);
    var sma10  = smaJS(close, 10);
    var sma20  = smaJS(close, 20);
    var sma50  = smaJS(close, 50);
    var sma200 = smaJS(close, 200);
    var emaFast = emaJS(close, 6);
    var emaSlow = emaJS(close, 20);
    var macdLine = subArr(emaFast, emaSlow);
    var signal   = emaJS(macdLine, 9);

    var volColors = [];
    for (var i = 0; i < close.length; i++) {{
      var up = (close[i] !== null && d.open[i] !== null && close[i] >= d.open[i]);
      volColors.push(up ? '#1eff1e' : '#ff3030');
    }}

    var traces = [
      {{ type: 'candlestick', x: dates, open: d.open, high: d.high, low: d.low, close: d.close,
         increasing: {{ line: {{ color: '#1eff1e', width: 1 }}, fillcolor: '#1eff1e' }},
         decreasing: {{ line: {{ color: '#ff3030', width: 1 }}, fillcolor: '#ff3030' }},
         showlegend: false, name: '', xaxis: 'x', yaxis: 'y',
         hoverlabel: {{ font: {{ family: 'Consolas, monospace', size: 11 }} }} }},
      {{ type: 'scatter', x: dates, y: sma5,   mode: 'lines', line: {{color:'#ff8800', width:1.2}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y', name: 'SMA 5' }},
      {{ type: 'scatter', x: dates, y: sma10,  mode: 'lines', line: {{color:'#5fc8ff', width:1.2}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y', name: 'SMA 10' }},
      {{ type: 'scatter', x: dates, y: sma20,  mode: 'lines', line: {{color:'#e8c890', width:1.2}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y', name: 'SMA 20' }},
      {{ type: 'scatter', x: dates, y: sma50,  mode: 'lines', line: {{color:'#ffcc00', width:1.2}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y', name: 'SMA 50' }},
      {{ type: 'scatter', x: dates, y: sma200, mode: 'lines', line: {{color:'#ffffff', width:1.5}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y', name: 'SMA 200' }},
      {{ type: 'bar', x: dates, y: d.volume, marker: {{ color: volColors, line: {{ width: 0 }} }}, showlegend: false, hoverinfo: 'skip', xaxis: 'x2', yaxis: 'y2', name: '' }},
      {{ type: 'scatter', x: dates, y: macdLine, mode: 'lines', line: {{color:'#5fc8ff', width:1.6}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x3', yaxis: 'y3', name: 'MACD' }},
      {{ type: 'scatter', x: dates, y: signal,   mode: 'lines', line: {{color:'#ff8800', width:1.6}}, showlegend: false, hoverinfo: 'skip', xaxis: 'x3', yaxis: 'y3', name: 'Signal' }}
    ];

    // MACD divergences were removed 2026-05-22 — no overlay traces drawn.

    // ── ext50 panel: histogram of the 50-SMA extension + descending
    //    trendline overlays from the snapshot. Bars colored green if the
    //    ext value is positive (price above SMA50), red if negative.
    if (d.ext50 && d.ext50.length) {{
      var extColors = d.ext50.map(function(v) {{
        if (v === null || v === undefined || isNaN(v)) return 'rgba(0,0,0,0)';
        return v >= 0 ? '#1eff1e' : '#ff3030';
      }});
      traces.push({{
        type: 'bar', x: dates, y: d.ext50,
        marker: {{ color: extColors, line: {{ width: 0 }} }},
        showlegend: false, hoverinfo: 'skip',
        xaxis: 'x4', yaxis: 'y4', name: 'ext50'
      }});
    }}
    // Trendline overlays. Colors: u-slot in white (descending resistance),
    // l-slot in dim cyan (ascending support). Rank 1 thickest.
    if (d.trendlines && d.trendlines.length) {{
      d.trendlines.forEach(function(tl) {{
        if (!tl.points || tl.points.length < 2) return;
        var lineX = tl.points.map(function(p) {{ return p[0]; }});
        var lineY = tl.points.map(function(p) {{ return p[1]; }});
        var color = (tl.slot === 'u') ? '#ffffff' : '#5fc8ff';
        var width = (tl.rank === 1) ? 2.0 : (tl.rank === 2 ? 1.2 : 0.8);
        var dash  = (tl.rank === 1) ? 'solid' : (tl.rank === 2 ? 'dot' : 'dashdot');
        traces.push({{
          type: 'scatter', x: lineX, y: lineY,
          mode: 'lines', line: {{ color: color, width: width, dash: dash }},
          showlegend: false, hoverinfo: 'skip',
          xaxis: 'x4', yaxis: 'y4', name: tl.name
        }});
      }});
    }}

    // Clone the layout template and set the x-range with weekend padding.
    var layout = JSON.parse(JSON.stringify(window.TICKER_LAYOUT));
    var firstDate = dates[0];
    var lastDate  = dates[dates.length - 1];
    var lastDt = new Date(lastDate + 'T00:00:00');
    lastDt.setDate(lastDt.getDate() + 30);
    var rightPad = lastDt.toISOString().slice(0, 10);
    if (!layout.xaxis)  layout.xaxis  = {{}};
    if (!layout.xaxis2) layout.xaxis2 = {{}};
    if (!layout.xaxis3) layout.xaxis3 = {{}};
    if (!layout.xaxis4) layout.xaxis4 = {{}};
    layout.xaxis.range  = [firstDate, rightPad];
    layout.xaxis2.range = [firstDate, rightPad];
    layout.xaxis3.range = [firstDate, rightPad];
    layout.xaxis4.range = [firstDate, rightPad];

    // Ticker label annotation (top-left of candle panel)
    layout.annotations = (layout.annotations || []).slice();
    layout.annotations.push({{
      text: '<b>' + ticker + ', D</b>',
      xref: 'paper', yref: 'paper',
      x: 0.008, y: 0.98, xanchor: 'left', yanchor: 'top',
      showarrow: false,
      font: {{ family: 'Segoe UI, Tahoma, sans-serif', size: 34, color: '#ffffff' }}
    }});
    if (d.long_name) {{
      layout.annotations.push({{
        text: d.long_name,
        xref: 'paper', yref: 'paper',
        x: 0.008, y: 0.90, xanchor: 'left', yanchor: 'top',
        showarrow: false,
        font: {{ family: 'Segoe UI, Tahoma, sans-serif', size: 12, color: '#c8ccd2' }}
      }});
    }}

    Plotly.newPlot(div, traces, layout, {{ displayModeBar: false, scrollZoom: true, doubleClick: 'reset' }});
    currentChartTicker = ticker;
  }}

  // ── Section nav ────────────────────────────────────────────
  var activeRowId = null;
  var tickerView = document.getElementById('__ticker_view__');

  function showThemeSection(themeId) {{
    document.querySelectorAll('section.theme').forEach(function(s) {{
      s.style.display = (s.id === themeId) ? '' : 'none';
      s.classList.toggle('is-active', s.id === themeId);
    }});
    if (tickerView) tickerView.style.display = 'none';
    var sec = document.getElementById(themeId);
    var pdiv = sec && sec.querySelector('.plotly-graph-div');
    if (pdiv && window.Plotly && Plotly.Plots && Plotly.Plots.resize) {{
      try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}}
    }}
  }}

  // Defer the heavy Plotly redraw so the tab/row switch paints immediately;
  // the chart fills in on the next tick. Rapid navigation collapses to one
  // redraw of the final ticker (single timer in flight).
  var pendingChartTicker = null;
  var chartRenderTimer = null;
  function scheduleTickerRender(ticker) {{
    pendingChartTicker = ticker;
    if (chartRenderTimer) return;
    chartRenderTimer = setTimeout(function() {{
      chartRenderTimer = null;
      renderTicker(pendingChartTicker);
    }}, 0);
  }}
  function showTickerSection(ticker) {{
    document.querySelectorAll('section.theme').forEach(function(s) {{ s.style.display = 'none'; }});
    if (tickerView) tickerView.style.display = '';
    buildTickerStrip(ticker);          // cheap → instant feedback
    scheduleTickerRender(ticker);      // heavy chart → next tick, after the switch paints
  }}

  // Setups page hosts multiple setup types, one per tab. The shared setups
  // helpers (sort / filter / nav) always operate on the ACTIVE table; we just
  // repoint setupsBody + setupsTableId when a tab is clicked.
  var SETUP_TABLES = {{
    extpeek:     {{ body:'setups-watchlist-body',     table:'setups-watchlist',     empty:'setups-empty',     content:'setups-content-extpeek',     label:'EXTENSION PEEK',   defSort:'peek', defDir:1 }},
    firstflags:  {{ body:'firstflags-watchlist-body', table:'firstflags-watchlist', empty:'firstflags-empty', content:'setups-content-firstflags', label:'FIRST FLAGS',      defSort:'days', defDir:1 }},
    tightenrange:{{ body:'tighten-watchlist-body',    table:'tighten-watchlist',    empty:'tighten-empty',   content:'setups-content-tightenrange', label:'TIGHTENING RANGE', defSort:'band', defDir:1 }}
  }};
  var activeSetup = 'extpeek';
  try {{
    var savedSetup = window.localStorage && window.localStorage.getItem('themeDashboard.activeSetup');
    if (savedSetup && SETUP_TABLES[savedSetup]) activeSetup = savedSetup;
  }} catch(e) {{}}
  // Tightening Range timeframe sub-toggle (one tighten table, rows tagged by tf).
  var activeTightenTF = 'D';
  try {{
    var savedTF = window.localStorage && window.localStorage.getItem('themeDashboard.tightenTF');
    if (savedTF === 'D' || savedTF === 'W' || savedTF === 'M') activeTightenTF = savedTF;
  }} catch(e) {{}}
  var setupsBody = document.getElementById(SETUP_TABLES[activeSetup].body);
  var setupsTableId = SETUP_TABLES[activeSetup].table;
  function setupsFlatRows() {{
    if (!setupsBody) return [];
    return Array.prototype.slice.call(setupsBody.querySelectorAll('tr.setups-row'));
  }}

  function setActiveByRowId(rowId, opts) {{
    if (!rowId) return;
    // Tickers + Setups rows live in separate tbodies. Look up in whichever
    // is active first; fall back to the others to support deep-link hashes.
    var row = null;
    if (activeView === 'tickers' && tickersBody) {{
      row = tickersBody.querySelector('tr[data-row-id="' + rowId + '"]');
    }}
    if (!row && activeView === 'setups' && setupsBody) {{
      row = setupsBody.querySelector('tr[data-row-id="' + rowId + '"]');
    }}
    if (!row) row = tbody.querySelector('tr[data-row-id="' + rowId + '"]');
    if (!row && tickersBody) row = tickersBody.querySelector('tr[data-row-id="' + rowId + '"]');
    if (!row && setupsBody) row = setupsBody.querySelector('tr[data-row-id="' + rowId + '"]');
    if (!row) return;
    activeRowId = rowId;
    rows().forEach(function(r) {{ r.classList.toggle('is-active', r.dataset.rowId === rowId); }});
    tickerFlatRows().forEach(function(r) {{ r.classList.toggle('is-active', r.dataset.rowId === rowId); }});
    setupsFlatRows().forEach(function(r) {{ r.classList.toggle('is-active', r.dataset.rowId === rowId); }});

    if (row.dataset.rowKind === 'ticker-flat' || row.dataset.rowKind === 'setup') {{
      showTickerSection(row.dataset.ticker);
    }} else if (row.dataset.rowKind === 'ticker') {{
      var parent = tbody.querySelector('tr.theme-row[data-theme-id="' + row.dataset.themeId + '"]');
      if (parent && !isExpanded(parent)) setExpanded(parent, true);
      showTickerSection(row.dataset.ticker);
    }} else {{
      showThemeSection(row.dataset.themeId);
    }}

    // Scroll active row into view
    if (sidebar) {{
      var rTop = row.offsetTop, rBot = rTop + row.offsetHeight;
      if (rTop < sidebar.scrollTop || rBot > sidebar.scrollTop + sidebar.clientHeight) {{
        row.scrollIntoView({{block: 'nearest'}});
      }}
    }}

    var visible = visibleRows();
    var idx = -1;
    for (var i = 0; i < visible.length; i++) {{
      if (visible[i].dataset.rowId === rowId) {{ idx = i; break; }}
    }}
    if (indicator) indicator.textContent = (idx >= 0 ? (idx+1) : '—') + ' / ' + visible.length;
    if (footerCurrent) {{
      var lbl = row.dataset.label;
      if (row.dataset.rowKind === 'ticker') lbl = row.dataset.themeId + ' / ' + lbl;
      else if (row.dataset.rowKind === 'ticker-flat') lbl = 'TICKERS / ' + lbl;
      footerCurrent.textContent = lbl;
    }}
    window.scrollTo(0, 0);

    if (!opts || !opts.skipHash) {{
      var part;
      if (row.dataset.rowKind === 'ticker-flat') {{
        part = 'tickers/' + row.dataset.ticker;
      }} else if (row.dataset.rowKind === 'ticker') {{
        part = row.dataset.themeId + '/' + row.dataset.ticker;
      }} else {{
        part = row.dataset.themeId;
      }}
      try {{ history.replaceState(null, '', '#' + part); }} catch(e) {{}}
    }}
  }}

  function moveActive(delta) {{
    var visible = visibleRows();
    if (!visible.length) return;
    var idx = -1;
    for (var i = 0; i < visible.length; i++) {{
      if (visible[i].dataset.rowId === activeRowId) {{ idx = i; break; }}
    }}
    var next = (idx < 0 ? 0 : idx + delta);
    if (next < 0) next = visible.length - 1;
    if (next >= visible.length) next = 0;
    setActiveByRowId(visible[next].dataset.rowId);
  }}

  function currentRow() {{
    if (!activeRowId) return null;
    if (activeView === 'tickers' && tickersBody) {{
      return tickersBody.querySelector('tr[data-row-id="' + activeRowId + '"]');
    }}
    if (activeView === 'setups' && setupsBody) {{
      return setupsBody.querySelector('tr[data-row-id="' + activeRowId + '"]');
    }}
    return tbody.querySelector('tr[data-row-id="' + activeRowId + '"]');
  }}

  function parentThemeOf(tickerRow) {{
    return tbody.querySelector('tr.theme-row[data-theme-id="' + tickerRow.dataset.themeId + '"]');
  }}

  // ── Event wiring ───────────────────────────────────────────
  // Header click → sort.
  document.querySelectorAll('#watchlist th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var k = th.dataset.sortKey;
      var t = th.dataset.sortType;
      if (sortKey === k) {{ sortDir = -sortDir; }} else {{ sortKey = k; sortType = t; sortDir = -1; }}
      sortRows();
    }});
  }});

  // Row click: caret toggles expansion in place; everything else activates.
  tbody.addEventListener('click', function(e) {{
    var caret = e.target.closest('.tree-caret');
    var tr = e.target.closest('tr.watchlist-row');
    if (!tr) return;
    if (caret && tr.dataset.rowKind === 'theme') {{
      setExpanded(tr, !isExpanded(tr));
      return;
    }}
    setActiveByRowId(tr.dataset.rowId);
  }});

  // Toggle hide-below-200.
  if (toggleHide) toggleHide.addEventListener('change', function() {{
    applyFilter();
    var row = currentRow();
    if (row && row.style.display === 'none') {{
      var visible = visibleRows();
      if (visible.length) setActiveByRowId(visible[0].dataset.rowId);
    }}
  }});
  function onHotTightChange() {{
    try {{
      if (window.localStorage) {{
        Object.keys(hotToggles).forEach(function(n) {{
          var tog = hotToggles[n];
          window.localStorage.setItem('themeDashboard.hot' + n,
            (tog && tog.checked) ? '1' : '0');
        }});
        Object.keys(coldToggles).forEach(function(n) {{
          var tog = coldToggles[n];
          window.localStorage.setItem('themeDashboard.cold' + n,
            (tog && tog.checked) ? '1' : '0');
        }});
        window.localStorage.setItem('themeDashboard.tightOnly',
          (toggleTightOnly && toggleTightOnly.checked) ? '1' : '0');
        window.localStorage.setItem('themeDashboard.near50sma',
          (toggleNear50 && toggleNear50.checked) ? '1' : '0');
        window.localStorage.setItem('themeDashboard.momoOnly',
          (toggleMomo && toggleMomo.checked) ? '1' : '0');
        window.localStorage.setItem('themeDashboard.flaggedOnly',
          (toggleFlaggedOnly && toggleFlaggedOnly.checked) ? '1' : '0');
      }}
    }} catch(e) {{}}
    applyFilter();
    updateFilterBadge();
    var row = currentRow();
    if (row && row.style.display === 'none') {{
      var visible = visibleRows();
      if (visible.length) setActiveByRowId(visible[0].dataset.rowId);
    }}
  }}
  Object.keys(hotToggles).forEach(function(n) {{
    var tog = hotToggles[n];
    if (tog) tog.addEventListener('change', onHotTightChange);
  }});
  Object.keys(coldToggles).forEach(function(n) {{
    var tog = coldToggles[n];
    if (tog) tog.addEventListener('change', onHotTightChange);
  }});
  if (toggleTightOnly)   toggleTightOnly.addEventListener('change', onHotTightChange);
  if (toggleNear50)      toggleNear50.addEventListener('change', onHotTightChange);
  if (toggleMomo)        toggleMomo.addEventListener('change', onHotTightChange);
  if (toggleFlaggedOnly) toggleFlaggedOnly.addEventListener('change', onHotTightChange);
  // Rotation-quadrant checkboxes: persist + restamp filtered-out on theme rows.
  Object.keys(wlQuadBoxes).forEach(function(q) {{
    var box = wlQuadBoxes[q];
    if (!box) return;
    box.addEventListener('change', function() {{
      try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.wlQuad.' + q, box.checked ? '1' : '0'); }} catch(e) {{}}
      applyFiltersToRows();
      var row = currentRow();
      if (row && row.style.display === 'none') {{
        var vis = visibleRows();
        if (vis.length) setActiveByRowId(vis[0].dataset.rowId);
      }}
    }});
  }});
  // Ticking "Flagged" auto-sorts the Tickers view by Hot (most flagged
  // themes first) — the confluence names rise to the top.
  if (toggleFlaggedOnly) toggleFlaggedOnly.addEventListener('change', function() {{
    if (toggleFlaggedOnly.checked) {{
      tkSortKey = 'hot'; tkSortType = 'num'; tkSortDir = -1;
      sortTickersRows();
    }}
  }});

  // ── Flag system ────────────────────────────────────────────
  // Persist the flagged-themes set to localStorage on every change.
  function persistFlags() {{
    try {{
      if (window.localStorage) {{
        var arr = [];
        flaggedThemes.forEach(function(id) {{ arr.push(id); }});
        window.localStorage.setItem('themeDashboard.flaggedThemes', JSON.stringify(arr));
      }}
    }} catch(e) {{}}
  }}
  // Repaint every flag icon's is-flagged class from the current set.
  function repaintFlagIcons() {{
    document.querySelectorAll('svg.flag-icon[data-flag-theme]').forEach(function(svg) {{
      var id = svg.dataset.flagTheme;
      svg.classList.toggle('is-flagged', flaggedThemes.has(id));
    }});
  }}
  // Initial paint from persisted set.
  repaintFlagIcons();
  // Click on a flag toggles it; stop propagation so the row click doesn't fire.
  document.addEventListener('click', function(e) {{
    var svg = e.target && e.target.closest && e.target.closest('svg.flag-icon[data-flag-theme]');
    if (!svg) return;
    e.preventDefault(); e.stopPropagation();
    var id = svg.dataset.flagTheme;
    if (flaggedThemes.has(id)) flaggedThemes.delete(id);
    else                       flaggedThemes.add(id);
    svg.classList.toggle('is-flagged', flaggedThemes.has(id));
    persistFlags();
    updateFilterBadge();
    applyHotCounts();
    if (tkSortKey === 'hot') sortTickersRows();
    if (toggleFlaggedOnly && toggleFlaggedOnly.checked) applyFilter();
    if (activeView === 'themes' && themesView === 'history') renderHistory();
    if (activeView === 'themes' && themesView === 'rotation') renderRotation();
  }});
  // Right-click on flag (or flag header) opens a tiny menu with Unflag all.
  var openFlagMenu = null;
  function closeFlagMenu() {{
    if (openFlagMenu && openFlagMenu.parentNode) openFlagMenu.parentNode.removeChild(openFlagMenu);
    openFlagMenu = null;
  }}
  document.addEventListener('contextmenu', function(e) {{
    var inFlag =
      (e.target && e.target.closest && (e.target.closest('svg.flag-icon[data-flag-theme]')
                                       || e.target.closest('.flag-cell')
                                       || e.target.closest('th.flag-col')));
    if (!inFlag) return;
    e.preventDefault();
    closeFlagMenu();
    var menu = document.createElement('div');
    menu.className = 'flag-context-menu';
    menu.style.left = e.clientX + 'px';
    menu.style.top  = e.clientY + 'px';
    var item = document.createElement('div');
    item.className = 'flag-context-menu-item';
    item.textContent = 'Unflag all';
    item.addEventListener('click', function() {{
      flaggedThemes.clear();
      persistFlags();
      repaintFlagIcons();
      updateFilterBadge();
      applyHotCounts();
      if (tkSortKey === 'hot') sortTickersRows();
      if (toggleFlaggedOnly && toggleFlaggedOnly.checked) applyFilter();
      if (activeView === 'themes' && themesView === 'history') renderHistory();
      if (activeView === 'themes' && themesView === 'rotation') renderRotation();
      closeFlagMenu();
    }});
    menu.appendChild(item);
    document.body.appendChild(menu);
    openFlagMenu = menu;
  }});
  // Close menu on any outside click or Escape.
  document.addEventListener('click', function(e) {{
    if (openFlagMenu && !openFlagMenu.contains(e.target)) closeFlagMenu();
  }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeFlagMenu();
  }});

  // ── Per-ticker flags (independent of the theme flags above) ──────────────
  // Flag a ticker from its thumbnail or any Tickers/Setups row; the flag is the
  // same set everywhere, so it lights up on every copy of that ticker. Persisted.
  var flaggedTickers = new Set();
  try {{
    var savedTF = window.localStorage && window.localStorage.getItem('themeDashboard.flaggedTickers');
    if (savedTF) JSON.parse(savedTF).forEach(function(tk) {{ flaggedTickers.add(tk); }});
  }} catch(e) {{}}
  function persistTickerFlags() {{
    try {{
      if (window.localStorage) {{
        var arr = []; flaggedTickers.forEach(function(tk) {{ arr.push(tk); }});
        window.localStorage.setItem('themeDashboard.flaggedTickers', JSON.stringify(arr));
      }}
    }} catch(e) {{}}
  }}
  function repaintTickerFlags() {{
    document.querySelectorAll('.tflag-icon[data-flag-ticker]').forEach(function(el) {{
      el.classList.toggle('is-tflagged', flaggedTickers.has(el.getAttribute('data-flag-ticker')));
    }});
  }}
  repaintTickerFlags();   // initial paint from the persisted set
  // Capture phase so the toggle fires BEFORE row-select / card handlers (which
  // bubble from below) and can stop them — clicking a flag only flags.
  document.addEventListener('click', function(e) {{
    var el = e.target && e.target.closest && e.target.closest('.tflag-icon[data-flag-ticker]');
    if (!el) return;
    e.preventDefault(); e.stopPropagation();
    var tk = el.getAttribute('data-flag-ticker');
    if (flaggedTickers.has(tk)) flaggedTickers.delete(tk); else flaggedTickers.add(tk);
    persistTickerFlags();
    repaintTickerFlags();   // update every copy of this ticker's flag across the UI
  }}, true);

  // Keyboard navigation.
  document.addEventListener('keydown', function(e) {{
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    var row = currentRow();

    if (k === 'ArrowRight') {{
      e.preventDefault();
      if (!row) return;
      if (row.dataset.rowKind === 'theme') {{
        if (!isExpanded(row)) {{
          setExpanded(row, true);
        }} else {{
          // Already expanded — drop focus into the first child.
          var kids = childrenOf(row).filter(function(c) {{ return c.style.display !== 'none'; }});
          if (kids.length) setActiveByRowId(kids[0].dataset.rowId);
        }}
      }}
      // → on a ticker row is a no-op.
    }} else if (k === 'ArrowLeft') {{
      e.preventDefault();
      if (!row) return;
      if (row.dataset.rowKind === 'ticker') {{
        var parent = parentThemeOf(row);
        if (parent) {{
          setActiveByRowId(parent.dataset.rowId);
          setExpanded(parent, false);
        }}
      }} else {{
        // ← on a theme row collapses it.
        if (isExpanded(row)) setExpanded(row, false);
      }}
    }} else if (k === 'ArrowDown' || k === 'j' || k === 'J' || k === ' ' || k === 'PageDown') {{
      e.preventDefault(); moveActive(+1);
    }} else if (k === 'ArrowUp' || k === 'k' || k === 'K' || k === 'PageUp') {{
      e.preventDefault(); moveActive(-1);
    }} else if (k === 'Home') {{
      e.preventDefault();
      var v = visibleRows();
      if (v.length) setActiveByRowId(v[0].dataset.rowId);
    }} else if (k === 'End') {{
      e.preventDefault();
      var v = visibleRows();
      if (v.length) setActiveByRowId(v[v.length-1].dataset.rowId);
    }}
  }});

  // ── Filter panel (sectors + themes; shared across both views) ──
  var filterCell    = document.getElementById('filter-cell');
  var filterPanel   = document.getElementById('filter-panel');
  var filterBadge   = document.getElementById('filter-badge');
  var filterSectorsList = document.getElementById('filter-sectors-list');
  var filterThemesList  = document.getElementById('filter-themes-list');
  var filterThemesSearch= document.getElementById('filter-themes-search');
  var filterResetLink   = document.getElementById('filter-reset-link');
  var filterCloseLink   = document.getElementById('filter-close-link');

  var fd = window.FILTER_DATA || {{
    sectors: [], industries: [], themes: [],
    themeIdsByTicker: {{}}, tickerSector: {{}}, tickerIndustry: {{}},
    themeDominantSector: {{}}, themeDominantIndustry: {{}},
  }};
  // Excluded sets: start from localStorage so refresh keeps state.
  var excludedSectors    = new Set();
  var excludedThemes     = new Set();
  var excludedIndustries = new Set();   // matches against tickerIndustry / themeDominantIndustry
  try {{
    var raw = window.localStorage && window.localStorage.getItem('themeDashboard.filter');
    if (raw) {{
      var parsed = JSON.parse(raw);
      (parsed.excludedSectors    || []).forEach(function(s) {{ excludedSectors.add(s); }});
      (parsed.excludedThemes     || []).forEach(function(t) {{ excludedThemes.add(t); }});
      (parsed.excludedIndustries || []).forEach(function(i) {{ excludedIndustries.add(i); }});
    }}
  }} catch(e) {{}}

  function persistFilter() {{
    try {{
      if (!window.localStorage) return;
      window.localStorage.setItem('themeDashboard.filter', JSON.stringify({{
        excludedSectors:    Array.from(excludedSectors),
        excludedThemes:     Array.from(excludedThemes),
        excludedIndustries: Array.from(excludedIndustries),
      }}));
    }} catch(e) {{}}
  }}

  function renderFilterPanel() {{
    // Sector checkboxes — actual sectors first (alphabetical), then any
    // industry-based rollups (Biotech, etc.) listed after so they read
    // as further refinements within the Sectors column.
    if (filterSectorsList) {{
      var sectorHtml = fd.sectors.map(function(s) {{
        var checked = excludedSectors.has(s) ? '' : 'checked';
        return '<label data-filter-key="' + s + '">'
             + '<input type="checkbox" data-filter-section="sector" data-filter-key="' + s + '" ' + checked + ' />'
             + '<span class="filter-item-label">' + s + '</span>'
             + '</label>';
      }}).join('');
      var industryHtml = (fd.industries || []).map(function(ind) {{
        var checked = excludedIndustries.has(ind.match) ? '' : 'checked';
        return '<label data-filter-key="' + ind.match + '">'
             + '<input type="checkbox" data-filter-section="industry" data-filter-key="' + ind.match + '" ' + checked + ' />'
             + '<span class="filter-item-label">' + ind.label + '</span>'
             + '<span class="filter-item-sector">industry</span>'
             + '</label>';
      }}).join('');
      filterSectorsList.innerHTML = sectorHtml + industryHtml;
    }}
    // Theme checkboxes. Show theme label + dominant sector chip.
    if (filterThemesList) {{
      filterThemesList.innerHTML = fd.themes.map(function(t) {{
        var checked = excludedThemes.has(t.id) ? '' : 'checked';
        return '<label data-filter-key="' + t.id + '" data-filter-label="' + t.label.toLowerCase() + '">'
             + '<input type="checkbox" data-filter-section="theme" data-filter-key="' + t.id + '" ' + checked + ' />'
             + '<span class="filter-item-label">' + t.label + '</span>'
             + '</label>';
      }}).join('');
    }}
  }}

  function updateFilterBadge() {{
    var n = excludedSectors.size + excludedThemes.size + excludedIndustries.size;
    var hotN = 0;
    Object.keys(hotToggles).forEach(function(k) {{
      var tog = hotToggles[k];
      if (tog && tog.checked) hotN++;
    }});
    var coldN = 0;
    Object.keys(coldToggles).forEach(function(k) {{
      var tog = coldToggles[k];
      if (tog && tog.checked) coldN++;
    }});
    var flaggedActive = (toggleFlaggedOnly && toggleFlaggedOnly.checked) ? flaggedThemes.size : 0;
    var total = n + hotN + coldN + flaggedActive;
    if (filterCell) {{
      filterCell.classList.toggle('has-exclusions', total > 0);
      filterCell.title = total > 0
        ? ('Filter (' + n + ' excluded, ' + hotN + ' Hot, ' + coldN + ' Cold, ' + flaggedActive + ' flagged-only)')
        : 'Filter sectors, themes, strength, and flagged';
    }}
  }}

  function themeRowPassesFilter(themeId) {{
    if (excludedThemes.has(themeId)) return false;
    var sec = fd.themeDominantSector[themeId] || 'Unknown';
    if (excludedSectors.has(sec)) return false;
    var ind = fd.themeDominantIndustry[themeId] || 'Unknown';
    if (excludedIndustries.has(ind)) return false;
    return true;
  }}
  function tickerRowPassesFilter(ticker) {{
    var sec = fd.tickerSector[ticker] || 'Unknown';
    if (excludedSectors.has(sec)) return false;
    var ind = fd.tickerIndustry[ticker] || 'Unknown';
    if (excludedIndustries.has(ind)) return false;
    var themeIds = fd.themeIdsByTicker[ticker] || [];
    if (themeIds.length === 0) {{
      // Truly unmapped — treat as Ungrouped.
      return !excludedThemes.has('ungrouped');
    }}
    // Pass if at least one theme is checked.
    for (var i = 0; i < themeIds.length; i++) {{
      if (!excludedThemes.has(themeIds[i])) return true;
    }}
    return false;
  }}
  // Rotation-quadrant gate (Chart view). A theme (or its child ticker rows, via
  // the parent theme id) is hidden when its current RRG quadrant is unchecked.
  // Themes with no rotation quadrant (ungrouped / too little history) always pass.
  function quadPasses(themeId) {{
    var q = themeQuad[themeId];
    if (!q) return true;
    var box = wlQuadBoxes[q];
    return !box || box.checked;
  }}
  // Tickers view: a flat ticker passes the rotation-quadrant filter when at
  // least one of its themes sits in a checked quadrant (mirrors the sector/
  // theme "any theme checked" rule). Tickers whose themes carry no quadrant
  // (ungrouped / too little history) always pass.
  function tickerQuadPasses(ticker) {{
    var themeIds = fd.themeIdsByTicker[ticker] || [];
    var sawQuad = false;
    for (var i = 0; i < themeIds.length; i++) {{
      var q = themeQuad[themeIds[i]];
      if (!q) continue;
      sawQuad = true;
      var box = wlQuadBoxes[q];
      if (!box || box.checked) return true;
    }}
    return !sawQuad;
  }}
  // One-time: tint each theme-name by its quadrant (overrides the below-200 red).
  function initThemeQuads() {{
    rows().forEach(function(r) {{
      if (r.dataset.rowKind !== 'theme') return;
      var q = themeQuad[r.dataset.themeId];
      if (!q) return;
      var lbl = r.querySelector('.theme-label');
      if (lbl) lbl.classList.add('quad-' + q);
    }});
  }}

  function applyFiltersToRows() {{
    // Themes pane: hide a theme row (and by extension its expanded
    // children) when its theme or dominant sector is excluded, or its
    // rotation quadrant is toggled off.
    rows().forEach(function(r) {{
      if (r.dataset.rowKind === 'theme') {{
        var ok = themeRowPassesFilter(r.dataset.themeId) && quadPasses(r.dataset.themeId);
        r.classList.toggle('filtered-out', !ok);
      }} else if (r.dataset.rowKind === 'ticker') {{
        var ok2 = tickerRowPassesFilter(r.dataset.ticker) && quadPasses(r.dataset.themeId);
        r.classList.toggle('filtered-out', !ok2);
      }}
    }});
    // Tickers pane: hide ticker-flat rows individually (sector/theme + quadrant).
    tickerFlatRows().forEach(function(r) {{
      var ok = tickerRowPassesFilter(r.dataset.ticker) && tickerQuadPasses(r.dataset.ticker);
      r.classList.toggle('filtered-out', !ok);
    }});
    // Setups pane: same per-ticker rule as the Tickers pane. Stamp ALL
    // setup tables (Extension Peek / First Flags / Tightening Range), not
    // just the active one — otherwise an exclusion set while one tab is open
    // never reaches the other tabs' rows, and they show through on switch.
    document.querySelectorAll('tr.setups-row').forEach(function(r) {{
      var ok = tickerRowPassesFilter(r.dataset.ticker);
      r.classList.toggle('filtered-out', !ok);
    }});
    // Reapply the below-200 filter so inline display is recomputed on top.
    applyFilter();
    // Keep the rotation map in sync with sector/theme exclusions.
    if (typeof rotDrawFrame === 'function' && activeView === 'themes' && themesView === 'rotation') {{
      rotUpdateThrustReadout();
      rotDrawFrame(rotCurDi, rotCurFrac, rotCurK);
    }}
  }}

  // Wire checkbox toggling (sectors + industries + themes).
  function onFilterCheckboxChange(e) {{
    var input = e.target;
    if (!input || input.tagName !== 'INPUT') return;
    var section = input.dataset.filterSection;
    var key     = input.dataset.filterKey;
    if (!section || !key) return;
    var set;
    if      (section === 'sector')   set = excludedSectors;
    else if (section === 'industry') set = excludedIndustries;
    else                              set = excludedThemes;
    if (input.checked) set.delete(key);
    else               set.add(key);
    persistFilter();
    updateFilterBadge();
    applyFiltersToRows();
  }}
  if (filterSectorsList) filterSectorsList.addEventListener('change', onFilterCheckboxChange);
  if (filterThemesList)  filterThemesList.addEventListener('change', onFilterCheckboxChange);

  // Select all / Clear shortcuts. The Sectors column owns both sectors
  // and industry rollups (Biotech), so its links sweep both at once.
  document.querySelectorAll('.filter-link').forEach(function(el) {{
    el.addEventListener('click', function() {{
      var section = el.dataset.filterSection;
      var action  = el.dataset.filterAction;
      if (section === 'sector') {{
        if (action === 'all')  {{ excludedSectors.clear(); excludedIndustries.clear(); }}
        if (action === 'none') {{
          fd.sectors.forEach(function(s) {{ excludedSectors.add(s); }});
          (fd.industries || []).forEach(function(i) {{ excludedIndustries.add(i.match); }});
        }}
      }} else if (section === 'theme') {{
        if (action === 'all')  excludedThemes.clear();
        if (action === 'none') fd.themes.forEach(function(t) {{ excludedThemes.add(t.id); }});
      }} else if (section === 'strength') {{
        // Clear = uncheck all Hot N + Cold N boxes (no "All" — that would over-restrict)
        if (action === 'none') {{
          Object.keys(hotToggles).forEach(function(n) {{
            var tog = hotToggles[n];
            if (tog) tog.checked = false;
          }});
          Object.keys(coldToggles).forEach(function(n) {{
            var tog = coldToggles[n];
            if (tog) tog.checked = false;
          }});
          onHotTightChange();
          return;
        }}
      }}
      persistFilter();
      renderFilterPanel();
      updateFilterBadge();
      applyFiltersToRows();
    }});
  }});

  // Reset everything.
  if (filterResetLink) {{
    filterResetLink.addEventListener('click', function() {{
      excludedSectors.clear();
      excludedThemes.clear();
      excludedIndustries.clear();
      Object.keys(hotToggles).forEach(function(n) {{
        var tog = hotToggles[n];
        if (tog) tog.checked = false;
      }});
      Object.keys(coldToggles).forEach(function(n) {{
        var tog = coldToggles[n];
        if (tog) tog.checked = false;
      }});
      persistFilter();
      renderFilterPanel();
      updateFilterBadge();
      applyFiltersToRows();
      onHotTightChange();
    }});
  }}

  // Themes search box: substring-match filters which checkboxes show.
  if (filterThemesSearch) {{
    filterThemesSearch.addEventListener('input', function() {{
      var q = filterThemesSearch.value.toLowerCase().trim();
      filterThemesList.querySelectorAll('label').forEach(function(lbl) {{
        var lab = lbl.dataset.filterLabel || '';
        lbl.classList.toggle('hidden-by-search', q && lab.indexOf(q) === -1);
      }});
    }});
  }}

  // Open / close behavior.
  function openFilterPanel() {{
    filterPanel.style.display = '';
    filterCell.classList.add('is-open');
  }}
  function closeFilterPanel() {{
    filterPanel.style.display = 'none';
    filterCell.classList.remove('is-open');
  }}
  if (filterCell) {{
    filterCell.addEventListener('click', function() {{
      if (filterPanel.style.display === 'none') openFilterPanel();
      else closeFilterPanel();
    }});
    filterCell.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' || e.key === ' ') {{
        e.preventDefault();
        filterCell.click();
      }}
    }});
  }}
  if (filterCloseLink) filterCloseLink.addEventListener('click', closeFilterPanel);
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape' && filterPanel && filterPanel.style.display !== 'none') {{
      e.preventDefault();
      closeFilterPanel();
    }}
  }});
  document.addEventListener('click', function(e) {{
    if (!filterPanel || filterPanel.style.display === 'none') return;
    if (filterPanel.contains(e.target)) return;
    if (filterCell && filterCell.contains(e.target)) return;
    closeFilterPanel();
  }});

  // Render the checkboxes once, paint state, apply persisted filter to rows.
  renderFilterPanel();
  updateFilterBadge();
  initThemeQuads();          // tint theme names by RRG quadrant (one-time)
  applyFiltersToRows();

  // ── Tickers-view sort + click + brand toggle ───────────────
  function applyTickersHeaderIndicators() {{
    document.querySelectorAll('#tickers-watchlist th').forEach(function(th) {{
      th.classList.remove('sort-active', 'sort-asc');
    }});
    var active = document.querySelector('#tickers-watchlist th[data-sort-key="' + tkSortKey + '"]');
    if (active) {{
      active.classList.add('sort-active');
      if (tkSortDir === 1) active.classList.add('sort-asc');
    }}
  }}
  function sortTickersRows() {{
    if (!tickersBody) return;
    var all = tickerFlatRows().slice();
    all.sort(function(a, b) {{
      if (tkSortType === 'num') {{
        var key = (tkSortKey === 'comp') ? ('comp' + currentCompPeriod) : tkSortKey;
        var miss = (tkSortKey === 'comp') ? '1e9' : '-1e9';
        var av = parseFloat(a.dataset[key] || miss);
        var bv = parseFloat(b.dataset[key] || miss);
        if (isNaN(av)) av = parseFloat(miss);
        if (isNaN(bv)) bv = parseFloat(miss);
        return (av - bv) * tkSortDir;
      }} else {{
        var ak = (tkSortKey === 'theme-label') ? 'themeLabel' : tkSortKey;
        var sa = (a.dataset[ak] || '').toLowerCase();
        var sb = (b.dataset[ak] || '').toLowerCase();
        return sa.localeCompare(sb) * tkSortDir;
      }}
    }});
    all.forEach(function(r) {{ tickersBody.appendChild(r); }});
    applyTickersHeaderIndicators();
  }}
  document.querySelectorAll('#tickers-watchlist th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var k = th.dataset.sortKey;
      var t = th.dataset.sortType;
      if (tkSortKey === k) {{ tkSortDir = -tkSortDir; }} else {{ tkSortKey = k; tkSortType = t; tkSortDir = -1; }}
      sortTickersRows();
    }});
  }});

  // ── Hot-theme confluence (Tickers view) ─────────────────────────
  // For each flat ticker row, count how many of its themes are currently
  // flagged ("hot"). Writes the count to data-hot (so the Hot column sorts),
  // fills the Hot cell, highlights rows in 2+ hot themes, and re-renders the
  // Theme cell so the flagged themes show greened. Recomputed on every flag
  // change — flags are client-side, so this can't be baked at build time.
  function hotThemeLabel(id) {{
    if (window.THEME_LABELS && window.THEME_LABELS[id]) return window.THEME_LABELS[id];
    if (id === 'ungrouped') return 'Ungrouped';
    return id;
  }}
  function applyHotCounts() {{
    var anyFlagged = flaggedThemes.size > 0;
    tickerFlatRows().forEach(function(r) {{
      var ids = (r.dataset.themeIds || '').split(',').filter(Boolean);
      var hotCount = 0;
      var parts = [];
      for (var i = 0; i < ids.length; i++) {{
        var id = ids[i];
        var isHot = flaggedThemes.has(id);
        if (isHot) hotCount++;
        var lbl = hotThemeLabel(id).replace(/&/g, '&amp;').replace(/</g, '&lt;');
        parts.push('<span class="mem-theme' + (isHot ? ' is-hot' : '') + '">' + lbl + '</span>');
      }}
      r.dataset.hot = hotCount;
      var memCell = r.querySelector('.theme-membership-cell');
      if (memCell) memCell.innerHTML = parts.join(', ');
      var hotCell = r.querySelector('.hot-cell');
      if (hotCell) {{
        hotCell.classList.remove('hot-1', 'hot-2', 'hot-3plus');
        if (!anyFlagged || hotCount === 0) {{
          hotCell.textContent = anyFlagged ? '·' : '';
        }} else {{
          hotCell.textContent = String(hotCount);
          if (hotCount >= 3) hotCell.classList.add('hot-3plus');
          else if (hotCount === 2) hotCell.classList.add('hot-2');
          else hotCell.classList.add('hot-1');
        }}
      }}
      r.classList.toggle('hot-confluence', hotCount >= 2);
    }});
  }}

  // ── Compression column: cell rendering + period picker ──────────
  // Each row carries comp3/comp5/comp10/comp20/comp30 data attrs. The visible
  // cell (.comp-cell) is populated from the data attr matching the current
  // period. Right-click on the .comp-header opens a small popup to switch
  // periods; left-click sorts via the existing th click handler.
  function fmtComp(v) {{
    if (v == null || isNaN(v) || v >= 1e8) return '—';
    return v.toFixed(2);
  }}
  function applyCompCells() {{
    var key = 'comp' + currentCompPeriod;
    document.querySelectorAll('td.comp-cell').forEach(function(cell) {{
      var row = cell.closest('tr');
      if (!row) return;
      var v = parseFloat(row.dataset[key]);
      cell.textContent = fmtComp(v);
    }});
    document.querySelectorAll('.comp-period').forEach(function(span) {{
      span.textContent = String(currentCompPeriod);
    }});
  }}
  function setCompPeriod(n) {{
    if (![3, 5, 10, 20, 30].includes(n)) return;
    currentCompPeriod = n;
    try {{ window.localStorage && window.localStorage.setItem('themeDashboard.compPeriod', String(n)); }} catch(e) {{}}
    applyCompCells();
    if (sortKey === 'comp') sortRows();
    if (tkSortKey === 'comp') sortTickersRows();
  }}

  // Right-click popup. Reused across both tables. Anchored at cursor.
  var compPopup = null;
  function hideCompPopup() {{
    if (compPopup && compPopup.parentNode) compPopup.parentNode.removeChild(compPopup);
    compPopup = null;
  }}
  function showCompPopup(x, y) {{
    hideCompPopup();
    compPopup = document.createElement('div');
    compPopup.style.cssText =
      'position:fixed;z-index:9999;left:' + x + 'px;top:' + y + 'px;' +
      'background:var(--bg-panel,#1a1a1a);border:1px solid var(--border,#333);' +
      'border-radius:4px;padding:6px 0;font-size:12px;' +
      'box-shadow:0 4px 12px rgba(0,0,0,0.4);min-width:90px;';
    [3, 5, 10, 20, 30].forEach(function(n) {{
      var row = document.createElement('div');
      var active = (n === currentCompPeriod);
      row.textContent = (active ? '● ' : '○ ') + n + ' bars';
      row.style.cssText =
        'padding:5px 14px;cursor:pointer;color:' +
        (active ? 'var(--accent,#ffcc00)' : 'var(--fg-primary,#eee)') + ';';
      row.addEventListener('mouseenter', function() {{ row.style.background = 'var(--bg-row-hover,#222)'; }});
      row.addEventListener('mouseleave', function() {{ row.style.background = 'transparent'; }});
      row.addEventListener('click', function() {{
        setCompPeriod(n);
        hideCompPopup();
      }});
      compPopup.appendChild(row);
    }});
    document.body.appendChild(compPopup);
  }}
  document.querySelectorAll('th.comp-header').forEach(function(th) {{
    th.addEventListener('contextmenu', function(e) {{
      e.preventDefault();
      showCompPopup(e.clientX, e.clientY);
    }});
  }});
  document.addEventListener('click', function(e) {{
    if (compPopup && !compPopup.contains(e.target)) hideCompPopup();
  }});
  // Render initial values + label.
  applyCompCells();
  if (tickersBody) {{
    tickersBody.addEventListener('click', function(e) {{
      var tr = e.target.closest('tr.tickers-row');
      // setups-row also carries the tickers-row class for shared styling;
      // skip those here, they get their own handler below.
      if (!tr || tr.classList.contains('setups-row')) return;
      setActiveByRowId(tr.dataset.rowId);
    }});
  }}

  // ── Setups view: tabbed setup types + sort + click-to-render ─────────
  var stSortKey  = SETUP_TABLES[activeSetup].defSort;
  var stSortDir  = SETUP_TABLES[activeSetup].defDir;   // 1 asc / -1 desc
  var stSortType = 'num';

  function sortSetupsRows() {{
    if (!setupsBody) return;
    var rows = setupsFlatRows().slice();
    rows.sort(function(a, b) {{
      if (stSortType === 'num') {{
        // dataset attribute names are kebab→camel, e.g. data-yest-sd → yestSd
        var attr = stSortKey;
        if (attr === 'yest-sd') attr = 'yestSd';
        var av = parseFloat(a.dataset[attr]);
        var bv = parseFloat(b.dataset[attr]);
        if (isNaN(av)) av = -1e9;
        if (isNaN(bv)) bv = -1e9;
        return (av - bv) * stSortDir;
      }} else {{
        var ak = (stSortKey === 'theme-label') ? 'themeLabel' : stSortKey;
        var sa = (a.dataset[ak] || '').toLowerCase();
        var sb = (b.dataset[ak] || '').toLowerCase();
        return sa.localeCompare(sb) * stSortDir;
      }}
    }});
    rows.forEach(function(r) {{ setupsBody.appendChild(r); }});
    // Header indicators on the ACTIVE setup table only.
    document.querySelectorAll('#' + setupsTableId + ' th').forEach(function(th) {{
      th.classList.remove('sort-active', 'sort-asc');
    }});
    var active = document.querySelector('#' + setupsTableId + ' th[data-sort-key="' + stSortKey + '"]');
    if (active) {{
      active.classList.add('sort-active');
      if (stSortDir === 1) active.classList.add('sort-asc');
    }}
  }}

  // Header clicks — wired on every setup table; only the active one is visible.
  document.querySelectorAll('#setups-watchlist th, #firstflags-watchlist th, #tighten-watchlist th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var k = th.dataset.sortKey;
      var t = th.dataset.sortType;
      if (stSortKey === k) {{ stSortDir = -stSortDir; }} else {{ stSortKey = k; stSortType = t; stSortDir = (t === 'num') ? -1 : 1; }}
      sortSetupsRows();
    }});
  }});

  // Row clicks → activate that ticker's chart — wired on every setup body.
  Object.keys(SETUP_TABLES).forEach(function(k) {{
    var b = document.getElementById(SETUP_TABLES[k].body);
    if (b) b.addEventListener('click', function(e) {{
      var tr = e.target.closest('tr.setups-row');
      if (!tr) return;
      setActiveByRowId(tr.dataset.rowId);
    }});
  }});

  function updateSetupBrand() {{
    if (activeView === 'setups' && brandTitle) {{
      brandTitle.textContent = 'SETUPS · ' + SETUP_TABLES[activeSetup].label;
    }}
  }}

  // Switch the active setup type (single click on a tab).
  function selectSetup(name) {{
    if (!SETUP_TABLES[name]) name = 'extpeek';
    activeSetup = name;
    try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.activeSetup', name); }} catch(e) {{}}
    Object.keys(SETUP_TABLES).forEach(function(k) {{
      var c = document.getElementById(SETUP_TABLES[k].content);
      if (c) c.style.display = (k === name) ? '' : 'none';
    }});
    document.querySelectorAll('.setups-tab').forEach(function(t) {{
      t.classList.toggle('is-active', t.dataset.setup === name);
    }});
    // Repoint shared helpers + reset to this table's default sort.
    setupsBody = document.getElementById(SETUP_TABLES[name].body);
    setupsTableId = SETUP_TABLES[name].table;
    stSortKey = SETUP_TABLES[name].defSort;
    stSortDir = SETUP_TABLES[name].defDir;
    stSortType = 'num';
    sortSetupsRows();
    if (activeView === 'setups') {{
      applyFilter();
      var cur = currentRow();
      if (!cur || cur.style.display === 'none') {{
        var vis = visibleRows();
        if (vis.length) setActiveByRowId(vis[0].dataset.rowId);
      }}
      updateSetupBrand();
    }}
  }}

  document.querySelectorAll('.setups-tab').forEach(function(tab) {{
    tab.addEventListener('click', function() {{ selectSetup(tab.dataset.setup); }});
  }});

  // Tightening Range D/W/M sub-toggle. One table holds all timeframes; the
  // row-level tf gate (in rowFailsHotOrTight) hides the non-active ones, so
  // switching is just: set the tf, re-filter, re-sort, re-pick a visible row.
  function selectTightenTF(tf) {{
    if (tf !== 'D' && tf !== 'W' && tf !== 'M') tf = 'D';
    activeTightenTF = tf;
    try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.tightenTF', tf); }} catch(e) {{}}
    document.querySelectorAll('.tighten-tf-tab').forEach(function(b) {{
      b.classList.toggle('is-active', b.dataset.tf === tf);
    }});
    if (activeSetup === 'tightenrange') {{
      sortSetupsRows();
      applyFilter();
      var cur = currentRow();
      if (!cur || cur.style.display === 'none') {{
        var vis = visibleRows();
        if (vis.length) setActiveByRowId(vis[0].dataset.rowId);
      }}
    }}
  }}
  document.querySelectorAll('.tighten-tf-tab').forEach(function(b) {{
    b.addEventListener('click', function() {{ selectTightenTF(b.dataset.tf); }});
    b.classList.toggle('is-active', b.dataset.tf === activeTightenTF);
  }});

  // Initial tab state: restore persisted choice + sort the active table.
  selectSetup(activeSetup);

  function applyTickersFilter() {{
    // Update the empty-state notice and the visible counter.
    if (!tickersBody) return;
    var visible = visibleRows();
    if (activeView === 'tickers') {{
      if (tickersEmpty) tickersEmpty.style.display = visible.length === 0 ? 'block' : 'none';
      if (visibleCount) visibleCount.textContent = visible.length + ' visible';
    }}
  }}

  function setView(name, opts) {{
    rotCloseOverlay();  // never leave a theme section orphaned in the overlay when navigating
    if (name !== 'tickers' && name !== 'themes' && name !== 'setups' && name !== 'candidates') name = 'themes';
    activeView = name;
    document.body.classList.toggle('view-themes',     activeView === 'themes');
    document.body.classList.toggle('view-tickers',    activeView === 'tickers');
    document.body.classList.toggle('view-setups',     activeView === 'setups');
    document.body.classList.toggle('view-candidates', activeView === 'candidates');
    // Themes sub-view classes (only take effect via the .view-themes compound CSS).
    document.body.classList.toggle('tv-chart',    themesView === 'chart');
    document.body.classList.toggle('tv-heatmap',  themesView === 'heatmap');
    document.body.classList.toggle('tv-history',  themesView === 'history');
    document.body.classList.toggle('tv-rotation', themesView === 'rotation');
    document.body.classList.toggle('tv-map',      themesView === 'map');
    viewBtns.forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.tv === themesView); }});
    if (brandTitle) {{
      brandTitle.textContent = (activeView === 'tickers')    ? 'HOT TICKERS DASHBOARD'
                              : (activeView === 'setups')    ? ('SETUPS · ' + SETUP_TABLES[activeSetup].label)
                              : (activeView === 'candidates') ? 'CANDIDATES'
                              : 'HOT THEME DASHBOARD';
    }}
    try {{
      if (window.localStorage) window.localStorage.setItem('themeDashboard.view', activeView);
    }} catch(e) {{}}
    // Re-derive visible counter + empty-state for the newly active pane.
    if (activeView === 'tickers') {{
      applyTickersFilter();
    }} else if (activeView === 'setups') {{
      // Setups view honors the same filters (Hide < 200, Tight D1,
      // Flagged, Sector / Theme / Industry excludes, Hot N, Cold N).
      // applyFilter walks setupsFlatRows alongside the other panes.
      applyFilter();
    }} else {{
      applyFilter();
    }}
    var themesSub = (activeView === 'themes') ? themesView : null;
    if (themesSub === 'heatmap') {{
      renderHeatmap();
    }} else if (themesSub === 'history') {{
      renderHistory();
    }} else if (themesSub === 'rotation') {{
      renderRotation();
    }} else if (themesSub === 'map') {{
      renderMap();
    }} else if (!opts || !opts.preserveActive) {{
      var v = visibleRows();
      if (v.length) setActiveByRowId(v[0].dataset.rowId);
    }} else if (activeView === 'themes' && activeRowId) {{
      setActiveByRowId(activeRowId, {{skipHash: true}});
    }}
  }}
  if (brandBtn) {{
    brandBtn.addEventListener('click', function() {{
      // Cycle: themes → tickers → setups → candidates → themes
      var next = (activeView === 'themes')      ? 'tickers'
               : (activeView === 'tickers')     ? 'setups'
               : (activeView === 'setups')      ? 'candidates'
               :                                  'themes';
      setView(next);
    }});
  }}

  // ── Theme heatmap (RS vs Universe, switchable window) ───────
  var HM_WINDOWS = ['0d','1d','3d','5d','10d'];
  var hmWindow = '5d';
  try {{
    var hmSaved = window.localStorage && window.localStorage.getItem('themeDashboard.hmWindow');
    if (hmSaved && HM_WINDOWS.indexOf(hmSaved) >= 0) hmWindow = hmSaved;
  }} catch(e) {{}}
  var hmGrid = document.getElementById('heatmap-grid');
  var hmBtns = Array.prototype.slice.call(document.querySelectorAll('.hm-win-btn'));
  var hmPage = document.getElementById('heatmap-page');
  var hmExpandBody = document.getElementById('hm-expand-body');
  var hmExpandTitle = document.getElementById('hm-expand-title');
  var hmExpandTheme = null;

  function hmAbsCap(vals) {{
    var a = vals.filter(function(v) {{ return v !== null && isFinite(v); }})
                .map(Math.abs).sort(function(x, y) {{ return x - y; }});
    if (!a.length) return 1;
    var c = a[Math.floor(0.90 * (a.length - 1))];
    return (c && c > 0) ? c : 1;
  }}
  function hmColor(v, cap) {{
    if (v === null || !isFinite(v)) return null;
    var t = v / cap; if (t > 1) t = 1; if (t < -1) t = -1;
    var base = [32, 32, 36];
    var tgt = (t >= 0) ? [24, 150, 24] : [184, 40, 40];
    var a = Math.abs(t);
    return 'rgb(' + Math.round(base[0] + a * (tgt[0] - base[0])) + ','
                  + Math.round(base[1] + a * (tgt[1] - base[1])) + ','
                  + Math.round(base[2] + a * (tgt[2] - base[2])) + ')';
  }}
  function hmFmt(v) {{
    if (v === null || !isFinite(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '×';
  }}
  function renderHeatmap() {{
    if (!hmGrid || !window.HEATMAP_DATA) return;
    if (hmPage) hmPage.classList.remove('is-expanded');
    hmBtns.forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.win === hmWindow); }});
    var data = window.HEATMAP_DATA.slice();
    var cap = hmAbsCap(data.map(function(d) {{ return d.rs[hmWindow]; }}));
    data.sort(function(x, y) {{
      var xv = x.rs[hmWindow], yv = y.rs[hmWindow];
      if (xv === null && yv === null) return 0;
      if (xv === null) return 1;
      if (yv === null) return -1;
      return yv - xv;
    }});
    var html = '';
    for (var i = 0; i < data.length; i++) {{
      var d = data[i];
      var v = d.rs[hmWindow];
      var col = hmColor(v, cap);
      var cls = 'hm-tile' + (col === null ? ' is-null' : '');
      var sty = (col === null) ? '' : (' style="background:' + col + '"');
      html += '<div class="' + cls + '" data-theme-id="' + d.id + '"' + sty + '>'
            +   '<div class="hm-name">' + d.label + '</div>'
            +   '<div class="hm-meta"><span class="hm-rs">' + hmFmt(v) + '</span>'
            +   '<span class="hm-n">n=' + d.n + '</span></div>'
            + '</div>';
    }}
    hmGrid.innerHTML = html;
  }}
  hmBtns.forEach(function(b) {{
    b.addEventListener('click', function() {{
      hmWindow = b.dataset.win;
      try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.hmWindow', hmWindow); }} catch(e) {{}}
      renderHeatmap();
    }});
  }});
  function hmOpenExpand(themeId, label) {{
    if (!hmPage || !hmExpandBody) return;
    hmExpandTheme = themeId;
    var section = document.getElementById(themeId);
    var grid = section ? section.querySelector('.member-grid') : null;
    hmExpandBody.innerHTML = '';
    if (grid) {{ hmExpandBody.appendChild(grid.cloneNode(true)); }}
    else {{ hmExpandBody.textContent = 'No member charts available for this theme.'; }}
    if (hmExpandTitle) hmExpandTitle.textContent = label || themeId;
    hmPage.classList.add('is-expanded');
    hmExpandBody.scrollTop = 0;
  }}
  function hmCloseExpand() {{ if (hmPage) hmPage.classList.remove('is-expanded'); }}
  if (hmGrid) {{
    hmGrid.addEventListener('click', function(e) {{
      var tile = e.target && e.target.closest ? e.target.closest('.hm-tile') : null;
      if (!tile || !tile.dataset.themeId) return;
      var nm = tile.querySelector('.hm-name');
      hmOpenExpand(tile.dataset.themeId, nm ? nm.textContent : tile.dataset.themeId);
    }});
  }}
  var hmBackBtn = document.getElementById('hm-back-btn');
  if (hmBackBtn) hmBackBtn.addEventListener('click', hmCloseExpand);
  var hmViewChartBtn = document.getElementById('hm-viewchart-btn');
  if (hmViewChartBtn) hmViewChartBtn.addEventListener('click', function() {{
    if (!hmExpandTheme) return;
    var id = hmExpandTheme;
    setThemesView('chart');
    setActiveByRowId(id);
  }});
  if (hmExpandBody) {{
    hmExpandBody.addEventListener('click', function(e) {{
      var card = e.target && e.target.closest ? e.target.closest('.member-card') : null;
      if (!card || !card.dataset.ticker) return;
      setView('tickers', {{preserveActive: true}});
      setActiveByRowId('tk__' + card.dataset.ticker);
    }});
  }}

  // ── Narrative Map (story-space placement, strength vs SPY) ──────
  var mapWindow = 'rs20';
  try {{
    var mwSaved = window.localStorage && window.localStorage.getItem('themeDashboard.mapWindow');
    if (mwSaved === 'rs5' || mwSaved === 'rs20' || mwSaved === 'rs65') mapWindow = mwSaved;
  }} catch(e) {{}}
  var mapBody = document.getElementById('map-body');
  var mapPage = document.getElementById('map-page');
  var mapExpandBody = document.getElementById('map-expand-body');
  var mapExpandTitle = document.getElementById('map-expand-title');
  var mapExpandNarr = document.getElementById('map-expand-narr');
  var mapExpandTheme = null;
  var mapWinBtns = Array.prototype.slice.call(document.querySelectorAll('.map-win-btn'));
  // themes whose tickers bridge Crypto<->Hub (drawn dashed)
  var MAP_STRADDLE_THEMES = {{'crypto_miners': 1}};

  function mapAbsCap(vals) {{
    var a = vals.filter(function(v) {{ return v !== null && isFinite(v); }})
                .map(Math.abs).sort(function(x, y) {{ return x - y; }});
    if (!a.length) return 1;
    var c = a[Math.floor(0.90 * (a.length - 1))];
    return (c && c > 0) ? c : 1;
  }}
  function mapColor(v, cap) {{
    if (v === null || !isFinite(v)) return null;
    var t = v / cap; if (t > 1) t = 1; if (t < -1) t = -1;
    var base = [32, 32, 36];
    var tgt = (t >= 0) ? [24, 150, 24] : [184, 40, 40];
    var a = Math.abs(t);
    return 'rgb(' + Math.round(base[0] + a*(tgt[0]-base[0])) + ','
                  + Math.round(base[1] + a*(tgt[1]-base[1])) + ','
                  + Math.round(base[2] + a*(tgt[2]-base[2])) + ')';
  }}
  function mapFmt(v) {{
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(1);
  }}
  function mapFlowChip(region) {{
    var cls = region.flow === 'in' ? 'flow-in' : (region.flow === 'out' ? 'flow-out' : 'flow-flat');
    var arrow = region.flow === 'in' ? '▲ money in' : (region.flow === 'out' ? '▼ distributing out' : '· flat');
    var nums = 'RS 5/20/65 ' + mapFmt(region.rs5) + ' / ' + mapFmt(region.rs20) + ' / ' + mapFmt(region.rs65);
    return '<span class="map-flow"><span class="' + cls + '">' + arrow + '</span>'
         + '<span class="map-rsnums">' + nums + '</span></span>';
  }}
  function mapNodeHTML(node, cap) {{
    var v = node[mapWindow];
    var col = mapColor(v, cap);
    var cls = 'map-node' + (col === null ? ' mn-null' : '') + (MAP_STRADDLE_THEMES[node.id] ? ' mn-straddle' : '') + (node.drift ? ' mn-drift' : '');
    var sty = (col === null) ? '' : (' style="background:' + col + '"');
    var tip = node.drift ? ('⚠ DRIFT: ' + node.drift + (node.narrative ? ('  —  ' + node.narrative) : ''))
                         : (node.narrative || '');
    var title = tip ? (' title="' + String(tip).replace(/"/g, '&quot;') + '"') : '';
    return '<div class="' + cls + '" data-theme-id="' + node.id + '"' + sty + title + '>'
         +   '<div class="mn-name">' + (node.drift ? '⚠ ' : '') + node.label + '</div>'
         +   '<div class="mn-rs">' + mapFmt(v) + '</div>'
         + '</div>';
  }}
  function mapShortLabel(s) {{
    s = String(s).split('·')[0].split('—')[0].split(' - ')[0].trim();
    return s.length > 15 ? s.slice(0, 14) + '…' : s;
  }}
  var MAP_NS = 'http://www.w3.org/2000/svg';
  function svgEl(t, attrs) {{
    var e = document.createElementNS(MAP_NS, t);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }}
  function mapFlowColor(f) {{
    return f === 'in' ? 'rgba(30,255,30,0.55)' : (f === 'out' ? 'rgba(255,48,48,0.5)' : 'rgba(150,150,160,0.30)');
  }}
  function renderMap() {{
    if (!mapBody || !window.MAP_DATA) return;
    if (mapPage) mapPage.classList.remove('is-expanded');
    mapWinBtns.forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.mwin === mapWindow); }});
    var data = window.MAP_DATA;
    var themes = data.themes || [];
    var zones = data.zones || [];
    var straddlers = data.straddlers || [];
    var cap = mapAbsCap(themes.map(function(t) {{ return t[mapWindow]; }}));
    var W = Math.max(900, (mapBody.clientWidth || 1200));
    var H = Math.max(620, (mapBody.clientHeight || 760));

    // Designed narrative backbone: zone-hub anchor positions (fractional).
    var ZP = {{
      crypto:[0.11,0.17], hub:[0.33,0.17], infrastructure:[0.33,0.40],
      power:[0.33,0.63], materials:[0.31,0.85], output:[0.76,0.26],
      adjacent:[0.63,0.62], noise:[0.86,0.86]
    }};
    var BACK = [
      ['materials','power','chain'], ['power','infrastructure','chain'],
      ['infrastructure','hub','chain'], ['hub','output','enable'],
      ['power','adjacent','branch'], ['hub','crypto','straddle']
    ];
    var zoneById = {{}}; zones.forEach(function(z) {{ zoneById[z.id] = z; }});

    var nodes = [], nodeIndex = {{}};
    function addNode(o) {{ nodes.push(o); nodeIndex[o.id] = o; }}
    Object.keys(ZP).forEach(function(z) {{
      var zd = zoneById[z] || {{}};
      addNode({{ id:'zone_'+z, zone:z, hub:true, fixed:true,
        x: ZP[z][0]*W, y: ZP[z][1]*H, r: 16,
        label:(zd.label||z).split('—')[0].split('·')[0].trim(),
        flow: zd.flow, rs: (zd[mapWindow] != null ? zd[mapWindow] : zd.rs20) }});
    }});
    var maxN = 1; themes.forEach(function(t) {{ if (t.n > maxN) maxN = t.n; }});
    themes.forEach(function(t, idx) {{
      var anchor = nodeIndex['zone_'+t.zone]; if (!anchor) return;
      var ang = idx * 2.399963;   // golden angle — deterministic spread
      addNode({{ id:t.id, zone:t.zone, hub:false, data:t,
        x: anchor.x + Math.cos(ang)*55, y: anchor.y + Math.sin(ang)*55,
        r: 7 + 17*Math.sqrt(t.n/maxN) }});
    }});

    // Force relaxation: leaves repel each other + spring to their own hub.
    for (var it=0; it<150; it++) {{
      for (var a=0; a<nodes.length; a++) {{
        var na = nodes[a];
        for (var b=a+1; b<nodes.length; b++) {{
          var nb = nodes[b];
          var dx = na.x-nb.x, dy = na.y-nb.y, d2 = dx*dx+dy*dy+0.01, d = Math.sqrt(d2);
          var rep = (na.r*nb.r*0.85)/d2, mind = na.r+nb.r+7;
          if (d < mind) rep += (mind-d)*0.5/d;
          var fx = dx/d*rep, fy = dy/d*rep;
          if (!na.fixed) {{ na.x += fx; na.y += fy; }}
          if (!nb.fixed) {{ nb.x -= fx; nb.y -= fy; }}
        }}
      }}
      for (var c=0; c<nodes.length; c++) {{
        var nc = nodes[c]; if (nc.fixed) continue;
        var hb = nodeIndex['zone_'+nc.zone]; if (!hb) continue;
        var ex = hb.x-nc.x, ey = hb.y-nc.y, ed = Math.sqrt(ex*ex+ey*ey)+0.01;
        var k = (ed-(hb.r+nc.r+26))*0.06;
        nc.x += ex/ed*k; nc.y += ey/ed*k;
        nc.x = Math.max(nc.r+4, Math.min(W-nc.r-4, nc.x));
        nc.y = Math.max(nc.r+26, Math.min(H-nc.r-6, nc.y));
      }}
    }}

    var svg = svgEl('svg', {{width:W, height:H, viewBox:'0 0 '+W+' '+H, 'class':'map-graph'}});
    // spokes (theme -> its hub)
    nodes.forEach(function(n) {{
      if (n.fixed) return;
      var hb = nodeIndex['zone_'+n.zone]; if (!hb) return;
      svg.appendChild(svgEl('line', {{x1:hb.x,y1:hb.y,x2:n.x,y2:n.y, stroke:'rgba(120,124,132,0.16)','stroke-width':1}}));
    }});
    // backbone edges
    BACK.forEach(function(e) {{
      var s = nodeIndex['zone_'+e[0]], t = nodeIndex['zone_'+e[1]]; if (!s || !t) return;
      var mx = (s.x+t.x)/2, my = (s.y+t.y)/2 - 24;
      svg.appendChild(svgEl('path', {{d:'M'+s.x+','+s.y+' Q'+mx+','+my+' '+t.x+','+t.y, fill:'none',
        stroke: mapFlowColor((zoneById[e[1]]||{{}}).flow), 'stroke-width': e[2]==='chain'?3:2,
        'stroke-dasharray': e[2]==='straddle'?'5 4':''}}));
      if (e[2]==='enable') {{
        var tx = svgEl('text', {{x:mx, y:my-4, fill:'#ffcc00', 'font-size':10, 'text-anchor':'middle'}});
        tx.textContent = 'AI enables →'; svg.appendChild(tx);
      }}
      if (e[2]==='straddle') {{
        var sx = svgEl('text', {{x:mx, y:my-4, fill:'#ffcc00', 'font-size':9, 'text-anchor':'middle'}});
        sx.textContent = '↔ straddle'; svg.appendChild(sx);
      }}
    }});
    // nodes
    nodes.forEach(function(n) {{
      var g = svgEl('g', {{'class':'map-node'+(n.hub?' mn-hub':''), 'data-theme-id': n.hub?'':n.id}});
      if (n.hub) {{
        var zc = n.flow==='in'?'#1eff1e':(n.flow==='out'?'#ff3030':'#888c92');
        g.appendChild(svgEl('circle', {{cx:n.x,cy:n.y,r:n.r, fill:'#15151a', stroke:zc, 'stroke-width':2}}));
        var h1 = svgEl('text', {{x:n.x, y:n.y-n.r-7, fill:'#fff','font-size':12,'font-weight':'700','text-anchor':'middle'}});
        h1.textContent = n.label.toUpperCase(); g.appendChild(h1);
        var h2 = svgEl('text', {{x:n.x, y:n.y+4, fill:zc,'font-size':10,'font-weight':'700','text-anchor':'middle','class':'mono'}});
        h2.textContent = (n.flow==='in'?'▲':(n.flow==='out'?'▼':'·')) + ' ' + mapFmt(n.rs); g.appendChild(h2);
      }} else {{
        var v = n.data[mapWindow], col = mapColor(v, cap) || '#161618';
        var isStr = straddlers.indexOf(n.id) >= 0;
        g.setAttribute('data-label', n.data.label);
        g.setAttribute('data-tip', n.data.drift ? ('⚠ DRIFT: '+n.data.drift+(n.data.narrative?('  —  '+n.data.narrative):'')) : (n.data.narrative||''));
        var circ = svgEl('circle', {{cx:n.x,cy:n.y,r:n.r, fill:col,
          stroke:(n.data.drift||isStr)?'#ffcc00':'rgba(255,255,255,0.28)', 'stroke-width':(n.data.drift?2:1)}});
        if (isStr) circ.setAttribute('stroke-dasharray','3 2');
        var ttl = svgEl('title'); ttl.textContent = (n.data.drift?'⚠ ':'')+n.data.label+' ('+mapFmt(v)+')'+(n.data.narrative?('\\n'+n.data.narrative):''); circ.appendChild(ttl);
        g.appendChild(circ);
        if (n.r >= 13) {{
          var lt = svgEl('text', {{x:n.x, y:n.y+3, fill:'#fff','font-size':9,'text-anchor':'middle','pointer-events':'none'}});
          lt.setAttribute('style','text-shadow:0 1px 2px #000,0 0 2px #000');
          lt.textContent = (n.data.drift?'⚠':'') + mapShortLabel(n.data.label); g.appendChild(lt);
        }}
      }}
      svg.appendChild(g);
    }});
    mapBody.innerHTML = '';
    mapBody.appendChild(svg);
  }}
  function mapOpenExpand(themeId, label, narrative) {{
    if (!mapPage || !mapExpandBody) return;
    mapExpandTheme = themeId;
    var section = document.getElementById(themeId);
    var grid = section ? section.querySelector('.member-grid') : null;
    mapExpandBody.innerHTML = '';
    if (grid) mapExpandBody.appendChild(grid.cloneNode(true));
    else mapExpandBody.textContent = 'No member charts available for this theme.';
    if (mapExpandTitle) mapExpandTitle.textContent = label || themeId;
    if (mapExpandNarr) mapExpandNarr.textContent = narrative || '';
    mapPage.classList.add('is-expanded');
    mapExpandBody.scrollTop = 0;
  }}
  function mapCloseExpand() {{ if (mapPage) mapPage.classList.remove('is-expanded'); }}
  if (mapBody) {{
    mapBody.addEventListener('click', function(e) {{
      var node = e.target && e.target.closest ? e.target.closest('.map-node') : null;
      if (!node || !node.dataset || !node.dataset.themeId) return;
      mapOpenExpand(node.dataset.themeId, node.dataset.label || node.dataset.themeId, node.dataset.tip || '');
    }});
  }}
  var mapBackBtn = document.getElementById('map-back-btn');
  if (mapBackBtn) mapBackBtn.addEventListener('click', mapCloseExpand);
  var mapViewChartBtn = document.getElementById('map-viewchart-btn');
  if (mapViewChartBtn) mapViewChartBtn.addEventListener('click', function() {{
    if (!mapExpandTheme) return;
    var id = mapExpandTheme;
    setThemesView('chart');
    setActiveByRowId(id);
  }});
  if (mapExpandBody) {{
    mapExpandBody.addEventListener('click', function(e) {{
      var card = e.target && e.target.closest ? e.target.closest('.member-card') : null;
      if (!card || !card.dataset.ticker) return;
      setView('tickers', {{preserveActive: true}});
      setActiveByRowId('tk__' + card.dataset.ticker);
    }});
  }}
  mapWinBtns.forEach(function(b) {{
    b.addEventListener('click', function() {{
      mapWindow = b.dataset.mwin;
      try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.mapWindow', mapWindow); }} catch(e) {{}}
      renderMap();
    }});
  }});

  // ── Themes View toggle: Chart / Heatmap / History ───────────
  function setThemesView(mode) {{
    if (mode !== 'chart' && mode !== 'heatmap' && mode !== 'history' && mode !== 'rotation' && mode !== 'map') mode = 'chart';
    themesView = mode;
    try {{ if (window.localStorage) window.localStorage.setItem('themeDashboard.themesView', mode); }} catch(e) {{}}
    setView('themes', {{preserveActive: true}});
  }}
  viewBtns.forEach(function(b) {{
    b.addEventListener('click', function() {{ setThemesView(b.dataset.tv); }});
  }});

  // ── Historical relative-strength chart (one line per flagged theme) ──
  var HISTORY_COLORS = ['#5fc8ff','#ff8800','#1eff1e','#ff3030','#ffcc00','#cc88ff','#e8c890','#33e0c0','#ff6ec7','#9acd32','#ffffff','#7fb0ff','#ffa64d','#66d9ff'];
  // History view: four selectable metrics, a length selector, and a simple
  // moving-average smoother — all persisted. Each series is per flagged theme.
  var HIST_META = {{
    rs:   {{ title: 'Relative strength · flagged themes', yaxis: 'RS · start = 100',  fmt: '.1f', reindex: 'base100' }},
    ext:  {{ title: 'ADR extension from 50 SMA · flagged themes', yaxis: 'ADRs from 50 SMA', fmt: '.2f', reindex: 'none' }},
    rvol: {{ title: 'Relative volume · flagged themes', yaxis: 'RVOL (1.0 = avg)', fmt: '.2f', reindex: 'none' }},
    move: {{ title: '% move · flagged themes', yaxis: '% from start', fmt: '.1f', reindex: 'pct' }}
  }};
  var histMetric = 'rs', histBars = 65, histSmooth = 1;
  try {{
    var LSH = window.localStorage;
    if (LSH) {{
      var _hm = LSH.getItem('themeDashboard.histMetric'); if (HIST_META[_hm]) histMetric = _hm;
      var _hb = parseInt(LSH.getItem('themeDashboard.histBars'), 10); if (_hb === 20 || _hb === 65 || _hb === 130) histBars = _hb;
      var _hs = parseInt(LSH.getItem('themeDashboard.histSmooth'), 10); if (_hs >= 1 && _hs <= 10) histSmooth = _hs;
    }}
  }} catch(e) {{}}
  function histPersist() {{
    try {{ if (window.localStorage) {{
      window.localStorage.setItem('themeDashboard.histMetric', histMetric);
      window.localStorage.setItem('themeDashboard.histBars', String(histBars));
      window.localStorage.setItem('themeDashboard.histSmooth', String(histSmooth));
    }} }} catch(e) {{}}
  }}
  // Simple N-period moving average, null-aware, expanding at the start so the line
  // spans the whole window rather than clipping the first N-1 points.
  function histSmoothArr(arr, period) {{
    if (period <= 1) return arr.slice();
    var out = new Array(arr.length);
    for (var i = 0; i < arr.length; i++) {{
      var sum = 0, cnt = 0;
      for (var j = Math.max(0, i - period + 1); j <= i; j++) {{
        var v = arr[j]; if (v != null && !isNaN(v)) {{ sum += v; cnt++; }}
      }}
      out[i] = cnt ? (sum / cnt) : null;
    }}
    return out;
  }}
  function histSyncControls() {{
    document.querySelectorAll('.hist-btn[data-hmetric]').forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.hmetric === histMetric); }});
    document.querySelectorAll('.hist-btn[data-hbars]').forEach(function(b) {{ b.classList.toggle('is-active', +b.dataset.hbars === histBars); }});
    var sl = document.getElementById('hist-smooth'); if (sl) sl.value = histSmooth;
    var sv = document.getElementById('hist-smooth-val'); if (sv) sv.textContent = (histSmooth <= 1) ? 'raw' : (histSmooth + 'd');
    var ti = document.getElementById('history-title'); if (ti) ti.textContent = HIST_META[histMetric].title;
  }}
  function renderHistory() {{
    var div = document.getElementById('history-chart');
    if (!div || !window.Plotly || !window.HISTORY_SERIES || !window.HISTORY_DATES) return;
    histSyncControls();
    var meta = HIST_META[histMetric] || HIST_META.rs;
    var ids = [];
    flaggedThemes.forEach(function(id) {{ if (window.HISTORY_SERIES[id]) ids.push(id); }});
    ids.sort(function(a, b) {{
      var la = (window.THEME_LABELS && window.THEME_LABELS[a]) || a;
      var lb = (window.THEME_LABELS && window.THEME_LABELS[b]) || b;
      return la < lb ? -1 : (la > lb ? 1 : 0);
    }});
    if (!ids.length) {{
      if (window.Plotly.purge) {{ try {{ Plotly.purge(div); }} catch(e) {{}} }}
      div.innerHTML = '<div class="history-empty">No themes flagged yet. Switch to Chart view and click the flag next to any theme in the watchlist to plot it here.</div>';
      return;
    }}
    var nAll = window.HISTORY_DATES.length;
    var start = Math.max(0, nAll - histBars);
    var xdates = window.HISTORY_DATES.slice(start);
    var traces = ids.map(function(id, i) {{
      var lbl = (window.THEME_LABELS && window.THEME_LABELS[id]) || id;
      var raw = (window.HISTORY_SERIES[id][histMetric] || []).slice(start);
      var y;
      if (meta.reindex === 'base100' || meta.reindex === 'pct') {{
        var base = null;
        for (var b = 0; b < raw.length; b++) {{ if (raw[b] != null && !isNaN(raw[b]) && raw[b] !== 0) {{ base = raw[b]; break; }} }}
        y = raw.map(function(v) {{
          if (v == null || isNaN(v) || base == null) return null;
          return (meta.reindex === 'base100') ? (v / base * 100.0) : ((v / base - 1.0) * 100.0);
        }});
      }} else {{
        y = raw.slice();
      }}
      y = histSmoothArr(y, histSmooth);
      return {{
        type: 'scatter', mode: 'lines', x: xdates, y: y,
        name: lbl, connectgaps: false,
        line: {{ color: HISTORY_COLORS[i % HISTORY_COLORS.length], width: 1.6 }},
        hovertemplate: '%{{y:' + meta.fmt + '}}<extra>' + lbl + '</extra>'
      }};
    }});
    var layout = {{
      paper_bgcolor: '#000', plot_bgcolor: '#000',
      margin: {{ l: 50, r: 16, t: 8, b: 34 }},
      xaxis: {{ type: 'date', gridcolor: '#1a1a1c', color: '#aaa' }},
      yaxis: {{ gridcolor: '#1a1a1c', color: '#aaa', title: {{ text: meta.yaxis, font: {{ size: 11, color: '#aaa' }} }} }},
      legend: {{ orientation: 'h', y: 1.06, font: {{ color: '#ddd', size: 11 }} }},
      showlegend: true, hovermode: 'x unified'
    }};
    Plotly.newPlot(div, traces, layout, {{ displayModeBar: false, responsive: true }});
  }}
  // History control wiring — metric / length buttons + smoothing slider.
  (function() {{
    document.querySelectorAll('.hist-btn[data-hmetric]').forEach(function(b) {{
      b.addEventListener('click', function() {{ histMetric = b.dataset.hmetric; histPersist(); renderHistory(); }});
    }});
    document.querySelectorAll('.hist-btn[data-hbars]').forEach(function(b) {{
      b.addEventListener('click', function() {{ histBars = +b.dataset.hbars; histPersist(); renderHistory(); }});
    }});
    var sl = document.getElementById('hist-smooth');
    if (sl) sl.addEventListener('input', function() {{ histSmooth = +sl.value; histPersist(); renderHistory(); }});
    histSyncControls();
  }})();

  // ── Rotation (RRG) map ──────────────────────────────────────
  var ROT_QUAD_COLORS = {{ leading: '#1eff1e', weakening: '#ffcc00', lagging: '#ff3030', improving: '#5fc8ff' }};
  var ROT_QUAD_RGB = {{ leading: [30, 255, 30], weakening: [255, 204, 0], lagging: [255, 48, 48], improving: [95, 200, 255] }};
  // Pre-built rgba stroke strings (quad × alpha levels) so the per-frame trail
  // draw LOOKS UP a colour string instead of allocating one per segment (~3000 a
  // frame). 24 levels reads as a continuous fade.
  var ROT_TRAIL_ALEVELS = 24;
  var _rotStroke = {{}};
  (function() {{
    Object.keys(ROT_QUAD_RGB).forEach(function(q) {{
      var c = ROT_QUAD_RGB[q], arr = [];
      for (var l = 0; l <= ROT_TRAIL_ALEVELS; l++) arr.push('rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (l / ROT_TRAIL_ALEVELS).toFixed(3) + ')');
      _rotStroke[q] = arr;
    }});
  }})();
  var ROT_COMET_LEN = 6;   // ~5-day fading trail behind each dot (head + 5 prior days)
  function rotHexA(hex, a) {{
    var h = hex.replace('#', '');
    return 'rgba(' + parseInt(h.substr(0, 2), 16) + ',' + parseInt(h.substr(2, 2), 16) + ',' + parseInt(h.substr(4, 2), 16) + ',' + a + ')';
  }}
  // Tails: a smooth, marker-free line per theme that fades from the dot backward
  // over the last ROT_COMET_LEN days (no dots on the trail). EACH THEME OWNS ITS
  // OWN traces (one per fade band) — never grouped or null-joined across themes —
  // so trails can't cross-connect or scramble colours, and because Plotly's
  // spline is local, sliding the window one day only nudges the head/tail, never
  // warps the middle. Colour is locked to the theme's quadrant.
  var ROT_TRAIL_SAMPLES = 44;    // dense points along the ~5-day Catmull-Rom trail (canvas: smooth curve + smooth fade)
  // Build the comet trace data for the CONTINUOUS day position di+frac (same
  // fraction the dots interpolate with) so trails glide smoothly with their dots
  // instead of snapping per day. To stay fast, ALL themes share a fixed set of 12
  // traces — one per (quadrant × fade band) — bucketed by the theme's own quadrant
  // (colours never shuffle) and null-separated between themes. Rendered LINEAR so
  // the null gaps break cleanly (no cross-theme lines) and nothing overshoots.
  // Older points ride the smoothed path; the head rides the raw path (= the dot).
  //
  // Smoothly interpolate arr at the continuous index idx+frac with UNIFORM
  // Catmull-Rom. Closed-form (the standard basis), and ALLOCATION-FREE: it writes
  // the result into a single shared scratch (_rotPt) that the caller reads
  // immediately. The old version built ~6 little [x,y] arrays per call; at
  // ~3000 calls per scrub frame that churned ~20k arrays/frame and the periodic
  // GC pauses showed up as the "freeze" at day boundaries. Uniform spacing →
  // continuous speed across the joins; overshoot is negligible for this data.
  var _rotPt = [0, 0];
  function rotCRAt(arr, idx, frac) {{
    var p1 = (idx >= 0 && idx < arr.length) ? arr[idx] : null;
    var p2 = (idx + 1 >= 0 && idx + 1 < arr.length) ? arr[idx + 1] : null;
    if (!p1 && !p2) return null;
    if (!p1) return p2;
    if (!p2) return p1;
    var p0 = (idx - 1 >= 0 && arr[idx - 1]) ? arr[idx - 1] : p1;
    var p3 = (idx + 2 < arr.length && arr[idx + 2]) ? arr[idx + 2] : p2;
    var t = frac, t2 = t * t, t3 = t2 * t;
    _rotPt[0] = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3);
    _rotPt[1] = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3);
    return _rotPt;
  }}
  // Draw the WHOLE rotation frame (trails + dots) onto the canvas at the
  // continuous day di+frac. Allocation-free per frame (no Plotly restyle, no temp
  // arrays/strings) so there is nothing for GC to periodically stall on — that
  // periodic stall was the boundary "freeze". Plotly only paints the static
  // chrome (axes / grid / quad shading / corner labels) underneath; the canvas
  // (on top) paints everything that moves. The trail piece colours each tiny
  // segment by the quadrant it sits in (multicolour) with a continuous fade alpha;
  // the dot piece replaces what Plotly markers used to do.
  function rotDrawFrame(di, frac, k) {{
    var div = document.getElementById('rotation-chart');
    var cv = document.getElementById('rotation-trail-canvas');
    if (!div || !cv) return;
    var fl = div._fullLayout;
    if (!fl || !fl.xaxis || !fl.yaxis) return;
    var xa = fl.xaxis, ya = fl.yaxis;
    var ctx = cv.getContext('2d');
    var w = div.clientWidth, h = div.clientHeight;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {{
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      cv.style.width = w + 'px'; cv.style.height = h + 'px';
    }}
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var span = ROT_COMET_LEN - 1, M = ROT_TRAIL_SAMPLES;
    var shown = window.ROTATION_DATA || [];
    var ti, d, p, X, Y;
    // ── Trails ──
    if (rotTails) {{
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      for (ti = 0; ti < shown.length; ti++) {{
        d = shown[ti];
        if (!d.tail || !rotVisible(d)) continue;
        var pX = null, pY = null, pA = 1;
        for (var i = 0; i < M; i++) {{
          var age = (M - 1 - i) / (M - 1);
          var s = (di + frac) - age * span;
          var fr = Math.floor(s);
          p = rotCRAt(d.tail, fr, s - fr);
          if (!p) {{ pX = null; continue; }}
          X = xa._offset + xa.l2p(p[0]);
          Y = ya._offset + ya.l2p(p[1]);
          if (pX != null) {{
            var opa = 1 - (age + pA) / 2;             // 1 at head → 0 at tail (continuous)
            if (opa > 0.04) {{
              var stc = _rotStroke[rotQuadOfPos(p[0], p[1]) || d.quad] || _rotStroke.leading;
              ctx.strokeStyle = stc[opa >= 1 ? ROT_TRAIL_ALEVELS : (opa * ROT_TRAIL_ALEVELS) | 0];
              ctx.lineWidth = 0.8 + opa * 1.9;        // taper: thicker at head
              ctx.beginPath(); ctx.moveTo(pX, pY); ctx.lineTo(X, Y); ctx.stroke();
            }}
          }}
          pX = X; pY = Y; pA = age;
        }}
      }}
    }}
    // ── Dots (replaces the per-frame Plotly marker restyle) ──
    var emph = rotEmphAtK(shown, k);
    ctx.lineWidth = 1.5; ctx.strokeStyle = '#fff';
    ctx.font = '10px "Segoe UI", sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
    var labels = null;
    for (ti = 0; ti < shown.length; ti++) {{
      d = shown[ti];
      if (!rotVisible(d)) continue;
      p = (d.tail) ? rotCRAt(d.tail, di, frac) : null;
      if (!p) continue;
      var dxv = p[0], dyv = p[1];
      X = xa._offset + xa.l2p(dxv);
      Y = ya._offset + ya.l2p(dyv);
      var e = !!emph[d.id];
      var bd = rotInterpArr(d.breadthp, di, frac, (d.breadth == null ? null : d.breadth));
      var rv = rotInterpArr(d.rvolp, di, frac, d.rvol);
      var sz = (rotSize === 'breadth') ? (6 + (bd || 0) * 16)
             : (rotSize === 'n') ? (6 + Math.min(d.n, 30) / 30 * 16)
             : (rotSize === 'rvol') ? rotRvolSize(rv)
             : 9;
      var rad = sz / 2;
      ctx.globalAlpha = e ? 1 : (rotEmph === 'none' ? 0.85 : 0.25);
      ctx.fillStyle = (rotColor === 'breadth') ? rotBreadthColor(bd) : (ROT_QUAD_COLORS[rotQuadOfPos(dxv, dyv)] || ROT_QUAD_COLORS[d.quad]);
      ctx.beginPath(); ctx.arc(X, Y, rad, 0, 6.283185); ctx.fill();
      if (e) {{ ctx.globalAlpha = 1; ctx.stroke(); }}           // white outline on emphasized
      // Labels: every visible dot when the toggle is on (thin the field with the
      // Thrust slider). Independent of emphasis — emphasis only brightens dots.
      if (rotLabels) {{ (labels || (labels = [])).push(d.label, X, Y - rad - 5); }}
    }}
    ctx.globalAlpha = 1;
    if (labels) {{ ctx.fillStyle = '#ddd'; for (ti = 0; ti < labels.length; ti += 3) ctx.fillText(labels[ti], labels[ti + 1], labels[ti + 2]); }}
  }}
  // "Just turning" rank, weighted by relative volume so volume-backed turns lead
  // and quiet curls sink; the x>0 penalty keeps weak (left-side) themes on top.
  function rotTurnScore(d) {{ return (d.turn * (d.rvol == null ? 1 : d.rvol)) - (d.x > 0 ? 1000 : 0); }}
  // ── Per-day versions (computed off the path) so emphasis, colour and the
  //    quadrant a theme sits in all reflect the day the scrubber is on, not today.
  var ROT_TURN_LB = 10;
  function rotPosTuple(d, k) {{ return (d.tail && k >= 0 && k < d.tail.length && d.tail[k]) ? d.tail[k] : null; }}
  function rotQuadAtK(d, k) {{
    var p = rotPosTuple(d, k) || [d.x, d.y];
    return (p[0] >= 0 && p[1] >= 0) ? 'leading' : (p[0] >= 0 && p[1] < 0) ? 'weakening' : (p[0] < 0 && p[1] < 0) ? 'lagging' : 'improving';
  }}
  function rotTurnAtK(d, k) {{
    var a = rotPosTuple(d, k), b = rotPosTuple(d, k - ROT_TURN_LB);
    if (!a) return -1e9;
    var turn = b ? (a[1] - b[1]) : 0;
    var rv = (d.rvolp && d.rvolp[k] != null) ? d.rvolp[k] : (d.rvol == null ? 1 : d.rvol);
    return (turn * rv) - (a[0] > 0 ? 1000 : 0);
  }}
  function rotMoverAtK(d, k) {{
    var a = rotPosTuple(d, k), b = rotPosTuple(d, k - ROT_TURN_LB);
    if (!a || !b) return 0;
    return Math.sqrt((a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]));
  }}
  // Strength at day k = the dot's x (strength axis) position that day.
  function rotStrengthAtK(d, k) {{ var p = rotPosTuple(d, k); return p ? p[0] : (d.x == null ? -1e9 : d.x); }}
  // Breadth at day k = % of members above their 20-day SMA that day (per-day
  // series if present, else the current-day scalar).
  function rotBreadthAtK(d, k) {{ return (d.breadthp && d.breadthp[k] != null) ? d.breadthp[k] : (d.breadth == null ? null : d.breadth); }}
  function rotEmphAtK(shownList, k) {{
    var set = {{}};
    flaggedThemes.forEach(function(id) {{ set[id] = true; }});
    if (rotEmph !== 'none') {{
      var sc = shownList.map(function(d) {{
        var s = (rotEmph === 'movers') ? rotMoverAtK(d, k)
              : (rotEmph === 'leaders') ? rotStrengthAtK(d, k)
              : rotTurnAtK(d, k);
        return {{ id: d.id, s: s }};
      }});
      sc.sort(function(a, b) {{ return b.s - a.s; }});
      sc.slice(0, 10).forEach(function(o) {{ set[o.id] = true; }});
    }}
    return set;
  }}
  // Square curve so the spread is exaggerated: quiet themes (rvol<1) shrink, loud
  // themes (rvol>1.5) balloon. rvol 0.7→~5, 1.0→~7.5, 1.7→~16, 2.5→~31, 2.8→~38.
  function rotRvolSize(rv) {{ var v = Math.min(Math.max(rv == null ? 1 : rv, 0.3), 2.8); return 3 + v * v * 4.5; }}
  var rotScrubState = null;  // {{shown}} stashed by renderRotation for the scrubber
  var rotCurDi = 0, rotCurFrac = 0, rotCurK = 0;  // current scrub position, so redraws (resize/click) use the right day
  var rotOverlaySection = null;  // theme <section> currently lifted into the click overlay
  function rotCloseOverlay() {{
    var ov = document.getElementById('rot-overlay');
    if (ov) ov.style.display = 'none';
    if (rotOverlaySection) {{
      rotOverlaySection.style.display = 'none';     // sections live hidden in <main>
      var mainEl = document.querySelector('main');
      if (mainEl) mainEl.appendChild(rotOverlaySection);  // drop it back where it belongs
      rotOverlaySection = null;
    }}
  }}
  function rotOpenOverlay(themeId, label) {{
    var ov = document.getElementById('rot-overlay');
    var body = document.getElementById('rot-overlay-body');
    var section = document.getElementById(themeId);
    if (!ov || !body || !section) return;
    rotCloseOverlay();
    rotOverlaySection = section;
    body.appendChild(section);                       // lift the real section (chart + thumbnails)
    section.style.display = 'block';
    var ttl = document.getElementById('rot-overlay-title');
    if (ttl) ttl.textContent = label || themeId;
    ov.style.display = 'flex';
    body.scrollTop = 0;
    rotApplyOverlayFilters();                         // synthetic toggle + thumbnail filters + sub-panels
    var pdiv = section.querySelector('.plotly-graph-div');
    if (pdiv && window.Plotly && Plotly.Plots && Plotly.Plots.resize) {{
      window.setTimeout(function() {{ try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}} }}, 40);
    }}
  }}
  var rotEmph = 'turns', rotColor = 'quad', rotSize = 'rvol', rotSide = 'turn';
  var rotTails = true, rotLabels = true, rotTopHalf = false;
  var rotThrustMin = 0;   // hide themes whose thrust percentile is below this (0 = show all)
  // Ball-overlay (click-a-dot) filter toolbar state. Persisted so every theme
  // ball opens with the same filters. Synthetic chart on by default; rest off.
  var ovfSynth = true, ovfMomo = false, ovfTight = false, ovfMacd = false, ovfExt = false;
  // A theme passes the thrust filter if its climb rank is at/above the slider %.
  function rotThrustVisible(d) {{ return (d.thrustRank == null) ? true : (d.thrustRank * 100 >= rotThrustMin - 0.0001); }}
  // A theme is drawn on the map only if it passes BOTH the thrust slider and the
  // gear-panel sector/theme exclusions (the same filter the ticker page uses).
  function rotVisible(d) {{ return rotThrustVisible(d) && themeRowPassesFilter(d.id); }}
  function rotUpdateThrustReadout() {{
    var el = document.getElementById('rot-thrust-val'); if (!el) return;
    var n = 0, list = window.ROTATION_DATA || [];
    for (var i = 0; i < list.length; i++) if (rotVisible(list[i])) n++;
    el.textContent = (n >= list.length) ? 'all' : (n + ' shown');
  }}
  try {{
    var LS = window.localStorage;
    if (LS) {{
      rotEmph  = LS.getItem('themeDashboard.rotEmph')  || rotEmph;
      rotColor = LS.getItem('themeDashboard.rotColor') || rotColor;
      rotSize  = LS.getItem('themeDashboard.rotSize')  || rotSize;
      rotSide  = LS.getItem('themeDashboard.rotSide')  || rotSide;
      if (LS.getItem('themeDashboard.rotTails') === '0') rotTails = false;
      if (LS.getItem('themeDashboard.rotLabels') === '0') rotLabels = false;
      if (LS.getItem('themeDashboard.rotTopHalf') === '1') rotTopHalf = true;
      var _rtm = LS.getItem('themeDashboard.rotThrustMin'); if (_rtm != null) {{ var _v = parseInt(_rtm, 10); if (_v >= 0 && _v <= 100) rotThrustMin = _v; }}
      if (LS.getItem('themeDashboard.ovfSynth') === '0') ovfSynth = false;
      if (LS.getItem('themeDashboard.ovfMomo')  === '1') ovfMomo  = true;
      if (LS.getItem('themeDashboard.ovfTight') === '1') ovfTight = true;
      if (LS.getItem('themeDashboard.ovfMacd')  === '1') ovfMacd  = true;
      if (LS.getItem('themeDashboard.ovfExt')   === '1') ovfExt   = true;
    }}
  }} catch(e) {{}}
  function rotPersist() {{
    try {{
      var LS2 = window.localStorage; if (!LS2) return;
      LS2.setItem('themeDashboard.rotEmph', rotEmph);
      LS2.setItem('themeDashboard.rotColor', rotColor);
      LS2.setItem('themeDashboard.rotSize', rotSize);
      LS2.setItem('themeDashboard.rotSide', rotSide);
      LS2.setItem('themeDashboard.rotTails', rotTails ? '1' : '0');
      LS2.setItem('themeDashboard.rotLabels', rotLabels ? '1' : '0');
      LS2.setItem('themeDashboard.rotTopHalf', rotTopHalf ? '1' : '0');
      LS2.setItem('themeDashboard.rotThrustMin', String(rotThrustMin));
      LS2.setItem('themeDashboard.ovfSynth', ovfSynth ? '1' : '0');
      LS2.setItem('themeDashboard.ovfMomo',  ovfMomo  ? '1' : '0');
      LS2.setItem('themeDashboard.ovfTight', ovfTight ? '1' : '0');
      LS2.setItem('themeDashboard.ovfMacd',  ovfMacd  ? '1' : '0');
      LS2.setItem('themeDashboard.ovfExt',   ovfExt   ? '1' : '0');
    }} catch(e) {{}}
  }}
  // ── Ball-overlay filter toolbar ──────────────────────────────
  // Reflect the toolbar buttons' active state from the persisted toggles.
  function rotOvfSyncButtons() {{
    var map = {{ synth: ovfSynth, momo: ovfMomo, tight: ovfTight, macd: ovfMacd, ext: ovfExt }};
    document.querySelectorAll('.rot-btn[data-ovf]').forEach(function(b) {{
      b.classList.toggle('is-active', !!map[b.dataset.ovf]);
    }});
  }}
  function rotClearMemberPanels(card) {{
    var olds = card.querySelectorAll('.member-sub-panel');
    for (var i = 0; i < olds.length; i++) olds[i].parentNode.removeChild(olds[i]);
  }}
  // MACD (6/20/9) mini-panel — same math as the per-ticker chart (emaJS/subArr),
  // drawn over the last 100 bars to line up under the thumbnail candles.
  function rotBuildMacdCanvas(d, w) {{
    var h = 54, cv = document.createElement('canvas');
    cv.className = 'member-sub-panel';
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.width = w + 'px'; cv.style.height = h + 'px';
    var ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    var close = d.close; if (!close || close.length < 2) return cv;
    var macd = subArr(emaJS(close, 6), emaJS(close, 20));
    var sig  = emaJS(macd, 9);
    var N = close.length, vis = Math.min(100, N), s = N - vis, k;
    var lo = Infinity, hi = -Infinity;
    for (k = s; k < N; k++) {{
      var trio = [macd[k], sig[k], (macd[k] != null && sig[k] != null) ? (macd[k] - sig[k]) : null];
      for (var j = 0; j < 3; j++) {{ var v = trio[j]; if (v != null && !isNaN(v)) {{ if (v < lo) lo = v; if (v > hi) hi = v; }} }}
    }}
    if (lo === Infinity) return cv;
    if (hi <= lo) hi = lo + 1;
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    var x0 = 2, cw = (w - 4) * 0.82, bw = cw / vis;
    function yp(v) {{ return (1 - (v - lo) / (hi - lo)) * (h - 2) + 1; }}
    var zy = (lo < 0 && hi > 0) ? yp(0) : yp(lo);
    if (lo < 0 && hi > 0) {{ ctx.strokeStyle = '#444'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0, zy); ctx.lineTo(x0 + cw, zy); ctx.stroke(); }}
    for (k = s; k < N; k++) {{
      var hv = (macd[k] != null && sig[k] != null) ? (macd[k] - sig[k]) : null;
      if (hv == null || isNaN(hv)) continue;
      var x = x0 + (k - s) * bw, y = yp(hv);
      ctx.fillStyle = hv >= 0 ? 'rgba(30,255,30,0.5)' : 'rgba(255,48,48,0.5)';
      ctx.fillRect(x, Math.min(y, zy), Math.max(1, bw * 0.8), Math.max(0.5, Math.abs(y - zy)));
    }}
    function line(arr, color) {{
      ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.beginPath(); var started = false;
      for (var k2 = s; k2 < N; k2++) {{
        var v = arr[k2]; if (v == null || isNaN(v)) {{ started = false; continue; }}
        var x = x0 + (k2 - s) * bw + bw * 0.5, y = yp(v);
        if (!started) {{ ctx.moveTo(x, y); started = true; }} else ctx.lineTo(x, y);
      }}
      ctx.stroke();
    }}
    line(macd, '#5fc8ff'); line(sig, '#ff8800');
    return cv;
  }}
  // 50-SMA extension mini-panel — the embedded ext50 series (ADR units), the
  // exact same series the per-ticker chart's bottom panel uses.
  function rotBuildExtCanvas(d, w) {{
    var h = 54, cv = document.createElement('canvas');
    cv.className = 'member-sub-panel';
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.width = w + 'px'; cv.style.height = h + 'px';
    var ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    var ext = d.ext50; if (!ext || !ext.length) return cv;
    var N = ext.length, vis = Math.min(100, N), s = N - vis, k;
    var lo = Infinity, hi = -Infinity;
    for (k = s; k < N; k++) {{ var v = ext[k]; if (v != null && !isNaN(v)) {{ if (v < lo) lo = v; if (v > hi) hi = v; }} }}
    if (lo === Infinity) return cv;
    if (hi <= lo) hi = lo + 1;
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    var x0 = 2, cw = (w - 4) * 0.82, bw = cw / vis;
    function yp(v) {{ return (1 - (v - lo) / (hi - lo)) * (h - 2) + 1; }}
    var zy = (lo < 0 && hi > 0) ? yp(0) : yp(lo);
    if (lo < 0 && hi > 0) {{ ctx.strokeStyle = '#444'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0, zy); ctx.lineTo(x0 + cw, zy); ctx.stroke(); }}
    for (k = s; k < N; k++) {{
      var ev = ext[k]; if (ev == null || isNaN(ev)) continue;
      var x = x0 + (k - s) * bw, y = yp(ev);
      ctx.fillStyle = ev >= 0 ? '#1eff1e' : '#ff3030';
      ctx.fillRect(x, Math.min(y, zy), Math.max(1, bw * 0.8), Math.max(0.5, Math.abs(y - zy)));
    }}
    return cv;
  }}
  function rotDrawMemberPanels(card) {{
    rotClearMemberPanels(card);
    if (!ovfMacd && !ovfExt) return;
    var tk = card.getAttribute('data-ticker');
    var d = window.TICKER_DATA && window.TICKER_DATA[tk];
    if (!d) return;
    var w = card.clientWidth || 300;
    if (ovfMacd) card.appendChild(rotBuildMacdCanvas(d, w));
    if (ovfExt)  card.appendChild(rotBuildExtCanvas(d, w));
  }}
  // Apply the toolbar to whatever theme section is lifted into the overlay.
  function rotApplyOverlayFilters() {{
    var body = document.getElementById('rot-overlay-body');
    var section = rotOverlaySection;
    if (!body || !section) return;
    body.classList.toggle('ovf-no-synth', !ovfSynth);
    if (ovfSynth) {{
      var pdiv = section.querySelector('.plotly-graph-div');   // re-show may need a resize
      if (pdiv && window.Plotly && Plotly.Plots && Plotly.Plots.resize) {{
        window.setTimeout(function() {{ try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}} }}, 30);
      }}
    }}
    var cards = section.querySelectorAll('.member-card');
    for (var i = 0; i < cards.length; i++) {{
      var card = cards[i];
      var hide = (ovfMomo && card.getAttribute('data-momo') !== '1')
              || (ovfTight && card.getAttribute('data-tight') !== '1');
      card.classList.toggle('ovf-hidden', hide);
      if (hide) rotClearMemberPanels(card); else rotDrawMemberPanels(card);
    }}
  }}
  function rotBreadthColor(b) {{
    if (b === null || b === undefined) return '#666';
    var r, g;
    if (b < 0.5) {{ r = 200; g = Math.round(60 + b * 2 * 90); }}
    else {{ r = Math.round(200 - (b - 0.5) * 2 * 160); g = 150 + Math.round((b - 0.5) * 2 * 50); }}
    return 'rgb(' + r + ',' + g + ',60)';
  }}
  // Single source of truth for one animation frame. Given the shown list, an
  // interpolation anchor (di + frac between two path days) and the discrete day
  // k it rounds to, returns parallel arrays for the dot trace. BOTH the initial
  // render and the scrubber call this, so emphasis / colour / size / breadth
  // can never again be honoured at "today" but frozen while scrubbing.
  // Interpolate a per-day array (rvolp / breadthp) at the continuous position
  // di+frac so size/breadth glide instead of snapping at each day boundary.
  function rotInterpArr(arr, di, frac, fb) {{
    if (!arr) return fb;
    var a = (di >= 0 && di < arr.length) ? arr[di] : null;
    var b = (di + 1 >= 0 && di + 1 < arr.length) ? arr[di + 1] : null;
    if (a == null && b == null) return fb;
    if (a == null) return b;
    if (b == null) return a;
    return a + (b - a) * frac;
  }}
  // Quadrant from a (continuous, interpolated) position — so the dot's colour
  // flips exactly when it crosses an axis, not in a discrete jump at day k.
  function rotQuadOfPos(x, y) {{
    if (x == null || y == null) return null;
    return (x >= 0 && y >= 0) ? 'leading' : (x >= 0 && y < 0) ? 'weakening' : (x < 0 && y < 0) ? 'lagging' : 'improving';
  }}
  // The auto-fit axis ranges renderRotation computes — kept so scroll-wheel zoom
  // can reset back to "fit" on a double-click.
  var rotFitX = null, rotFitY = null;
  function renderRotation() {{
    var div = document.getElementById('rotation-chart');
    if (!div || !window.Plotly || !window.ROTATION_DATA) return;
    document.querySelectorAll('.rot-btn[data-rot="emph"]').forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.val === rotEmph); }});
    document.querySelectorAll('.rot-btn[data-rot="color"]').forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.val === rotColor); }});
    document.querySelectorAll('.rot-btn[data-rot="size"]').forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.val === rotSize); }});
    document.querySelectorAll('.rot-btn[data-rot="tails"]').forEach(function(b) {{ b.classList.toggle('is-active', rotTails); }});
    document.querySelectorAll('.rot-btn[data-rot="labels"]').forEach(function(b) {{ b.classList.toggle('is-active', rotLabels); }});
    document.querySelectorAll('.rot-btn[data-rot="tophalf"]').forEach(function(b) {{ b.classList.toggle('is-active', rotTopHalf); }});
    document.querySelectorAll('.rot-side-btn').forEach(function(b) {{ b.classList.toggle('is-active', b.dataset.side === rotSide); }});

    var shown = window.ROTATION_DATA;   // every theme is on the map (Show filter removed)
    var lastK = (window.ROTATION_DATES || []).length - 1;
    // Axis ranges fit the data robustly and stay fixed for the whole scrub (so
    // they never jump). Use a high percentile of all path positions — a rare
    // outlier day can't blow the frame out and crush today into the middle — but
    // always keep today's dots inside. x and y are sized independently so the
    // dots fill the wide chart. (A few extreme past-day positions clip when you
    // scrub to them; bubbles are never removed.)
    function rotPctMax(extract) {{
      var vals = [];
      shown.forEach(function(d) {{ if (d.tail) d.tail.forEach(function(p) {{ if (p) {{ var v = extract(p); if (v != null) vals.push(v); }} }}); }});
      if (!vals.length) return 0;
      vals.sort(function(a, b) {{ return a - b; }});
      return vals[Math.floor(vals.length * 0.96)];
    }}
    function rotTodayMax(extract) {{
      var m = 0;
      shown.forEach(function(d) {{ var p = d.tail && d.tail[lastK]; if (p) {{ var v = extract(p); if (v != null) m = Math.max(m, v); }} }});
      return m;
    }}
    var absX = function(p) {{ return Math.abs(p[0]); }};
    var xmax = Math.max(1.2, Math.max(rotPctMax(absX), rotTodayMax(absX)) * 1.08);
    var ymax, ymin;
    if (rotTopHalf) {{
      // Only positive-momentum dots are shown, so size the top to THEM — not to
      // the (clipped) negative-momentum dots — otherwise the half-frame is mostly
      // empty. ymin pinned at 0.
      var posY = function(p) {{ return p[1] > 0 ? p[1] : null; }};
      ymax = Math.max(1.0, Math.max(rotPctMax(posY), rotTodayMax(posY)) * 1.10);
      ymin = 0;
    }} else {{
      var absY = function(p) {{ return Math.abs(p[1]); }};
      ymax = Math.max(1.2, Math.max(rotPctMax(absY), rotTodayMax(absY)) * 1.08);
      ymin = -ymax;
    }}
    rotFitX = [-xmax, xmax]; rotFitY = [ymin, ymax];   // remembered for the zoom-reset (double-click)

    // Dots + trails are painted on the canvas overlay (rotDrawFrame), allocation-
    // free, so the scrub never churns objects and stalls on GC. Plotly draws ONLY
    // the static chrome (axes / grid / quad shading / corner labels) from an empty
    // data set and is never restyled per frame.
    var traces = [];
    var layout = {{
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', margin: {{ l: 46, r: 12, t: 12, b: 38 }},
      xaxis: {{ range: [-xmax, xmax], zeroline: false, gridcolor: '#161618', color: '#888',
               title: {{ text: '← weaker      STRENGTH vs universe      stronger →', font: {{ size: 11, color: '#888' }} }} }},
      yaxis: {{ range: [ymin, ymax], zeroline: false, gridcolor: '#161618', color: '#888',
               title: {{ text: '← fading      MOMENTUM      rising →', font: {{ size: 11, color: '#888' }} }} }},
      shapes: [
        {{ type: 'rect', x0: 0, y0: 0, x1: xmax, y1: ymax, fillcolor: 'rgba(30,255,30,0.05)', line: {{ width: 0 }}, layer: 'below' }},
        {{ type: 'rect', x0: 0, y0: -ymax, x1: xmax, y1: 0, fillcolor: 'rgba(255,204,0,0.05)', line: {{ width: 0 }}, layer: 'below' }},
        {{ type: 'rect', x0: -xmax, y0: -ymax, x1: 0, y1: 0, fillcolor: 'rgba(255,48,48,0.05)', line: {{ width: 0 }}, layer: 'below' }},
        {{ type: 'rect', x0: -xmax, y0: 0, x1: 0, y1: ymax, fillcolor: 'rgba(95,200,255,0.06)', line: {{ width: 0 }}, layer: 'below' }},
        {{ type: 'line', x0: 0, y0: -ymax, x1: 0, y1: ymax, line: {{ color: '#333', width: 1 }} }},
        {{ type: 'line', x0: -xmax, y0: 0, x1: xmax, y1: 0, line: {{ color: '#333', width: 1 }} }}
      ],
      annotations: [
        {{ x: xmax, y: ymax, xanchor: 'right', yanchor: 'top', text: 'LEADING', showarrow: false, font: {{ color: 'rgba(30,255,30,0.45)', size: 12 }} }},
        {{ x: xmax, y: -ymax, xanchor: 'right', yanchor: 'bottom', text: 'WEAKENING', showarrow: false, font: {{ color: 'rgba(255,204,0,0.45)', size: 12 }} }},
        {{ x: -xmax, y: -ymax, xanchor: 'left', yanchor: 'bottom', text: 'LAGGING', showarrow: false, font: {{ color: 'rgba(255,48,48,0.45)', size: 12 }} }},
        {{ x: -xmax, y: ymax, xanchor: 'left', yanchor: 'top', text: 'IMPROVING', showarrow: false, font: {{ color: 'rgba(95,200,255,0.55)', size: 12 }} }}
      ],
      hovermode: 'closest'
    }};
    Plotly.newPlot(div, traces, layout, {{ displayModeBar: false, responsive: true }});
    // Paint the frame (dots + trails) on the canvas at "today", and repaint it
    // whenever Plotly relayouts (e.g. a resize remaps axes→pixels). Dot clicks are
    // handled by the hit-test bound once below.
    rotCurDi = lastK; rotCurFrac = 0; rotCurK = lastK;
    rotDrawFrame(lastK, 0, lastK);
    if (div.removeAllListeners) {{ try {{ div.removeAllListeners('plotly_relayout'); }} catch(e) {{}} }}
    if (div.on) div.on('plotly_relayout', function() {{ rotDrawFrame(rotCurDi, rotCurFrac, rotCurK); }});
    // Hand the analogue scrubber what it needs and reset it to "today" (right end).
    rotScrubState = {{ shown: shown }};
    var _scrubIn = document.getElementById('rot-scrub-input');
    if (_scrubIn) _scrubIn.value = _scrubIn.max;
    var _scrubDt = document.getElementById('rot-scrub-date');
    var _rdates = window.ROTATION_DATES || [];
    if (_scrubDt && _rdates.length) _scrubDt.textContent = _rdates[_rdates.length - 1];
    renderRotationSide();
  }}
  function renderRotationSide() {{
    var listDiv = document.getElementById('rotation-side-list');
    if (!listDiv || !window.ROTATION_DATA) return;
    var arr = window.ROTATION_DATA.slice();
    if (rotSide === 'turn') arr.sort(function(a, b) {{ return rotTurnScore(b) - rotTurnScore(a); }});
    else arr.sort(function(a, b) {{ return b.mover - a.mover; }});
    arr = arr.slice(0, 18);
    var html = '';
    arr.forEach(function(d) {{
      var rv = (d.rvol != null) ? d.rvol.toFixed(1) + 'x' : '';
      html += '<div class="rot-item" data-theme-id="' + d.id + '" title="relative volume (recent vs baseline)">'
            + '<span class="rot-dot" style="background:' + ROT_QUAD_COLORS[d.quad] + '"></span>'
            + '<span class="rot-name">' + d.label + '</span>'
            + '<span class="rot-bd">' + rv + '</span>'
            + '</div>';
    }});
    listDiv.innerHTML = html;
  }}
  document.querySelectorAll('.rot-btn').forEach(function(b) {{
    if (b.dataset.ovf) return;   // ball-overlay toolbar buttons are wired separately below
    b.addEventListener('click', function() {{
      var kind = b.dataset.rot;
      if (kind === 'tails') rotTails = !rotTails;
      else if (kind === 'labels') rotLabels = !rotLabels;
      else if (kind === 'tophalf') rotTopHalf = !rotTopHalf;
      else if (kind === 'emph') rotEmph = b.dataset.val;
      else if (kind === 'color') rotColor = b.dataset.val;
      else if (kind === 'size') rotSize = b.dataset.val;
      rotPersist();
      renderRotation();
    }});
  }});
  // Ball-overlay filter toolbar: toggle, persist, and re-apply to the open ball
  // (no map re-render — these only affect what's inside the overlay).
  document.querySelectorAll('.rot-btn[data-ovf]').forEach(function(b) {{
    b.addEventListener('click', function() {{
      var kind = b.dataset.ovf;
      if (kind === 'synth') ovfSynth = !ovfSynth;
      else if (kind === 'momo') ovfMomo = !ovfMomo;
      else if (kind === 'tight') ovfTight = !ovfTight;
      else if (kind === 'macd') ovfMacd = !ovfMacd;
      else if (kind === 'ext') ovfExt = !ovfExt;
      rotPersist();
      rotOvfSyncButtons();
      rotApplyOverlayFilters();
    }});
  }});
  rotOvfSyncButtons();   // reflect persisted toolbar state on load
  document.querySelectorAll('.rot-side-btn').forEach(function(b) {{
    b.addEventListener('click', function() {{ rotSide = b.dataset.side; rotPersist(); renderRotation(); }});
  }});
  var rotSideList = document.getElementById('rotation-side-list');
  if (rotSideList) {{
    rotSideList.addEventListener('click', function(e) {{
      var item = e.target && e.target.closest ? e.target.closest('.rot-item') : null;
      if (!item || !item.dataset.themeId) return;
      setThemesView('chart');
      setActiveByRowId(item.dataset.themeId);
    }});
  }}

  // ── Analogue time scrubber: interpolate positions live, push to the plot ──
  var rotScrubInput = document.getElementById('rot-scrub-input');
  if (rotScrubInput) {{
    var _scrubRaf = false;
    function rotScrubApply() {{
      _scrubRaf = false;
      if (!rotScrubState || !window.Plotly) return;
      var dates = window.ROTATION_DATES || [];
      var N = dates.length;
      if (N < 2) return;
      var t = (+rotScrubInput.value) / (+rotScrubInput.max || 1);  // 0..1
      var fidx = t * (N - 1);
      var di = Math.floor(fidx);
      if (di >= N - 1) di = N - 2;
      var frac = fidx - di;
      var k = Math.round(fidx); if (k >= N) k = N - 1; if (k < 0) k = 0;  // day for emphasis/colour
      // Repaint the whole frame (dots + trails) on the canvas at the continuous
      // di+frac — no Plotly restyle, so no per-frame GC churn / boundary freeze.
      rotCurDi = di; rotCurFrac = frac; rotCurK = k;
      rotDrawFrame(di, frac, k);
      var dd = document.getElementById('rot-scrub-date');
      if (dd) dd.textContent = dates[k];
    }}
    rotScrubInput.addEventListener('input', function() {{
      if (_scrubRaf) return;
      _scrubRaf = true;
      window.requestAnimationFrame(rotScrubApply);
    }});
  }}
  // Keep the canvas aligned with the chart on window resize (Plotly's responsive
  // resize changes the axis→pixel mapping a beat later).
  window.addEventListener('resize', function() {{
    if (activeView === 'themes' && themesView === 'rotation') {{
      window.setTimeout(function() {{ rotDrawFrame(rotCurDi, rotCurFrac, rotCurK); }}, 80);
    }}
  }});
  // Mouse interactions run on the canvas (it captures the events; the Plotly chart
  // underneath is static). Hit-test the dots at the current scrub position:
  // hover → tooltip with the theme's numbers, click → open the theme overlay.
  var _rotCanvas = document.getElementById('rotation-trail-canvas');
  var _rotChartEl = document.getElementById('rotation-chart');
  function rotHitTest(mx, my) {{
    var fl = _rotChartEl && _rotChartEl._fullLayout; if (!fl || !fl.xaxis || !fl.yaxis) return null;
    var xa = fl.xaxis, ya = fl.yaxis, shown = window.ROTATION_DATA || [], best = null, bestD = 1e9;
    for (var i = 0; i < shown.length; i++) {{
      var d = shown[i]; if (!d.tail || !rotVisible(d)) continue;
      var p = rotCRAt(d.tail, rotCurDi, rotCurFrac); if (!p) continue;
      var dxp = mx - (xa._offset + xa.l2p(p[0])), dyp = my - (ya._offset + ya.l2p(p[1]));
      var dist = dxp * dxp + dyp * dyp;
      if (dist < bestD) {{ bestD = dist; best = d; }}
    }}
    return (best && bestD < 18 * 18) ? best : null;
  }}
  function rotHideTooltip() {{ var tt = document.getElementById('rot-tooltip'); if (tt) tt.style.display = 'none'; }}
  if (_rotCanvas) {{
    _rotCanvas.addEventListener('click', function(e) {{
      var rect = _rotCanvas.getBoundingClientRect();
      var d = rotHitTest(e.clientX - rect.left, e.clientY - rect.top);
      if (d) rotOpenOverlay(d.id, d.label);
    }});
    _rotCanvas.addEventListener('mousemove', function(e) {{
      var rect = _rotCanvas.getBoundingClientRect();
      var d = rotHitTest(e.clientX - rect.left, e.clientY - rect.top);
      var tt = document.getElementById('rot-tooltip');
      if (!d || !tt) {{ rotHideTooltip(); _rotCanvas.style.cursor = 'default'; return; }}
      _rotCanvas.style.cursor = 'pointer';
      var p = rotCRAt(d.tail, rotCurDi, rotCurFrac);
      var sx = p ? p[0] : null, sy = p ? p[1] : null;
      var bd = rotInterpArr(d.breadthp, rotCurDi, rotCurFrac, d.breadth);
      var rv = rotInterpArr(d.rvolp, rotCurDi, rotCurFrac, d.rvol);
      tt.innerHTML = '<b>' + d.label + '</b><br>strength ' + (sx == null ? '–' : sx.toFixed(2)) + ' · momentum ' + (sy == null ? '–' : sy.toFixed(2))
        + (bd != null ? ('<br>breadth ' + Math.round(bd * 100) + '%') : '') + (rv != null ? (' · rvol ' + rv.toFixed(2) + 'x') : '');
      tt.style.display = 'block';
      var bodyRect = _rotCanvas.parentNode.getBoundingClientRect();
      var lx = e.clientX - bodyRect.left + 14, ty = e.clientY - bodyRect.top + 14;
      if (lx + tt.offsetWidth > bodyRect.width) lx = e.clientX - bodyRect.left - tt.offsetWidth - 12;
      if (ty + tt.offsetHeight > bodyRect.height) ty = e.clientY - bodyRect.top - tt.offsetHeight - 12;
      tt.style.left = lx + 'px'; tt.style.top = ty + 'px';
    }});
    _rotCanvas.addEventListener('mouseleave', function() {{ rotHideTooltip(); _rotCanvas.style.cursor = 'default'; }});
    // Scroll-wheel zoom, centered on the cursor. The canvas captures the wheel
    // (it sits over the Plotly chart), so we drive the zoom by relayouting the
    // chart's axis ranges; the bound plotly_relayout handler repaints the canvas.
    _rotCanvas.addEventListener('wheel', function(e) {{
      var fl = _rotChartEl && _rotChartEl._fullLayout;
      if (!fl || !fl.xaxis || !fl.yaxis) return;
      e.preventDefault();
      var xa = fl.xaxis, ya = fl.yaxis;
      var rect = _rotCanvas.getBoundingClientRect();
      var cx = xa.p2l(e.clientX - rect.left - xa._offset);   // data coords under the cursor
      var cy = ya.p2l(e.clientY - rect.top  - ya._offset);
      var xr = xa.range, yr = ya.range;
      var f = (e.deltaY < 0) ? 0.85 : (1 / 0.85);            // wheel up = zoom in, down = zoom out
      var nx0 = cx - (cx - xr[0]) * f, nx1 = cx + (xr[1] - cx) * f;
      var ny0 = cy - (cy - yr[0]) * f, ny1 = cy + (yr[1] - cy) * f;
      var xspan = nx1 - nx0, yspan = ny1 - ny0;
      if (f < 1 && (xspan < 0.08 || yspan < 0.08)) return;   // don't zoom in past a hair
      // Zoom-out is capped at the auto-fit view (nothing lives beyond it); hitting
      // the cap snaps back to the centered fit, which also re-centers any drift.
      if (f > 1 && rotFitX && rotFitY && (xspan >= rotFitX[1] - rotFitX[0] || yspan >= rotFitY[1] - rotFitY[0])) {{
        Plotly.relayout(_rotChartEl, {{ 'xaxis.range': rotFitX.slice(), 'yaxis.range': rotFitY.slice() }});
        return;
      }}
      Plotly.relayout(_rotChartEl, {{ 'xaxis.range': [nx0, nx1], 'yaxis.range': [ny0, ny1] }});
    }}, {{ passive: false }});
    // Double-click empty space to snap back to the auto-fit view. (On a dot the
    // first click opens its overlay, so reset is a clear-space gesture.)
    _rotCanvas.addEventListener('dblclick', function(e) {{
      if (!rotFitX || !rotFitY || !_rotChartEl) return;
      Plotly.relayout(_rotChartEl, {{ 'xaxis.range': rotFitX.slice(), 'yaxis.range': rotFitY.slice() }});
    }});
  }}
  // Thrust slider: hide the bottom % of themes by their up-and-right climb rank.
  (function() {{
    var sl = document.getElementById('rot-thrust-slider');
    if (!sl) return;
    sl.value = rotThrustMin;
    rotUpdateThrustReadout();
    sl.addEventListener('input', function() {{
      rotThrustMin = +sl.value;
      rotPersist();
      rotUpdateThrustReadout();
      rotDrawFrame(rotCurDi, rotCurFrac, rotCurK);
    }});
  }})();
  // Rotation gear → open the shared sector/theme filter panel (same one the
  // ticker page uses). stopPropagation so the document outside-click handler
  // doesn't immediately re-close it.
  (function() {{
    var fb = document.getElementById('rot-filter-btn');
    if (!fb) return;
    fb.addEventListener('click', function(e) {{
      e.stopPropagation();
      if (filterPanel && filterPanel.style.display === 'none') openFilterPanel();
      else closeFilterPanel();
    }});
  }})();

  // Overlay close: click the backdrop (outside the panel), the ✕, or Escape.
  var rotOverlayEl = document.getElementById('rot-overlay');
  if (rotOverlayEl) rotOverlayEl.addEventListener('click', function(e) {{ if (e.target === rotOverlayEl) rotCloseOverlay(); }});
  var rotOverlayCloseBtn = document.getElementById('rot-overlay-close');
  if (rotOverlayCloseBtn) rotOverlayCloseBtn.addEventListener('click', rotCloseOverlay);
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{
      var ov = document.getElementById('rot-overlay');
      if (ov && ov.style.display !== 'none') rotCloseOverlay();
    }}
  }});

  // ── Initial activate ───────────────────────────────────────
  applyFilter();
  applyHeaderIndicators();
  applyTickersHeaderIndicators();

  var hash = (window.location.hash || '').slice(1);
  var initialRowId = null;
  if (hash) {{
    var slash = hash.indexOf('/');
    if (slash >= 0) {{
      var part1 = hash.slice(0, slash);
      var part2 = hash.slice(slash + 1);
      if (part1 === 'tickers') {{
        // `#tickers/MRVL` — switch to Tickers view, focus that row.
        activeView = 'tickers';
        var flatId = 'tk__' + part2;
        var flatRow = tickersBody && tickersBody.querySelector('tr[data-row-id="' + flatId + '"]');
        if (flatRow) initialRowId = flatId;
      }} else {{
        var themeId = part1;
        var ticker  = part2;
        var childId = themeId + '__' + ticker;
        var childRow = tbody.querySelector('tr[data-row-id="' + childId + '"]');
        if (childRow) initialRowId = childId;
        var parent = tbody.querySelector('tr.theme-row[data-theme-id="' + themeId + '"]');
        if (parent) setExpanded(parent, true);
      }}
    }} else if (hash === 'tickers') {{
      activeView = 'tickers';
    }} else {{
      var themeRow = tbody.querySelector('tr.theme-row[data-theme-id="' + hash + '"]');
      if (themeRow) initialRowId = themeRow.dataset.rowId;
    }}
  }}
  // Apply the view (CSS classes + brand text) before picking the initial row.
  setView(activeView, {{preserveActive: true}});
  if (!initialRowId) {{
    var v0 = visibleRows();
    if (v0.length) initialRowId = v0[0].dataset.rowId;
  }}
  if (initialRowId) setActiveByRowId(initialRowId, {{skipHash: true}});

  // ── Refresh button (native --app mode only) ───────────────
  // Wires the "Generated" header cell to the QWebChannel bridge that
  // the Qt-window launcher registers. When loaded in a regular browser
  // (no qt.webChannelTransport), the cell stays clickable but the click
  // is a no-op with a tooltip explaining the requirement.
  var refreshCell    = document.getElementById('refresh-cell');
  var refreshToast   = null;
  var refreshInFlight = false;

  function showRefreshError(msg) {{
    if (!refreshToast) {{
      refreshToast = document.createElement('div');
      refreshToast.className = 'rm-refresh-toast';
      document.body.appendChild(refreshToast);
    }}
    refreshToast.textContent = 'Refresh failed: ' + msg;
    refreshToast.classList.add('is-visible');
    setTimeout(function() {{ refreshToast.classList.remove('is-visible'); }}, 6000);
  }}

  function triggerRefresh() {{
    if (refreshInFlight) return;
    if (!window.bridge || typeof window.bridge.refresh !== 'function') {{
      showRefreshError('only available in native window (run: python local_runner/theme_dashboard.py --app)');
      return;
    }}
    refreshInFlight = true;
    refreshCell.classList.add('is-refreshing');
    window.bridge.refresh();
  }}

  if (refreshCell) {{
    refreshCell.addEventListener('click', triggerRefresh);
    refreshCell.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); triggerRefresh(); }}
    }});
  }}

  // QWebChannel transport is injected only when the page is loaded by
  // PySide6's QWebEngineView with a registered channel. Set up the bridge
  // here so the refresh button can find it.
  if (typeof QWebChannel !== 'undefined' && typeof qt !== 'undefined' && qt.webChannelTransport) {{
    new QWebChannel(qt.webChannelTransport, function(channel) {{
      window.bridge = channel.objects.bridge;
      if (window.bridge && window.bridge.refreshFinished && window.bridge.refreshFinished.connect) {{
        window.bridge.refreshFinished.connect(function(ok, msg) {{
          refreshInFlight = false;
          refreshCell.classList.remove('is-refreshing');
          if (!ok) showRefreshError(msg || 'unknown error');
          // On success the Qt side calls view.reload() which re-runs this script,
          // so no explicit reload() needed here.
        }});
      }}
    }});
  }}

  // ── Candidates ────────────────────────────────────────────
  var candidatesInput  = document.getElementById('candidates-input');
  var candidatesAddBtn = document.getElementById('candidates-add-btn');
  var candidatesTbody  = document.getElementById('candidates-tbody');
  var candidatesCount  = document.getElementById('candidates-count');
  var candidatesActiveTicker = null;

  var candidatesList = [];
  try {{
    var _saved = window.localStorage && window.localStorage.getItem('themeDashboard.candidates');
    if (_saved) candidatesList = JSON.parse(_saved).filter(function(t) {{ return t && typeof t === 'string'; }});
  }} catch(e) {{}}

  function saveCandidates() {{
    try {{ window.localStorage && window.localStorage.setItem('themeDashboard.candidates', JSON.stringify(candidatesList)); }} catch(e) {{}}
  }}

  function themeChipsHtml(ticker) {{
    var themeIds = (window.FILTER_DATA && window.FILTER_DATA.themeIdsByTicker && window.FILTER_DATA.themeIdsByTicker[ticker]) || [];
    if (!themeIds.length) return '<span class="cand-unknown">—</span>';
    var rankOrder = window.THEME_RANK_ORDER || [];
    var labels    = window.THEME_LABELS    || {{}};
    var rs5map    = window.THEME_RS5       || {{}};
    var n = rankOrder.length;
    var chips = themeIds.map(function(tid) {{
      var rank = rankOrder.indexOf(tid);  // 0-based; -1 if ungrouped
      var label = labels[tid] || tid;
      var rs5   = rs5map[tid];
      var cls;
      if (rank < 0 || rs5 === undefined) {{
        cls = 'theme-chip-cold';
      }} else if (rank < Math.ceil(n * 0.33)) {{
        cls = 'theme-chip-hot';
      }} else if (rank < Math.ceil(n * 0.66)) {{
        cls = 'theme-chip-warm';
      }} else {{
        cls = 'theme-chip-cold';
      }}
      var rankStr = rank >= 0 ? (' #' + (rank + 1)) : '';
      return '<span class="theme-chip ' + cls + '" title="' + label + rankStr + '">' + label + rankStr + '</span>';
    }});
    return chips.join('');
  }}

  function renderCandidatesTable() {{
    if (!candidatesTbody) return;
    candidatesTbody.innerHTML = '';
    candidatesList.forEach(function(ticker, idx) {{
      var d = window.TICKER_DATA && window.TICKER_DATA[ticker];
      var tr = document.createElement('tr');
      if (ticker === candidatesActiveTicker) tr.classList.add('cand-active');
      if (d) {{
        var chgPct  = d.day_chg_pct != null ? d.day_chg_pct : null;
        var chgCls  = chgPct == null ? '' : (chgPct >= 0 ? 'cand-chg-pos' : 'cand-chg-neg');
        var chgStr  = chgPct == null ? '—' : (chgPct >= 0 ? '+' : '') + chgPct.toFixed(2) + '%';
        var lastStr = d.last_close != null ? '$' + d.last_close.toFixed(2) : '—';
        var name    = (d.long_name || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        var sector  = (d.sector    || '').replace(/</g,'&lt;');
        tr.innerHTML =
          '<td><button class="cand-remove" data-idx="' + idx + '" title="Remove">×</button></td>' +
          '<td><span class="cand-ticker">' + ticker + '</span></td>' +
          '<td><div class="cand-name" title="' + name + '">' + name + '</div></td>' +
          '<td>' + themeChipsHtml(ticker) + '</td>' +
          '<td><span class="cand-sector">' + sector + '</span></td>' +
          '<td style="text-align:right"><span class="cand-price">' + lastStr + '</span></td>' +
          '<td style="text-align:right"><span class="' + chgCls + '">' + chgStr + '</span></td>';
      }} else {{
        tr.innerHTML =
          '<td><button class="cand-remove" data-idx="' + idx + '" title="Remove">×</button></td>' +
          '<td><span class="cand-ticker">' + ticker + '</span></td>' +
          '<td colspan="5"><span class="cand-unknown">not in universe</span></td>';
      }}
      tr.addEventListener('click', function(e) {{
        if (e.target.classList.contains('cand-remove')) return;
        if (!d) return;
        candidatesActiveTicker = ticker;
        candidatesTbody.querySelectorAll('tr').forEach(function(r) {{ r.classList.remove('cand-active'); }});
        tr.classList.add('cand-active');
        showTickerSection(ticker);
      }});
      tr.querySelector('.cand-remove').addEventListener('click', function(e) {{
        e.stopPropagation();
        var i = parseInt(this.dataset.idx, 10);
        candidatesList.splice(i, 1);
        saveCandidates();
        if (candidatesActiveTicker === ticker) candidatesActiveTicker = null;
        renderCandidatesTable();
        updateCandidatesCount();
      }});
      candidatesTbody.appendChild(tr);
    }});
  }}

  function updateCandidatesCount() {{
    if (candidatesCount) candidatesCount.textContent = candidatesList.length + ' ticker' + (candidatesList.length === 1 ? '' : 's');
  }}

  function addCandidatesBulk(raw) {{
    // Split on any combination of newlines, commas, spaces, tabs
    var tickers = raw.split(/[\\s,]+/)
      .map(function(t) {{ return t.trim().toUpperCase().replace(/[^A-Z0-9.]/g, ''); }})
      .filter(function(t) {{ return t.length > 0 && t.length <= 6; }})
      .filter(function(t) {{ return candidatesList.indexOf(t) < 0; }});
    if (!tickers.length) return;
    tickers.forEach(function(t) {{ candidatesList.push(t); }});
    saveCandidates();
    renderCandidatesTable();
    updateCandidatesCount();
  }}

  if (candidatesAddBtn) {{
    candidatesAddBtn.addEventListener('click', function() {{
      if (candidatesInput) {{ addCandidatesBulk(candidatesInput.value); candidatesInput.value = ''; candidatesInput.focus(); }}
    }});
  }}
  if (candidatesInput) {{
    candidatesInput.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter') {{ e.preventDefault(); addCandidatesBulk(this.value); this.value = ''; }}
    }});
    candidatesInput.addEventListener('paste', function(e) {{
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData('text');
      addCandidatesBulk(text);
      this.value = '';
    }});
  }}

  renderCandidatesTable();
  updateCandidatesCount();

  // Paint hot-theme confluence counts from any persisted flags.
  applyHotCounts();

  // Resize handler: re-fit whatever Plotly chart is currently visible.
  window.addEventListener('resize', function() {{
    var row = currentRow();
    if (!row) return;
    if (row.dataset.rowKind === 'ticker') {{
      var pdiv = document.getElementById('ticker-chart');
      if (pdiv && window.Plotly && Plotly.Plots) {{
        try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}}
      }}
    }} else {{
      var sec = document.getElementById(row.dataset.themeId);
      var pdiv2 = sec && sec.querySelector('.plotly-graph-div');
      if (pdiv2 && window.Plotly && Plotly.Plots) {{
        try {{ Plotly.Plots.resize(pdiv2); }} catch(e) {{}}
      }}
    }}
  }});
}})();
</script>
</body>
</html>
"""
    return html, skipped


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

def _build_html_to_disk(theme=None, bars=250, skip_snapshot=False):
    """Run the full build pipeline (load cache, compute, write HTML).

    Pulled out of main() so the native-window mode can rebuild on demand
    without re-parsing argv or re-spawning a subprocess.

    Step 0 (always-on unless ``skip_snapshot``): rebuild the ext50-trendline
    snapshot. The Setups page reads this JSON; the morning scheduled run
    needs a fresh snapshot before the dashboard renders. Set
    ``skip_snapshot=True`` for fast interactive iterations during the day.
    """
    print("=" * 70)
    print("Hot Theme Dashboard")
    print("=" * 70)
    print(f"CACHE_DIR: {CACHE_DIR}")
    print(f"OUTPUT:    {OUTPUT_HTML}")
    print(f"BARS:      {bars}")

    if not skip_snapshot:
        print("\n" + "-" * 70)
        print("Step 0: ext50-trendline snapshot rebuild")
        print("-" * 70)
        try:
            from ext50_trendline_snapshot_builder import build as _build_snapshot
            _build_snapshot(verbose=True)
        except Exception as exc:
            print(f"  WARNING: ext50 snapshot rebuild failed: {exc!r}")
            print(f"  (dashboard will fall back to whatever snapshot file exists)")
        print("\n" + "-" * 70)
        print("Step 0b: First Flags snapshot rebuild")
        print("-" * 70)
        try:
            from first_flags_snapshot_builder import build as _build_first_flags
            _build_first_flags(verbose=True)
        except Exception as exc:
            print(f"  WARNING: First Flags snapshot rebuild failed: {exc!r}")
            print(f"  (dashboard will fall back to whatever snapshot file exists)")
        print("\n" + "-" * 70)
        print("Step 0c: Tightening Range snapshot rebuild")
        print("-" * 70)
        try:
            from tightening_range_snapshot_builder import build as _build_tighten
            _build_tighten(verbose=True)
        except Exception as exc:
            print(f"  WARNING: Tightening Range snapshot rebuild failed: {exc!r}")
            print(f"  (dashboard will fall back to whatever snapshot file exists)")

    cache, source_meta = load_daily_cache()
    if source_meta.get("source") == "intraday":
        print(f"Source: INTRADAY snapshot {source_meta.get('label', '')}")

    fundamentals = load_fundamentals()
    company_meta = load_company_meta()
    if company_meta:
        print(f"Loaded company_meta.json: {len(company_meta)} tickers with longName / longBusinessSummary")
    else:
        print("company_meta.json not present — mini-cards will render without longName / hover tooltip")
    validate_theme_sectors(THEMES, fundamentals, company_meta=company_meta)

    if theme:
        if theme not in THEMES:
            available = ", ".join(sorted(THEMES.keys()))
            raise SystemExit(f"Theme '{theme}' not in theme_map.py. Available: {available}")
        theme_keys = [theme]
    else:
        theme_keys = list(THEMES.keys())

    print(f"\nThemes to render: {len(theme_keys)}")
    n_unique = len({tk for k in theme_keys for tk in THEMES[k]})
    print(f"Unique tickers across themes: {n_unique}")

    html, skipped = build_dashboard(theme_keys, cache, bars,
                                    company_meta=company_meta,
                                    source_meta=source_meta,
                                    fundamentals=fundamentals)

    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(OUTPUT_HTML) / (1024 * 1024)
    print(f"\nWrote {OUTPUT_HTML} ({size_mb:.1f} MB)")
    if skipped:
        print(f"Skipped {len(skipped)} themes for insufficient data: {[s[0] for s in skipped]}")
    return OUTPUT_HTML


def launch_native_window(theme=None, bars=250, rebuild=True):
    """Open the dashboard in a native Qt window.

    Single in-process app: the "Generated" header button triggers a real
    intraday refresh via a QWebChannel JS↔Python bridge, no localhost / no
    Task Scheduler hop. The refresh runs on a background thread so the UI
    stays responsive; on completion the view reloads from disk against
    the freshly-rebuilt HTML.
    """
    import threading
    from PySide6.QtCore import QObject, QUrl, Signal, Slot, Qt
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import (
        QWebEngineSettings, QWebEngineProfile, QWebEnginePage,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    # Build once up front so the window has something to load immediately.
    if rebuild or not os.path.exists(OUTPUT_HTML):
        _build_html_to_disk(theme=theme, bars=bars)
    else:
        print(f"--no-rebuild: reusing existing {OUTPUT_HTML}")

    qt_app = QApplication.instance() or QApplication(sys.argv)

    class Bridge(QObject):
        # Signals fire on whichever thread emits them; QWebChannel marshals
        # them onto the GUI thread automatically for the JS side.
        refreshStarted  = Signal()
        refreshFinished = Signal(bool, str)

        @Slot()
        def refresh(self):
            """Kick off intraday_refresh.main() on a worker thread."""
            self.refreshStarted.emit()
            def worker():
                ok = True
                msg = ""
                try:
                    saved_argv = sys.argv
                    try:
                        sys.argv = ["intraday_refresh.py"]
                        import intraday_refresh
                        # Reload so any in-place edits get picked up.
                        import importlib
                        importlib.reload(intraday_refresh)
                        intraday_refresh.main()
                    finally:
                        sys.argv = saved_argv
                except SystemExit as exc:
                    code = getattr(exc, "code", 1)
                    if code not in (0, None):
                        ok = False
                        msg = f"intraday_refresh exited with code {code}"
                except Exception as exc:
                    ok = False
                    msg = f"{type(exc).__name__}: {exc}"
                self.refreshFinished.emit(ok, msg)
            threading.Thread(target=worker, daemon=True).start()

    bridge = Bridge()
    channel = QWebChannel()
    channel.registerObject("bridge", bridge)

    win = QMainWindow()
    win.setWindowTitle("ScanPerfect — Theme Dashboard")
    win.resize(1600, 1000)

    # Named persistent profile so localStorage (filter checkboxes, active
    # view, hide-below-200 state) survives app exit. QWebEngineProfile's
    # default profile is off-the-record and wipes localStorage on close;
    # giving the profile a name + persistent storage path keeps state
    # written between launches.
    profile_data_dir = os.path.join(CACHE_DIR, "qtwebengine_profile")
    os.makedirs(profile_data_dir, exist_ok=True)
    profile = QWebEngineProfile("scanperfect_dashboard", qt_app)
    profile.setPersistentStoragePath(profile_data_dir)
    profile.setCachePath(os.path.join(profile_data_dir, "cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.AllowPersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)

    view = QWebEngineView()
    page = QWebEnginePage(profile, view)
    view.setPage(page)

    # By default Qt WebEngine blocks file:// pages from loading remote
    # resources (Plotly CDN) and other file:// resources. Both are
    # required by our dashboard — composites + ticker charts pull Plotly
    # from cdn.plot.ly. Enabling these flags is safe because the only
    # page we load is our own dashboard.
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls,   True)
    settings.setAttribute(QWebEngineSettings.JavascriptEnabled,               True)
    settings.setAttribute(QWebEngineSettings.AllowRunningInsecureContent,     True)
    settings.setAttribute(QWebEngineSettings.LocalStorageEnabled,             True)

    view.page().setWebChannel(channel)
    view.load(QUrl.fromLocalFile(os.path.abspath(OUTPUT_HTML)))
    win.setCentralWidget(view)

    # When the refresh worker thread completes, reload the HTML from disk
    # so the freshly rendered file is what the user sees.
    def on_refresh_finished(ok, msg):
        if ok:
            view.reload()
        else:
            print(f"[refresh failed] {msg}")
    bridge.refreshFinished.connect(on_refresh_finished, Qt.QueuedConnection)

    win.show()
    sys.exit(qt_app.exec())


def main():
    ap = argparse.ArgumentParser(description="Build hot theme dashboard HTML.")
    ap.add_argument("--theme", default=None, help="Render only one theme by key (e.g., optics_photonics).")
    ap.add_argument("--bars", type=int, default=250, help="Bars to show in composite chart (default 250).")
    ap.add_argument("--open", action="store_true", help="Open the resulting HTML in your default browser.")
    ap.add_argument("--app",  action="store_true", help="Open the dashboard in a native PySide6 window with an in-process refresh button (no server, no Task Scheduler dependency).")
    ap.add_argument("--no-rebuild", action="store_true", help="With --app: skip the initial HTML rebuild and reuse the existing file on disk (faster relaunch).")
    ap.add_argument("--skip-snapshot", action="store_true", help="Skip the Step 0 snapshot rebuilds (ext50 trendlines + First Flags + Tightening Range). For fast interactive rebuilds when the morning snapshots are still fresh.")
    args = ap.parse_args()

    if args.app:
        launch_native_window(theme=args.theme, bars=args.bars, rebuild=not args.no_rebuild)
        return

    _build_html_to_disk(theme=args.theme, bars=args.bars, skip_snapshot=args.skip_snapshot)

    if args.open:
        webbrowser.open("file:///" + OUTPUT_HTML.replace("\\", "/"))


if __name__ == "__main__":
    main()
