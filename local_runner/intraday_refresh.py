"""Live intraday-snapshot refresh for the Theme Dashboard.

Pulls **real-time** quotes from Yahoo's batched `/v7/finance/quote` endpoint
for the theme-dashboard universe only (~THEMES ∪ UNIVERSE from theme_map.py,
hundreds of tickers — NOT the full 11k OHLCV cache), writes today's live bar
into an in-memory copy of `universe_ohlcv_daily.pkl`, saves it atomically to
`universe_ohlcv_daily_intraday.pkl`, and regenerates `theme_dashboard.html`
against the new pickle.

Why Yahoo, not EODHD: EODHD's REST real-time is 15–20 min delayed on the
All-World plan (confirmed in their docs). Yahoo's quote endpoint is real-time
for US equities (`exchangeDataDelayedBy = 0`), free, and batches ~100 symbols
per call, so the whole theme universe refreshes in ~10 calls / a few seconds.
EODHD is still the source for prior-day EOD history (its bulk endpoint); Yahoo
only supplies today's live bar.

Reconciliation: next morning's `nightly.py` overwrites the main pickle with the
official EOD close. From then on the main pickle's mtime is newer than the
intraday pickle's and the dashboard ignores the intraday file. Source of truth
always remains nightly.

Readiness gate: a today bar is only written for a ticker whose quote's
`regularMarketTime` resolves to today's ET date. Before the open (pre-market),
that timestamp is still yesterday for everyone, so if fewer than
MIN_TODAY_FRACTION of the universe has today-session data the run treats it as
"market not open / feed not ready" and writes nothing — which is also what
prevents a pre-market run from stamping yesterday's prices onto today.

Out of scope (do NOT add here): full 11k universe intraday, bullish/bearish
commentary, trade signals.

Run manually:
    python local_runner/intraday_refresh.py
Scheduled task XML at local_runner/cache/intraday_refresh_task.xml.
"""

import http.cookiejar
import json
import os
import pickle
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, LOCAL_DIR)

from theme_map import THEMES, UNIVERSE  # noqa: E402

# Benchmarks the theme dashboard reads directly even though they aren't theme
# members. SPY drives the header "Cache Last Bar" date and every theme/SPY
# TC2000 RS ratio; if it isn't refreshed, those comparisons cross a one-day
# mismatch and the header reports yesterday's date with the intraday marker
# attached. Keep this list narrow — these are the tickers the dashboard
# itself looks up by name.
BENCHMARK_TICKERS = ["SPY"]

# ──────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

BATCH_SIZE = 100          # symbols per /v7/finance/quote call (Yahoo allows ~200)
HTTP_TIMEOUT = 30
MAX_BATCH_RETRIES = 3
# Rate-limit headroom: the whole universe is only ~10 batched calls, so instead
# of firing them back-to-back we space them out by REFRESH_BUDGET_SEC/n_batches.
# With ~10 batches that's ~4.5s between calls; add the ~14s of request + crumb
# overhead and a full refresh lands ~55s — under the ~60s ceiling Dan OK'd, and
# cheap insurance against Yahoo throttling.
REFRESH_BUDGET_SEC = 45
MIN_TODAY_FRACTION = 0.60  # below this share with today-session data → treat as market-closed/not-ready

MAIN_PICKLE = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
INTRADAY_PICKLE = os.path.join(CACHE_DIR, "universe_ohlcv_daily_intraday.pkl")
INTRADAY_MARKER_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_daily_intraday.meta")

ET = ZoneInfo("America/New_York")


