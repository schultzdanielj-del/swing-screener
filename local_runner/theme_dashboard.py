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

    label = "(intraday 4:20pm)" if source == "intraday" else ""
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


def compute_extension_peeks(snapshot_doc, cache, universe):
    """Scan UNIVERSE for tickers peeking above a descending 50-SMA-extension
    trendline today.

    Peek rule (per locked sign convention signed_dist = proj - ext):
      - Yesterday's stored signed_dist >= 0 (price was at/below the line)
      - Today's live signed_dist  <  0 (price has crossed above)

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
}
.member-card svg { width: 100%; height: auto; display: block; }

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
  padding: 6px 10px;
  background: #060608;
  border-bottom: 1px solid var(--border-faint);
  font-family: var(--font-sans); font-size: 11px;
  color: var(--fg-secondary);
}
.watchlist-controls label { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; user-select: none; }
.watchlist-controls input[type="checkbox"] { accent-color: var(--accent); }
.watchlist-controls .wl-count { margin-left: auto; color: var(--fg-tertiary); font-family: var(--font-mono); }

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
.setups-meta {
  padding: 4px 10px; font-size: 11px; color: var(--fg-tertiary);
  font-family: var(--font-sans); border-bottom: 1px solid var(--border-faint);
}

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
    bench_rs_20  = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=20)
    bench_rs_65  = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=65)
    bench_rs_130 = tc2000_rs_raw(bench_o, bench_h, bench_l, bench_c, n_bars=130)
    bench_5d  = n_period_return(bench_c, 5)
    bench_adr = adr_pct(bench_h, bench_l, 20)
    for name in ("bench_rs_0d", "bench_rs_1", "bench_rs_3", "bench_rs_5", "bench_rs_20", "bench_rs_65", "bench_rs_130"):
        if locals().get(name) is None or locals().get(name) == 0:
            print(f"\nWARNING: Universe {name} could not be computed; using 1.0 as fallback.")
    bench_rs_0d  = bench_rs_0d  if (bench_rs_0d  is not None and bench_rs_0d  != 0) else 1.0
    bench_rs_1   = bench_rs_1   if (bench_rs_1   is not None and bench_rs_1   != 0) else 1.0
    bench_rs_3   = bench_rs_3   if (bench_rs_3   is not None and bench_rs_3   != 0) else 1.0
    bench_rs_5   = bench_rs_5   if (bench_rs_5   is not None and bench_rs_5   != 0) else 1.0
    bench_rs_20  = bench_rs_20  if (bench_rs_20  is not None and bench_rs_20  != 0) else 1.0
    bench_rs_65  = bench_rs_65  if (bench_rs_65  is not None and bench_rs_65  != 0) else 1.0
    bench_rs_130 = bench_rs_130 if (bench_rs_130 is not None and bench_rs_130 != 0) else 1.0
    print(f"Universe TC2000 RS  0D={bench_rs_0d:+.4f}  1d={bench_rs_1:+.4f}  3d={bench_rs_3:+.4f}  "
          f"5d={bench_rs_5:+.4f}  20d={bench_rs_20:+.4f}  65d={bench_rs_65:+.4f}  130d={bench_rs_130:+.4f}  "
          f"5d return={bench_5d:+.2f}%  ADR%={bench_adr:.2f}%")

    # ── First pass: build composites, compute 1d + 5d + 20d RS ratios ──
    print("\nBuilding composites and computing TC2000 RS ratios (theme / Universe) for 1d, 5d, 20d...")
    theme_pack = {}
    for tk_theme in theme_keys:
        members = THEMES[tk_theme]
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
        theme_rs_5   = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                      composite_df["low"].values,  composite_df["close"].values, n_bars=5)
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
        rs5_ratio   = (theme_rs_5   / abs(bench_rs_5))   if theme_rs_5   is not None else -1e9
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
            theme_rs_1=theme_rs_1, theme_rs_5=theme_rs_5, theme_rs_20=theme_rs_20,
            theme_rs_65=theme_rs_65, theme_rs_130=theme_rs_130,
            rs0d_ratio=rs0d_ratio,
            rs1_ratio=rs1_ratio, rs5_ratio=rs5_ratio, rs20_ratio=rs20_ratio,
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

        member_svgs = "".join(
            f'<div class="member-card">{build_mini_svg(cache[tk], tk, meta=company_meta.get(tk))}</div>'
            for tk in used
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
            f'<div class="member-card">{build_mini_svg(cache[tk], tk, meta=company_meta.get(tk))}</div>'
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
            f' data-adr="{adr_attr}" data-ext50="{ext50_attr}">'
            f'<td class="ticker-symbol-cell"><span class="ticker-symbol">{tk}</span></td>'
            f'<td class="theme-membership-cell" title="{theme_cell_text}">{theme_cell_text}</td>'
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
            f'<td class="ticker-symbol-cell"><span class="ticker-symbol">{tk}</span></td>'
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
        '<label title="Show only rows belonging to flagged themes"><input type="checkbox" id="toggle-flagged-only"/> Flagged</label>'
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
        f'<div class="setups-meta">Extension Peek — '
        f'{len(extension_peeks)} matches  ·  snapshot asof '
        f'{peek_asof_date}</div>'
        f'{setups_table_html}'
        '<div class="tickers-empty" id="setups-empty">No Extension Peek matches right now.</div>'
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

    embedded_data_script = (
        f'<script>window.TICKER_DATA = {ticker_data_json};'
        f'window.TICKER_LAYOUT = {ticker_layout_json};'
        f'window.FILTER_DATA = {filter_data_json};</script>'
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
    <div>
      <span class="rm-label">Sort</span>
      <span class="rm-h-sub">TC2000 RS PCF · theme/Universe · desc</span>
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
    </main>
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
  var tickersEmpty = document.getElementById('tickers-empty');
  var toggleTightOnly = document.getElementById('toggle-tight-only');
  var toggleNear50 = document.getElementById('toggle-near-50sma');
  var toggleFlaggedOnly = document.getElementById('toggle-flagged-only');
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
    var savedFlagged = window.localStorage && window.localStorage.getItem('themeDashboard.flaggedOnly');
    if (savedFlagged === '1' && toggleFlaggedOnly) toggleFlaggedOnly.checked = true;
  }} catch(e) {{}}

  // View state: 'themes' (default tree) or 'tickers' (flat ADR-tight list).
  // Persisted in localStorage so refresh keeps the active view.
  var activeView = 'themes';
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
    var setupsEmpty = document.getElementById('setups-empty');
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

  function renderTicker(ticker) {{
    var d = window.TICKER_DATA[ticker];
    var div = document.getElementById('ticker-chart');
    if (!d || !div || !window.Plotly) return;

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

  function showTickerSection(ticker) {{
    document.querySelectorAll('section.theme').forEach(function(s) {{ s.style.display = 'none'; }});
    if (tickerView) tickerView.style.display = '';
    buildTickerStrip(ticker);
    renderTicker(ticker);
  }}

  var setupsBody = document.getElementById('setups-watchlist-body');
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
  if (toggleFlaggedOnly) toggleFlaggedOnly.addEventListener('change', onHotTightChange);

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
    if (toggleFlaggedOnly && toggleFlaggedOnly.checked) applyFilter();
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
      if (toggleFlaggedOnly && toggleFlaggedOnly.checked) applyFilter();
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
             + '<span class="filter-item-sector">' + t.sector + '</span>'
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

  function applyFiltersToRows() {{
    // Themes pane: hide a theme row (and by extension its expanded
    // children) when its theme or dominant sector is excluded.
    rows().forEach(function(r) {{
      if (r.dataset.rowKind === 'theme') {{
        var ok = themeRowPassesFilter(r.dataset.themeId);
        r.classList.toggle('filtered-out', !ok);
      }} else if (r.dataset.rowKind === 'ticker') {{
        var ok2 = tickerRowPassesFilter(r.dataset.ticker);
        r.classList.toggle('filtered-out', !ok2);
      }}
    }});
    // Tickers pane: hide ticker-flat rows individually.
    tickerFlatRows().forEach(function(r) {{
      var ok = tickerRowPassesFilter(r.dataset.ticker);
      r.classList.toggle('filtered-out', !ok);
    }});
    // Setups pane: same per-ticker rule as the Tickers pane.
    setupsFlatRows().forEach(function(r) {{
      var ok = tickerRowPassesFilter(r.dataset.ticker);
      r.classList.toggle('filtered-out', !ok);
    }});
    // Reapply the below-200 filter so inline display is recomputed on top.
    applyFilter();
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

  // ── Setups view: sort + click-to-render ──────────────────────────
  var stSortKey  = 'peek';
  var stSortDir  = -1;   // -1 desc / 1 asc; "peek" sorts asc-by-default for tightest first → flip dir on init
  var stSortType = 'num';
  // For Setups the default is ASCENDING (tightest peek first). Other num
  // columns toggle ascending on first click as elsewhere; the |Peek| column
  // gets -1 here so the initial sort renders tightest-first.
  stSortDir = 1;

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
    // Header indicators
    document.querySelectorAll('#setups-watchlist th').forEach(function(th) {{
      th.classList.remove('sort-active', 'sort-asc');
    }});
    var active = document.querySelector('#setups-watchlist th[data-sort-key="' + stSortKey + '"]');
    if (active) {{
      active.classList.add('sort-active');
      if (stSortDir === 1) active.classList.add('sort-asc');
    }}
  }}
  document.querySelectorAll('#setups-watchlist th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var k = th.dataset.sortKey;
      var t = th.dataset.sortType;
      if (stSortKey === k) {{ stSortDir = -stSortDir; }} else {{ stSortKey = k; stSortType = t; stSortDir = -1; }}
      sortSetupsRows();
    }});
  }});
  if (setupsBody) {{
    setupsBody.addEventListener('click', function(e) {{
      var tr = e.target.closest('tr.setups-row');
      if (!tr) return;
      setActiveByRowId(tr.dataset.rowId);
    }});
    sortSetupsRows();  // initial sort: tightest |Peek| first (asc)
  }}

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
    if (name !== 'tickers' && name !== 'themes' && name !== 'setups') name = 'themes';
    activeView = name;
    document.body.classList.toggle('view-themes',  activeView === 'themes');
    document.body.classList.toggle('view-tickers', activeView === 'tickers');
    document.body.classList.toggle('view-setups',  activeView === 'setups');
    if (brandTitle) {{
      brandTitle.textContent = (activeView === 'tickers') ? 'HOT TICKERS DASHBOARD'
                              : (activeView === 'setups') ? 'SETUPS · EXTENSION PEEK'
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
    if (!opts || !opts.preserveActive) {{
      var v = visibleRows();
      if (v.length) setActiveByRowId(v[0].dataset.rowId);
    }}
  }}
  if (brandBtn) {{
    brandBtn.addEventListener('click', function() {{
      // Cycle: themes → tickers → setups → themes
      var next = (activeView === 'themes')   ? 'tickers'
               : (activeView === 'tickers')  ? 'setups'
               :                                'themes';
      setView(next);
    }});
  }}

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
            print(f"  WARNING: snapshot rebuild failed: {exc!r}")
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
    ap.add_argument("--skip-snapshot", action="store_true", help="Skip the ext50-trendline snapshot rebuild step (~2 min). For fast interactive rebuilds when the morning snapshot is still fresh.")
    args = ap.parse_args()

    if args.app:
        launch_native_window(theme=args.theme, bars=args.bars, rebuild=not args.no_rebuild)
        return

    _build_html_to_disk(theme=args.theme, bars=args.bars, skip_snapshot=args.skip_snapshot)

    if args.open:
        webbrowser.open("file:///" + OUTPUT_HTML.replace("\\", "/"))


if __name__ == "__main__":
    main()
