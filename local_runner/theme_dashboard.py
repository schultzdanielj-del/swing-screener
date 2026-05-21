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
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No OHLCV cache found in {CACHE_DIR}. Run cache_builder.py first.")
    print(f"Loading {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    n = len(cache)
    print(f"  {n} tickers in cache")
    if n < 11_200:
        raise RuntimeError(f"Cache has only {n} tickers (expected ~11,500). Aborting.")
    # SPY last-bar sanity check
    if "SPY" in cache:
        last_date = cache["SPY"]["date"].iloc[-1]
        print(f"  SPY last bar: {last_date}")
    return cache


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


def tc2000_rs_raw(open_arr, high_arr, low_arr, close_arr, n_bars=5):
    """Dan's TC2000 PCF relative strength, averaged over n_bars bars.

    PCF:
        avg = mean over last n_bars of (close/open - 1) * 100
        mult = ((close + close_50_bars_ago) / 2) / ATR50
        RS = avg * mult

    Returns None if insufficient data.
    """
    if open_arr is None or close_arr is None or high_arr is None or low_arr is None:
        return None
    n = len(close_arr)
    if n < max(51, n_bars):
        return None
    o = np.asarray(open_arr,  dtype=np.float64)
    h = np.asarray(high_arr,  dtype=np.float64)
    l = np.asarray(low_arr,   dtype=np.float64)
    c = np.asarray(close_arr, dtype=np.float64)

    # Average open-to-close % change over last n_bars bars
    o_win = o[-n_bars:]; c_win = c[-n_bars:]
    if np.any(np.isnan(o_win)) or np.any(np.isnan(c_win)) or np.any(o_win == 0):
        return None
    pct_changes = (c_win / o_win - 1.0) * 100.0
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

    # ── MACD divergence lines + markers ──────────────────
    # Lines are drawn at the MACD-line values (EMA6 − EMA20), not the histogram.
    # All divergences across the window are drawn; only the most recent one
    # per direction gets a text label to keep the chart readable.
    if divergences:
        for kind in ("bull", "bear"):
            div_list = divergences.get(kind, [])
            if not div_list:
                continue
            is_bull = (kind == "bull")
            col_line = COLOR_UP if is_bull else COLOR_DOWN
            for div_i, div in enumerate(div_list):
                is_most_recent = (div_i == len(div_list) - 1)
                # Older divergences drawn slightly translucent to declutter
                opacity = 1.0 if is_most_recent else 0.55
                d1  = pd.to_datetime(df["date"].iloc[div["p1_idx"]])
                d2  = pd.to_datetime(df["date"].iloc[div["p2_idx"]])
                dm1 = pd.to_datetime(df["date"].iloc[div["m1_idx"]])
                dm2 = pd.to_datetime(df["date"].iloc[div["m2_idx"]])
                # Price-pivot line on candle panel
                fig.add_trace(go.Scatter(
                    x=[d1, d2], y=[div["p1_price"], div["p2_price"]],
                    mode="lines+markers",
                    line=dict(color=col_line, width=1.5, dash="dot"),
                    marker=dict(color=col_line, size=7, symbol="circle"),
                    opacity=opacity,
                    showlegend=False, hoverinfo="skip", name="",
                ), row=1, col=1)
                # MACD-line pivot line on MACD panel
                fig.add_trace(go.Scatter(
                    x=[dm1, dm2], y=[div["m1_macd"], div["m2_macd"]],
                    mode="lines+markers",
                    line=dict(color=col_line, width=1.5, dash="dot"),
                    marker=dict(color=col_line, size=5, symbol="circle"),
                    opacity=opacity,
                    showlegend=False, hoverinfo="skip", name="",
                ), row=3, col=1)
                # Only label the most recent divergence per direction
                if is_most_recent:
                    label_text = "BULL DIV" if is_bull else "BEAR DIV"
                    fig.add_annotation(
                        x=d2, y=div["p2_price"],
                        xref="x", yref="y",
                        text=f"<b>{label_text}</b>",
                        showarrow=True, arrowhead=2, arrowcolor=col_line,
                        arrowsize=1, arrowwidth=1.2,
                        ax=20, ay=30 if is_bull else -30,
                        bgcolor="#000000", bordercolor=col_line, borderwidth=1,
                        font=dict(family="Segoe UI, sans-serif", size=10, color=col_line),
                    )

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
    df = df.tail(n_bars).reset_index(drop=True)
    if len(df) < 2:
        return f'<svg width="{width}" height="{height}" style="background:#000;display:block"></svg>'

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
        f'</linearGradient></defs>',
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
  grid-template-columns: 220px 1fr;
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

/* RS-vs-SPY cell in info bar */
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

/* High-interest: below 200-day AND bullish MACD divergence (Peoplewish divergence-pivot setup) */
.sidebar-link.sidebar-bull-under-200 {
  color: #1eff1e;
  border-left: 3px solid #1eff1e;
  background: rgba(30, 255, 30, 0.12);
  font-weight: 700;
}
.sidebar-link.sidebar-bull-under-200:hover {
  background: rgba(30, 255, 30, 0.22);
}
.sidebar-link.sidebar-bull-under-200.is-active {
  background: rgba(30, 255, 30, 0.32);
  color: #ffffff;
}

/* Divergence tags in the chart info bar */
.div-tag {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  padding: 2px 8px;
  margin-left: 6px;
  border: 1px solid currentColor;
}
.div-tag.div-bull { color: #1eff1e; background: rgba(30, 255, 30, 0.10); }
.div-tag.div-bear { color: #ff3030; background: rgba(255, 48, 48, 0.10); }

/* Divergence chip in sidebar */
.sidebar-div-chip {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 700;
  padding: 1px 4px;
  margin-left: 4px;
  letter-spacing: 0.05em;
  border: 1px solid currentColor;
}
.sidebar-div-chip.div-bull { color: #1eff1e; }
.sidebar-div-chip.div-bear { color: #ff3030; }
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
.body-grid { grid-template-columns: 320px 1fr; }   /* widen sidebar for the table */

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

@media (max-width: 900px) {
  .body-grid { grid-template-columns: 240px 1fr; }
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
    chg_cls = "up" if vals["chg"] >= 0 else "down"
    pct_str = f"{pct_200:+.1f}%" if pct_200 is not None else "—"
    rs_str = f"{rs_val:+.2f}" if rs_val is not None and rs_val > -1e8 else "—"
    t5d_str = f"{theme_5d:+.2f}%" if theme_5d is not None else "—"
    adr_str = f"{theme_adr:.2f}%" if theme_adr is not None else "—"
    rs_cls = "rs-up" if (rs_val is not None and rs_val >= 0) else "rs-down"
    div_tags = ""
    if divergences:
        bull_n = len(divergences.get("bull", []))
        bear_n = len(divergences.get("bear", []))
        if bull_n:
            suffix = f" ×{bull_n}" if bull_n > 1 else ""
            div_tags += f'<span class="div-tag div-bull">BULL DIV{suffix}</span>'
        if bear_n:
            suffix = f" ×{bear_n}" if bear_n > 1 else ""
            div_tags += f'<span class="div-tag div-bear">BEAR DIV{suffix}</span>'
    return (
        f'<div class="chart-info-bar">'
        f'<span class="cluster rs-cluster {rs_cls}">'
        f'<span class="lbl">TC2000 RS / SPY</span>'
        f'<span class="rs-val">{rs_str}x</span>'
        f'<span class="lbl">5d</span><span class="rs-theme">{t5d_str}</span>'
        f'<span class="lbl">ADR</span><span class="rs-theme">{adr_str}</span>'
        f'</span>'
        f'<span class="cluster pos200-cluster {pos_css}">'
        f'<span class="lbl">vs 200D</span>'
        f'<span class="pos200-pct">{pct_str}</span>'
        f'<span class="pos200-label">{pos_label}</span>'
        f'</span>'
        f'{div_tags}'
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


def build_dashboard(theme_keys, cache, n_bars, company_meta=None):
    sections_html = []
    sidebar_links = []
    skipped = []
    all_missing = []
    company_meta = company_meta or {}

    # ── Compute SPY benchmark using Dan's TC2000 RS PCF (1-bar, 5-bar, 20-bar) ──
    spy_rs_1 = None
    spy_rs_5 = None
    spy_rs_20 = None
    spy_5d = None
    spy_adr = None
    if "SPY" in cache:
        spy_df = cache["SPY"]
        spy_rs_1  = tc2000_rs_raw(spy_df["open"].values, spy_df["high"].values,
                                  spy_df["low"].values,  spy_df["close"].values, n_bars=1)
        spy_rs_5  = tc2000_rs_raw(spy_df["open"].values, spy_df["high"].values,
                                  spy_df["low"].values,  spy_df["close"].values, n_bars=5)
        spy_rs_20 = tc2000_rs_raw(spy_df["open"].values, spy_df["high"].values,
                                  spy_df["low"].values,  spy_df["close"].values, n_bars=20)
        spy_5d = n_period_return(spy_df["close"].values, 5)
        spy_adr = adr_pct(spy_df["high"].values, spy_df["low"].values, 20)
    if spy_rs_1 is None or spy_rs_1 == 0:
        print("\nWARNING: SPY 1-bar RS could not be computed; using 1.0 as fallback.")
        spy_rs_1 = 1.0
    if spy_rs_5 is None or spy_rs_5 == 0:
        print("WARNING: SPY 5-bar RS could not be computed; using 1.0 as fallback.")
        spy_rs_5 = 1.0
    if spy_rs_20 is None or spy_rs_20 == 0:
        print("WARNING: SPY 20-bar RS could not be computed; using 1.0 as fallback.")
        spy_rs_20 = 1.0
    print(f"\nSPY TC2000 RS  1d={spy_rs_1:+.4f}  5d={spy_rs_5:+.4f}  20d={spy_rs_20:+.4f}  "
          f"5d return={spy_5d:+.2f}%  ADR%={spy_adr:.2f}%")

    # ── First pass: build composites, compute 1d + 5d + 20d RS ratios ──
    print("\nBuilding composites and computing TC2000 RS ratios (theme / SPY) for 1d, 5d, 20d...")
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
        theme_rs_1  = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                    composite_df["low"].values,  composite_df["close"].values, n_bars=1)
        theme_rs_5  = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                    composite_df["low"].values,  composite_df["close"].values, n_bars=5)
        theme_rs_20 = tc2000_rs_raw(composite_df["open"].values, composite_df["high"].values,
                                    composite_df["low"].values,  composite_df["close"].values, n_bars=20)
        theme_5d  = n_period_return(composite_df["close"].values, 5)
        theme_adr = adr_pct(composite_df["high"].values, composite_df["low"].values, 20)
        rs1_ratio  = (theme_rs_1  / spy_rs_1)  if theme_rs_1  is not None else -1e9
        rs5_ratio  = (theme_rs_5  / spy_rs_5)  if theme_rs_5  is not None else -1e9
        rs20_ratio = (theme_rs_20 / spy_rs_20) if theme_rs_20 is not None else -1e9
        theme_pack[tk_theme] = dict(
            composite_df=composite_df, used=used,
            theme_5d=theme_5d, theme_adr=theme_adr,
            theme_rs_1=theme_rs_1, theme_rs_5=theme_rs_5, theme_rs_20=theme_rs_20,
            rs1_ratio=rs1_ratio, rs5_ratio=rs5_ratio, rs20_ratio=rs20_ratio,
        )

    # ── Initial sort: by 5d RS ratio desc (user can re-sort interactively) ──
    sorted_keys = sorted(theme_pack.keys(), key=lambda k: -theme_pack[k]["rs5_ratio"])

    # ── Second pass: emit HTML in sorted order ──
    print("\nEmitting themes sorted by 5-day RS vs SPY (descending)...")
    watchlist_rows = []  # collected for the sortable watchlist table

    for tk_theme in sorted_keys:
        pack = theme_pack[tk_theme]
        composite_df = pack["composite_df"]
        used = pack["used"]
        rs1_val  = pack["rs1_ratio"]
        rs5_val  = pack["rs5_ratio"]
        rs20_val = pack["rs20_ratio"]
        theme_5d = pack["theme_5d"]
        theme_adr = pack["theme_adr"]
        label = THEME_LABELS.get(tk_theme, tk_theme.replace("_", " ").title())

        divergences = detect_divergences(composite_df)
        narrative = THEME_NARRATIVES.get(tk_theme)
        fig, last_vals = build_composite_figure(composite_df, label, used, divergences, narrative=narrative)
        pct_200, pos_label, pos_css = position_vs_200d(composite_df)
        chart_div = fig.to_html(
            include_plotlyjs=False, full_html=False,
            div_id=f"chart_{tk_theme}",
            config={"displayModeBar": False, "scrollZoom": True, "doubleClick": "reset"},
        )

        member_svgs = "".join(
            f'<div class="member-card">{build_mini_svg(cache[tk], tk, meta=company_meta.get(tk))}</div>'
            for tk in used
        )

        has_bull = bool(divergences.get("bull"))
        has_bear = bool(divergences.get("bear"))
        is_below_200 = (pos_css == "pos-below")
        is_high_interest = is_below_200 and has_bull   # bullish divergence on under-200 theme

        sidebar_extra_cls = ""
        if is_high_interest:
            sidebar_extra_cls = " sidebar-bull-under-200"
        elif is_below_200:
            sidebar_extra_cls = " sidebar-below200"

        chip_text = f"{pct_200:+.1f}%" if pct_200 is not None else "—"
        sidebar_div_chip = ""
        if has_bull:
            sidebar_div_chip = '<span class="sidebar-div-chip div-bull">BULL</span>'
        if has_bear:
            sidebar_div_chip += '<span class="sidebar-div-chip div-bear">BEAR</span>'

        section_html = (
            f'<section class="theme" id="{tk_theme}">'
            f'{_ohlc_strip_html(last_vals, len(composite_df), pct_200, pos_label, pos_css, divergences, rs5_val, theme_5d, theme_adr)}'
            f'<div class="composite-chart">{chart_div}</div>'
            f'<div class="member-grid">{member_svgs}</div>'
            f'<div class="theme-foot"><span class="theme-foot-name">{label}</span>'
            f'<span class="theme-foot-meta">n={len(used)}</span></div>'
            f'</section>'
        )
        sections_html.append(section_html)
        # Collect data for the sortable watchlist table
        watchlist_rows.append(dict(
            theme_id=tk_theme,
            label=label,
            rs1=rs1_val if rs1_val is not None and rs1_val > -1e8 else None,
            rs5=rs5_val if rs5_val is not None and rs5_val > -1e8 else None,
            rs20=rs20_val if rs20_val is not None and rs20_val > -1e8 else None,
            pct_200=pct_200,
            count=len(used),
            below_200=is_below_200,
            has_bull=has_bull,
            has_bear=has_bear,
        ))

        pct_str  = f"{pct_200:+.1f}%" if pct_200 is not None else "n/a"
        rs1_str  = f"{rs1_val:+.2f}"  if rs1_val  is not None and rs1_val  > -1e8 else "n/a"
        rs5_str  = f"{rs5_val:+.2f}"  if rs5_val  is not None and rs5_val  > -1e8 else "n/a"
        rs20_str = f"{rs20_val:+.2f}" if rs20_val is not None and rs20_val > -1e8 else "n/a"
        t5d_str = f"{theme_5d:+.2f}%" if theme_5d is not None else "n/a"
        adr_str = f"{theme_adr:.2f}%" if theme_adr is not None else "n/a"
        div_str = ""
        if has_bull: div_str += " BULLDIV"
        if has_bear: div_str += " BEARDIV"
        print(f"  1d={rs1_str:>7}x  5d={rs5_str:>7}x  20d={rs20_str:>7}x  {tk_theme:35s} "
              f"5dret={t5d_str:>7}  ADR={adr_str:>6}  200D={pct_str:>7}{div_str}")

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

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_themes = len([s for s in sections_html if 'id="ungrouped"' not in s])
    n_universe = len(universe_dedup)
    n_in_themes = len([tk for tk in universe_dedup if tk in tickers_in_themes])
    n_ungrouped = len(ungrouped_in_cache) + len(ungrouped_missing)
    universe_summary = f"{n_universe} total · {n_in_themes} in themes · {n_ungrouped} ungrouped"
    spy_5d_str = f"{spy_5d:+.2f}%"
    spy_5d_cls = "up" if spy_5d >= 0 else "down"

    # SPY last date for the status bar
    spy_last = "?"
    if "SPY" in cache:
        try:
            spy_last = str(cache["SPY"]["date"].iloc[-1])[:10]
        except Exception:
            pass

    # ── Build the sortable watchlist HTML ──
    def _num_cell(val, fmt="{:+.2f}"):
        if val is None:
            return '<td class="num nul">—</td>'
        cls = "pos" if val >= 0 else "neg"
        return f'<td class="num {cls}">{fmt.format(val)}</td>'

    watchlist_body_rows = []
    for r in watchlist_rows:
        below_cls = " below-200" if r["below_200"] else ""
        rs1 = r["rs1"]; rs5 = r["rs5"]; rs20 = r["rs20"]
        rs1_attr  = f"{rs1:.4f}"  if rs1  is not None else "-1e9"
        rs5_attr  = f"{rs5:.4f}"  if rs5  is not None else "-1e9"
        rs20_attr = f"{rs20:.4f}" if rs20 is not None else "-1e9"
        watchlist_body_rows.append(
            f'<tr class="watchlist-row{below_cls}" data-theme-id="{r["theme_id"]}"'
            f' data-label="{r["label"]}" data-rs1="{rs1_attr}" data-rs5="{rs5_attr}" data-rs20="{rs20_attr}"'
            f' data-n="{r["count"]}">'
            f'<td class="theme-name">{r["label"]}</td>'
            f'{_num_cell(rs1)}'
            f'{_num_cell(rs5)}'
            f'{_num_cell(rs20)}'
            f'<td class="num count">{r["count"]}</td>'
            f'</tr>'
        )

    watchlist_html = (
        '<div class="watchlist-controls">'
        '<label><input type="checkbox" id="toggle-hide-below" checked/> Hide below 200D</label>'
        '<span class="wl-count" id="wl-visible-count"></span>'
        '</div>'
        '<table class="watchlist-table" id="watchlist">'
        '<thead><tr>'
        '<th data-sort-key="label" data-sort-type="text">Theme</th>'
        '<th data-sort-key="rs1"   data-sort-type="num">1d RS</th>'
        '<th data-sort-key="rs5"   data-sort-type="num" class="sort-active">5d RS</th>'
        '<th data-sort-key="rs20"  data-sort-type="num">20d RS</th>'
        '<th data-sort-key="n"     data-sort-type="num">N</th>'
        '</tr></thead>'
        f'<tbody id="watchlist-body">{"".join(watchlist_body_rows)}</tbody>'
        '</table>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Hot Theme Dashboard</title>
<style>{CSS}</style>
<script src="{PLOTLY_CDN}"></script>
</head>
<body>
<div class="app">
  <header class="rm-fn-bar">
    <div class="rm-fn-brand">
      <span class="rm-status-dot"></span>
      <span class="rm-fn-title">Hot Theme Dashboard</span>
    </div>
    <div>
      <span class="rm-label">Generated</span>
      <span class="rm-val">{now}</span>
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
      <span class="rm-val accent">{spy_last}</span>
    </div>
    <div>
      <span class="rm-label">SPY 5d  ·  ADR</span>
      <span class="rm-val {spy_5d_cls}">{spy_5d_str}  ·  {spy_adr:.2f}%</span>
    </div>
    <div>
      <span class="rm-label">Sort</span>
      <span class="rm-h-sub">TC2000 RS PCF · theme/SPY · desc</span>
    </div>
    <div>
      <span class="rm-label">Showing</span>
      <span class="rm-val accent" id="position-indicator">— / —</span>
    </div>
    <div>
      <span class="rm-label">Navigate</span>
      <span class="rm-h-sub mono">← → arrows · sidebar click</span>
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
    <span class="mono">← →  ↑↓  Home End  · ScanPerfect Theme Dashboard · {now}</span>
  </footer>
</div>
<script>
(function() {{
  var indicator    = document.getElementById('position-indicator');
  var footerCurrent= document.getElementById('footer-current');
  var visibleCount = document.getElementById('wl-visible-count');
  var tbody        = document.getElementById('watchlist-body');
  var sidebar      = document.querySelector('aside.sidebar');
  var toggleHide   = document.getElementById('toggle-hide-below');

  // Sort state
  var sortKey  = 'rs5';
  var sortDir  = -1;  // -1 desc, 1 asc
  var sortType = 'num';

  function rows() {{ return Array.prototype.slice.call(tbody.querySelectorAll('tr.watchlist-row')); }}
  function visibleRows() {{
    return rows().filter(function(r) {{
      var hidden = r.style.display === 'none' || r.classList.contains('hidden-by-filter');
      return !hidden;
    }});
  }}

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

  function sortRows() {{
    var all = rows();
    all.sort(function(a, b) {{
      var av, bv;
      if (sortType === 'num') {{
        av = parseFloat(a.dataset[sortKey === 'pct' ? 'pct' : sortKey === 'n' ? 'n' : sortKey] || '-1e9');
        bv = parseFloat(b.dataset[sortKey === 'pct' ? 'pct' : sortKey === 'n' ? 'n' : sortKey] || '-1e9');
        if (isNaN(av)) av = -1e9;
        if (isNaN(bv)) bv = -1e9;
        return (av - bv) * sortDir;
      }} else if (sortKey === 'div') {{
        // bull > both > bear > none, just text-compare to keep stable
        function rank(r) {{
          var hasBull = r.querySelector('.div-cell .bull') ? 1 : 0;
          var hasBear = r.querySelector('.div-cell .bear') ? 1 : 0;
          return hasBull * 2 + hasBear;
        }}
        return (rank(a) - rank(b)) * sortDir;
      }} else {{
        av = (a.dataset.label || '').toLowerCase();
        bv = (b.dataset.label || '').toLowerCase();
        return av.localeCompare(bv) * sortDir;
      }}
    }});
    all.forEach(function(r) {{ tbody.appendChild(r); }});
    applyHeaderIndicators();
  }}

  function applyFilter() {{
    var hideBelow = toggleHide && toggleHide.checked;
    rows().forEach(function(r) {{
      var below = r.classList.contains('below-200');
      r.style.display = (hideBelow && below) ? 'none' : '';
    }});
    if (visibleCount) visibleCount.textContent = visibleRows().length + ' visible';
  }}

  // ── Section nav driven by the watchlist row order ──
  var activeThemeId = null;

  function setActiveByThemeId(themeId, opts) {{
    if (!themeId) return;
    var sec = document.getElementById(themeId);
    if (!sec) return;
    document.querySelectorAll('section.theme').forEach(function(s) {{ s.classList.remove('is-active'); }});
    sec.classList.add('is-active');
    rows().forEach(function(r) {{ r.classList.toggle('is-active', r.dataset.themeId === themeId); }});
    activeThemeId = themeId;
    var pdiv = sec.querySelector('.plotly-graph-div');
    if (pdiv && window.Plotly && Plotly.Plots && Plotly.Plots.resize) {{
      try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}}
    }}
    // Scroll selected row into view
    var activeRow = tbody.querySelector('tr.is-active');
    if (activeRow && sidebar) {{
      var rTop = activeRow.offsetTop, rBot = rTop + activeRow.offsetHeight;
      if (rTop < sidebar.scrollTop || rBot > sidebar.scrollTop + sidebar.clientHeight) {{
        activeRow.scrollIntoView({{block: 'nearest'}});
      }}
    }}
    var visible = visibleRows();
    var idx = -1;
    for (var i = 0; i < visible.length; i++) {{
      if (visible[i].dataset.themeId === themeId) {{ idx = i; break; }}
    }}
    if (indicator) indicator.textContent = (idx >= 0 ? (idx+1) : '—') + ' / ' + visible.length;
    if (footerCurrent) {{
      footerCurrent.textContent = activeRow ? activeRow.dataset.label : '';
    }}
    window.scrollTo(0, 0);
    if (!opts || !opts.skipHash) {{
      try {{ history.replaceState(null, '', '#' + themeId); }} catch(e) {{}}
    }}
  }}

  function moveActive(delta) {{
    var visible = visibleRows();
    if (!visible.length) return;
    var idx = -1;
    for (var i = 0; i < visible.length; i++) {{
      if (visible[i].dataset.themeId === activeThemeId) {{ idx = i; break; }}
    }}
    var next = (idx < 0 ? 0 : idx + delta);
    if (next < 0) next = visible.length - 1;
    if (next >= visible.length) next = 0;
    setActiveByThemeId(visible[next].dataset.themeId);
  }}

  // Header click → sort
  document.querySelectorAll('#watchlist th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var k = th.dataset.sortKey;
      var t = th.dataset.sortType;
      if (sortKey === k) {{ sortDir = -sortDir; }} else {{ sortKey = k; sortType = t; sortDir = -1; }}
      sortRows();
    }});
  }});

  // Row click → activate
  tbody.addEventListener('click', function(e) {{
    var tr = e.target.closest('tr.watchlist-row');
    if (!tr) return;
    setActiveByThemeId(tr.dataset.themeId);
  }});

  // Toggle hide-below-200
  if (toggleHide) toggleHide.addEventListener('change', function() {{
    applyFilter();
    // If active row got hidden, jump to first visible
    if (activeThemeId) {{
      var activeRow = tbody.querySelector('tr.is-active');
      if (activeRow && activeRow.style.display === 'none') {{
        var visible = visibleRows();
        if (visible.length) setActiveByThemeId(visible[0].dataset.themeId);
      }}
    }}
  }});

  // Keyboard navigation
  document.addEventListener('keydown', function(e) {{
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    if (k === 'ArrowRight' || k === 'ArrowDown' || k === 'j' || k === 'J' || k === ' ' || k === 'PageDown') {{
      e.preventDefault(); moveActive(+1);
    }} else if (k === 'ArrowLeft' || k === 'ArrowUp' || k === 'k' || k === 'K' || k === 'PageUp') {{
      e.preventDefault(); moveActive(-1);
    }} else if (k === 'Home') {{
      e.preventDefault();
      var v = visibleRows();
      if (v.length) setActiveByThemeId(v[0].dataset.themeId);
    }} else if (k === 'End') {{
      e.preventDefault();
      var v = visibleRows();
      if (v.length) setActiveByThemeId(v[v.length-1].dataset.themeId);
    }}
  }});

  // Initial activate
  applyFilter();
  applyHeaderIndicators();
  var hash = (window.location.hash || '').slice(1);
  var initialId = null;
  if (hash) {{
    var hashRow = tbody.querySelector('tr[data-theme-id="' + hash + '"]');
    if (hashRow) initialId = hashRow.dataset.themeId;
  }}
  if (!initialId) {{
    var v = visibleRows();
    if (v.length) initialId = v[0].dataset.themeId;
  }}
  if (initialId) setActiveByThemeId(initialId, {{skipHash: true}});

  window.addEventListener('resize', function() {{
    var sec = activeThemeId ? document.getElementById(activeThemeId) : null;
    if (sec) {{
      var pdiv = sec.querySelector('.plotly-graph-div');
      if (pdiv && window.Plotly && Plotly.Plots) {{
        try {{ Plotly.Plots.resize(pdiv); }} catch(e) {{}}
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

def main():
    ap = argparse.ArgumentParser(description="Build hot theme dashboard HTML.")
    ap.add_argument("--theme", default=None, help="Render only one theme by key (e.g., optics_photonics).")
    ap.add_argument("--bars", type=int, default=250, help="Bars to show in composite chart (default 250).")
    ap.add_argument("--open", action="store_true", help="Open the resulting HTML in your default browser.")
    args = ap.parse_args()

    print("=" * 70)
    print("Hot Theme Dashboard")
    print("=" * 70)
    print(f"CACHE_DIR: {CACHE_DIR}")
    print(f"OUTPUT:    {OUTPUT_HTML}")
    print(f"BARS:      {args.bars}")

    cache = load_daily_cache()

    # Cross-check theme assignments against fundamentals sectors
    fundamentals = load_fundamentals()
    company_meta = load_company_meta()
    if company_meta:
        print(f"Loaded company_meta.json: {len(company_meta)} tickers with longName / longBusinessSummary")
    else:
        print("company_meta.json not present — mini-cards will render without longName / hover tooltip")
    validate_theme_sectors(THEMES, fundamentals, company_meta=company_meta)

    if args.theme:
        if args.theme not in THEMES:
            available = ", ".join(sorted(THEMES.keys()))
            raise SystemExit(f"Theme '{args.theme}' not in theme_map.py. Available: {available}")
        theme_keys = [args.theme]
    else:
        theme_keys = list(THEMES.keys())

    print(f"\nThemes to render: {len(theme_keys)}")
    n_unique = len({tk for k in theme_keys for tk in THEMES[k]})
    print(f"Unique tickers across themes: {n_unique}")

    html, skipped = build_dashboard(theme_keys, cache, args.bars, company_meta=company_meta)

    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(OUTPUT_HTML) / (1024 * 1024)
    print(f"\nWrote {OUTPUT_HTML} ({size_mb:.1f} MB)")
    if skipped:
        print(f"Skipped {len(skipped)} themes for insufficient data: {[s[0] for s in skipped]}")

    if args.open:
        webbrowser.open("file:///" + OUTPUT_HTML.replace("\\", "/"))


if __name__ == "__main__":
    main()