def _log(msg):
    print(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────
# UNIVERSE
# ──────────────────────────────────────────────────────────────────

def build_theme_universe():
    """Return sorted list of unique tickers covered by the theme dashboard.

    = tickers in any THEMES list ∪ tickers in UNIVERSE ∪ BENCHMARK_TICKERS.
    """
    tickers = set(UNIVERSE)
    for members in THEMES.values():
        tickers.update(members)
    tickers.update(BENCHMARK_TICKERS)
    return sorted(tickers)


# ──────────────────────────────────────────────────────────────────
# YAHOO /v7/finance/quote FETCH  (real-time, batched)
# ──────────────────────────────────────────────────────────────────

def _yahoo_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # Only a User-Agent — the crumb endpoint returns plain text and 406s if we
    # demand Accept: application/json. The quote endpoint returns JSON regardless.
    op.addheaders = [("User-Agent", USER_AGENT)]
    return op


def _yahoo_crumb(op):
    """Yahoo's quote endpoint requires a per-session crumb. Prime the cookie
    jar against fc.yahoo.com (may 404 — that's fine, it still sets the cookie),
    then fetch the crumb token. Returns the crumb string, or "" on failure.
    """
    try:
        op.open(YAHOO_COOKIE_URL, timeout=HTTP_TIMEOUT)
    except Exception:
        pass  # 404 is expected; we only need the Set-Cookie side effect
    try:
        return op.open(YAHOO_CRUMB_URL, timeout=HTTP_TIMEOUT).read().decode("utf-8").strip()
    except Exception as e:
        _log(f"  crumb fetch failed: {e!r}")
        return ""


def fetch_yahoo_quotes(tickers):
    """Fetch real-time quotes for all tickers via Yahoo's batched quote
    endpoint. Returns {ticker: quote_dict} keyed by the symbol we sent
    (which is the OHLCV cache key — already Yahoo's dash format).
    """
    op = _yahoo_opener()
    crumb = _yahoo_crumb(op)
    if not crumb:
        _log("FATAL: could not obtain a Yahoo crumb. Cannot fetch quotes.")
        return {}

    quotes = {}
    n = len(tickers)
    n_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE
    # Spread the ~10 calls across the budget so we're gentle on Yahoo.
    inter_sleep = max(0.5, REFRESH_BUDGET_SEC / max(1, n_batches))
    _log(f"Fetching {n} tickers from Yahoo in {n_batches} batches of {BATCH_SIZE} "
         f"(~{inter_sleep:.1f}s between calls)…")

    for i in range(0, n, BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]

        def _build_url(cr):
            return (f"{YAHOO_QUOTE_URL}?symbols={urllib.parse.quote(','.join(batch))}"
                    f"&crumb={urllib.parse.quote(cr)}")

        url = _build_url(crumb)
        last_err = None
        for attempt in range(MAX_BATCH_RETRIES):
            try:
                with op.open(url, timeout=HTTP_TIMEOUT) as resp:
                    js = json.loads(resp.read().decode("utf-8"))
                for q in js.get("quoteResponse", {}).get("result", []):
                    sym = q.get("symbol", "")
                    if sym:
                        quotes[sym] = q
                last_err = None
                break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code == 401:  # crumb expired → refresh once and retry
                    crumb = _yahoo_crumb(op)
                    url = _build_url(crumb)
                    time.sleep(1.0)
                    continue
                if e.code == 429:  # throttled → back off hard before retrying
                    time.sleep(min(15.0, 5.0 * (attempt + 1)))
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break  # other 4xx won't fix with a retry
            except Exception as e:
                last_err = repr(e)
                time.sleep(1.0 * (attempt + 1))
        if last_err:
            _log(f"  batch {i // BATCH_SIZE + 1} failed [{batch[0]}…{batch[-1]}]: {last_err}")
        # Pace between batches (skip the wait after the final batch).
        if i + BATCH_SIZE < n:
            time.sleep(inter_sleep)

    _log(f"Received {len(quotes)} / {n} quotes ({len(quotes) / max(1, n) * 100:.1f}%)")
    return quotes


def _yq_today_bar(q, today_ts):
    """Return (open, high, low, close, volume) from a Yahoo quote IF it carries
    valid regular-session data for TODAY, else None.

    Yahoo is real-time (exchangeDataDelayedBy = 0), so there is no stale-echo
    window to guard against — we only confirm the quote belongs to today's
    session (its last regular trade is today, not a pre-market carryover of
    yesterday's close) and that the price is a finite positive number.
    """
    t = q.get("regularMarketTime")
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    if t != t or t <= 0:  # NaN / non-positive
        return None
    if pd.Timestamp(datetime.fromtimestamp(t, ET).date()) != today_ts:
        return None  # last regular trade was a prior session

    def num(key, default=None):
        v = q.get(key)
        try:
            v = float(v)
            return default if v != v else v
        except (TypeError, ValueError):
            return default

    close_v = num("regularMarketPrice")
    if close_v is None or close_v <= 0:
        return None
    open_v = num("regularMarketOpen", close_v)
    high_v = num("regularMarketDayHigh", close_v)
    low_v = num("regularMarketDayLow", close_v)
    vol_v = num("regularMarketVolume", 0.0)
    return open_v, high_v, low_v, close_v, vol_v


# ──────────────────────────────────────────────────────────────────
# SUBSTITUTION
# ──────────────────────────────────────────────────────────────────

def substitute_last_bars(cache, quotes, today_et):
    """Mutate cache in place: write today's live bar for every ticker whose
    Yahoo quote carries valid today-session data.

    cache: dict {ticker: DataFrame[date, open, high, low, close, volume, ...]}
    quotes: dict {ticker: Yahoo quote dict}
    today_et: pandas.Timestamp at ET date (no time).

    Returns (n_updated, n_appended, n_skipped_missing_cache, n_no_today_data).
    """
    n_updated = 0
    n_appended = 0
    n_skipped = 0
    n_no_today = 0

    today_ts = pd.Timestamp(today_et).normalize()

    for ticker, q in quotes.items():
        if ticker not in cache:
            n_skipped += 1
            continue
        df = cache[ticker]
        if df is None or df.empty:
            n_skipped += 1
            continue

        bar = _yq_today_bar(q, today_ts)
        if bar is None:
            n_no_today += 1  # no real today-session trade yet (rare during hours)
            continue
        open_v, high_v, low_v, close_v, vol_v = bar

        last_date = pd.Timestamp(df["date"].iloc[-1]).normalize()
        if last_date == today_ts:
            # Today's bar already present (e.g. a prior refresh this session) → update it.
            idx = df.index[-1]
            df.at[idx, "open"] = open_v
            df.at[idx, "high"] = high_v
            df.at[idx, "low"] = low_v
            df.at[idx, "close"] = close_v
            df.at[idx, "volume"] = vol_v
            n_updated += 1
        elif last_date < today_ts:
            new_row = {col: pd.NA for col in df.columns}
            new_row["date"] = today_ts
            new_row["open"] = open_v
            new_row["high"] = high_v
            new_row["low"] = low_v
            new_row["close"] = close_v
            new_row["volume"] = vol_v
            cache[ticker] = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            n_appended += 1
        else:
            # Cache already newer than today (shouldn't happen) — never rewrite history.
            n_skipped += 1

    return n_updated, n_appended, n_skipped, n_no_today


# ──────────────────────────────────────────────────────────────────
# ATOMIC PICKLE WRITE
# ──────────────────────────────────────────────────────────────────

def atomic_write_pickle(obj, target_path):
    """Write pickle to target_path atomically (tmp file + rename)."""
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_intraday_", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, target_path)  # atomic on Windows too
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_marker(today_et, n_updated, n_appended, quote_count):
    """Write a tiny JSON marker alongside the intraday pickle so downstream
    consumers (and humans) can see exactly what snapshot is in the pickle.
    """
    now_et = datetime.now(ET)
    clock = now_et.strftime("%I:%M%p").lstrip("0").lower()  # e.g. "9:44am" / "4:20pm"
    meta = {
        "snapshot_et": f"{today_et.strftime('%Y-%m-%d')} {now_et.strftime('%H:%M:%S')} ET",
        "written_at": now_et.isoformat(),
        "source": "yahoo-realtime",
        "quotes_received": quote_count,
        "bars_updated": n_updated,
        "bars_appended": n_appended,
        "label": f"intraday {clock}",
    }
    with open(INTRADAY_MARKER_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ──────────────────────────────────────────────────────────────────
# DASHBOARD REGEN
# ──────────────────────────────────────────────────────────────────

def regenerate_dashboard():
    """Invoke theme_dashboard.main() with default args (bars=250, no --open)."""
    _log("Regenerating theme_dashboard.html against intraday pickle…")
    saved_argv = sys.argv
    try:
        # --skip-snapshot: intraday refresh is the same trading day, so the
        # morning ext50/first-flags/tightening snapshots are still valid.
        sys.argv = ["theme_dashboard.py", "--bars", "250", "--skip-snapshot"]
        import theme_dashboard
        theme_dashboard.main()
    finally:
        sys.argv = saved_argv


# ──────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    _log("=" * 70)
    _log("ScanPerfect Intraday Refresh (Yahoo real-time)")
    _log("=" * 70)

    if not os.path.exists(MAIN_PICKLE):
        _log(f"FATAL: main pickle missing at {MAIN_PICKLE}.")
        return 2

    universe = build_theme_universe()
    _log(f"Theme universe: {len(universe)} unique tickers")

    _log(f"Loading main pickle: {MAIN_PICKLE}")
    with open(MAIN_PICKLE, "rb") as f:
        cache = pickle.load(f)
    _log(f"  loaded {len(cache)} tickers from main pickle")
    if len(cache) < 11_200:
        _log(f"FATAL: main pickle has only {len(cache)} tickers (expected ~11,500). Aborting.")
        return 2

    universe_in_cache = [t for t in universe if t in cache]
    missing_from_cache = [t for t in universe if t not in cache]
    if missing_from_cache:
        _log(f"  {len(missing_from_cache)} theme tickers missing from cache (skipped): "
             f"{missing_from_cache[:10]}{'...' if len(missing_from_cache) > 10 else ''}")
    _log(f"  {len(universe_in_cache)} theme tickers will be quoted")

    # Fetch live quotes.
    t0 = time.time()
    quotes = fetch_yahoo_quotes(universe_in_cache)
    _log(f"Fetch complete in {time.time() - t0:.1f}s.")

    # Readiness gate: how many have a real today-session bar?
    today_et = pd.Timestamp(datetime.now(ET).date())
    today_ts = today_et.normalize()
    n_today = sum(1 for t, q in quotes.items()
                  if t in cache and _yq_today_bar(q, today_ts) is not None)
    frac = n_today / max(1, len(universe_in_cache))
    _log(f"Tickers with today-session data: {n_today} / {len(universe_in_cache)} ({frac * 100:.1f}%)")

    if frac < MIN_TODAY_FRACTION:
        _log(f"Below {MIN_TODAY_FRACTION * 100:.0f}% — market likely not open / feed not ready. "
             f"Intraday pickle untouched.")
        return 4

    # Substitute today's live bar.
    _log(f"Writing today's bars (today = {today_et.date()} ET)…")
    n_upd, n_app, n_skip, n_no_today = substitute_last_bars(cache, quotes, today_et)
    _log(f"  updated: {n_upd}  appended: {n_app}  skipped (not in cache): {n_skip}  "
         f"no-today-data: {n_no_today}")

    if n_upd + n_app == 0:
        _log("No bars written. Intraday pickle untouched.")
        return 4

    # Atomic write.
    _log(f"Writing intraday pickle: {INTRADAY_PICKLE}")
    t0 = time.time()
    atomic_write_pickle(cache, INTRADAY_PICKLE)
    write_marker(today_et, n_upd, n_app, n_today)
    size_mb = os.path.getsize(INTRADAY_PICKLE) / (1024 * 1024)
    _log(f"  wrote {size_mb:.1f} MB in {time.time() - t0:.1f}s")

    # Regen the dashboard against the freshly written intraday pickle.
    try:
        regenerate_dashboard()
    except Exception as e:
        _log(f"WARNING: dashboard regen failed: {e!r}")
        return 5

    _log("=" * 70)
    _log("Intraday refresh complete.")
    _log("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
