"""Intraday close-snapshot refresh for the Theme Dashboard.

Fires daily at 4:20 PM ET (20 min after the regular-session close). Pulls
EODHD `/real-time/` quotes for the theme-dashboard universe only
(~THEMES ∪ UNIVERSE from theme_map.py, hundreds of tickers — NOT the
full 11k OHLCV cache), substitutes those quotes into the last daily bar
of an in-memory copy of `universe_ohlcv_daily.pkl`, writes the modified
cache atomically to `universe_ohlcv_daily_intraday.pkl`, and regenerates
`theme_dashboard.html` against the new pickle.

Reconciliation: next morning's `nightly.py` overwrites the main pickle
with the official EODHD MOC close. From then on the main pickle's
mtime is newer than the intraday pickle's and the dashboard ignores
the intraday file. Source of truth always remains nightly.

Failure modes:
  - EODHD unreachable / 5xx / token rejected: log + exit non-zero,
    intraday pickle untouched. Dashboard continues reading the prior
    intraday pickle (if still newer than main) or the main pickle.
  - Partial batch failure: if < 70% of theme-universe tickers got
    valid quotes, treat as outage and exit without writing.
  - Quote stale (e.g. weekend / holiday / market closed): if every
    valid quote's close equals the main pickle's last-bar close for
    that ticker, log "no movement detected" and exit without writing.

Out of scope (do NOT add here): full 11k universe intraday, multiple
runs per day, bullish/bearish commentary on the snapshot, trade signals.

Run manually:
    python local_runner/intraday_refresh.py
Scheduled task XML at local_runner/cache/intraday_refresh_task.xml.
"""

import json
import os
import pickle
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
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
EODHD_API_TOKEN = os.environ.get("EODHD_API_TOKEN", "")
EODHD_BASE = "https://eodhd.com/api"
BATCH_SIZE = 18  # tickers per /real-time/ call (spec window 15-20)
HTTP_TIMEOUT = 30
INTER_BATCH_SLEEP = 0.10  # 18*60 = 1080 → ~600/min, safely under 1000/min limit
MAX_BATCH_RETRIES = 3
MIN_SUCCESS_FRACTION = 0.70  # below this → treat as outage

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
    The benchmark tickers (SPY today) are included even when they aren't
    theme members because the dashboard reads them directly for the header
    "Cache Last Bar" date and the theme/SPY TC2000 RS ratios.
    """
    tickers = set(UNIVERSE)
    for members in THEMES.values():
        tickers.update(members)
    tickers.update(BENCHMARK_TICKERS)
    return sorted(tickers)


# ──────────────────────────────────────────────────────────────────
# EODHD /real-time/ FETCH
# ──────────────────────────────────────────────────────────────────

def _eodhd_realtime_batch(batch):
    """Fetch a batch of /real-time/ quotes.

    batch: list of ticker symbols (BATCH_SIZE or fewer).

    EODHD real-time batched form: one ticker in the URL path, the rest in
    the `s=` query parameter as comma-separated values. With multiple
    tickers the response is a JSON array; with a single ticker it's
    a single JSON object — we normalize to a list.

    Returns list of dicts with at least `code, open, high, low, close,
    volume, previousClose`. On HTTP / parsing failure, returns [].
    """
    if not batch:
        return []

    # EODHD expects exchange-suffixed symbols (e.g. AAPL.US). Caching
    # in the OHLCV pickle uses unsuffixed; we add .US for the call.
    first = batch[0] + ".US"
    rest = ",".join(t + ".US" for t in batch[1:])
    if rest:
        url = (f"{EODHD_BASE}/real-time/{first}"
               f"?s={rest}&api_token={EODHD_API_TOKEN}&fmt=json")
    else:
        url = (f"{EODHD_BASE}/real-time/{first}"
               f"?api_token={EODHD_API_TOKEN}&fmt=json")

    last_err = None
    for attempt in range(MAX_BATCH_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ScanPerfect-Intraday/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict):
                payload = [payload]
            return payload
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue
            break  # 4xx (other than 429) won't fix with retry
        except Exception as e:
            last_err = repr(e)
            time.sleep(0.5 * (attempt + 1))

    _log(f"  batch fetch failed [{batch[0]}…{batch[-1]}]: {last_err}")
    return []


def fetch_realtime_quotes(tickers):
    """Fetch real-time quotes for all tickers, returning {ticker: quote_dict}.

    Strips the .US suffix from response codes before keying.
    """
    quotes = {}
    n = len(tickers)
    n_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE
    _log(f"Fetching {n} tickers in {n_batches} batches of {BATCH_SIZE}…")

    for i in range(0, n, BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        results = _eodhd_realtime_batch(batch)
        for q in results:
            code = q.get("code", "")
            # Code comes back as e.g. "AAPL.US" — strip exchange suffix.
            if "." in code:
                code = code.split(".", 1)[0]
            if not code:
                continue
            quotes[code] = q
        time.sleep(INTER_BATCH_SLEEP)
        if ((i // BATCH_SIZE) + 1) % 10 == 0:
            _log(f"  …{i + len(batch)} / {n} done ({len(quotes)} quotes received)")

    _log(f"Received {len(quotes)} / {n} quotes ({len(quotes)/n*100:.1f}%)")
    return quotes


def _quote_is_valid(q):
    """Real-time row is usable iff close is a finite positive number."""
    c = q.get("close")
    try:
        c = float(c)
        return c > 0 and not (c != c)  # NaN check
    except (TypeError, ValueError):
        return False


def _quote_session_date(q):
    """ET calendar date of the quote's last update, derived from its EODHD
    `timestamp` (epoch seconds). Returns a normalized (midnight), tz-naive
    pandas.Timestamp matching the cache's `date` dtype, or None when the
    timestamp field is missing or unparseable."""
    ts = q.get("timestamp")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts != ts or ts <= 0:  # NaN / non-positive
        return None
    return pd.Timestamp(datetime.fromtimestamp(ts, ET).date())


def _quote_is_stale(q, today_ts):
    """True when a real-time quote is a prior-session echo rather than today's
    live tape.

    Right after the open the (delayed) feed briefly keeps reporting the last
    completed daily bar — same open/low/close as yesterday, `close` equal to
    `previousClose` — which, if written, just duplicates yesterday's candle
    onto today's date (the morning "doubled D1 candle" bug). Two independent
    signals flag that echo:

      1. the quote's `timestamp` resolves to an ET date earlier than today, or
      2. its `close` equals its own `previousClose` to the cent.

    Either is enough to skip the ticker for this run; once the feed advances to
    the real session, both clear and the bar is written normally.
    """
    sess = _quote_session_date(q)
    if sess is not None and sess < today_ts:
        return True
    try:
        if abs(float(q.get("close")) - float(q.get("previousClose"))) < 1e-9:
            return True
    except (TypeError, ValueError):
        pass
    return False


# ──────────────────────────────────────────────────────────────────
# SUBSTITUTION
# ──────────────────────────────────────────────────────────────────

def substitute_last_bars(cache, quotes, today_et):
    """Mutate cache in place: for each ticker with a valid quote, update or
    append today's bar with the real-time O/H/L/C/V.

    cache: dict {ticker: DataFrame[date, open, high, low, close, volume, ...]}
    quotes: dict {ticker: real-time quote dict}
    today_et: pandas.Timestamp at ET date (no time).

    Returns (n_updated, n_appended, n_skipped_missing_cache, n_no_movement,
             n_invalid_quote, n_stale).
    """
    n_updated = 0
    n_appended = 0
    n_skipped_missing_cache = 0
    n_no_movement = 0
    n_invalid_quote = 0
    n_stale = 0

    today_ts = pd.Timestamp(today_et).normalize()

    for ticker, q in quotes.items():
        if not _quote_is_valid(q):
            n_invalid_quote += 1
            continue
        if ticker not in cache:
            n_skipped_missing_cache += 1
            continue

        df = cache[ticker]
        if df is None or df.empty:
            n_skipped_missing_cache += 1
            continue

        # Reject prior-session echoes before they can be written as today's
        # bar (the morning "doubled D1 candle" bug — see _quote_is_stale).
        if _quote_is_stale(q, today_ts):
            n_stale += 1
            continue

        # Build row from the quote. Open/High/Low may be missing on the EODHD
        # response (rare); fall back to close so the bar is still valid.
        def _num(k, default=None):
            v = q.get(k)
            try:
                v = float(v)
                if v != v:  # NaN
                    return default
                return v
            except (TypeError, ValueError):
                return default

        close_v = _num("close")
        open_v = _num("open", close_v)
        high_v = _num("high", close_v)
        low_v = _num("low", close_v)
        vol_v = _num("volume", 0.0)

        # Date the bar from the quote's own session timestamp, not the wall
        # clock — a quote must never be stamped with a date its data doesn't
        # belong to. Falls back to today only when the timestamp is absent
        # (those quotes have already cleared the close==previousClose gate).
        bar_date = _quote_session_date(q)
        if bar_date is None:
            bar_date = today_ts

        last_date = pd.Timestamp(df["date"].iloc[-1]).normalize()
        last_close = float(df["close"].iloc[-1])

        if bar_date < last_date:
            # Quote is older than the newest cached bar — never rewrite
            # history. (Shouldn't reach here after the staleness gate; guard.)
            n_stale += 1
            continue
        elif bar_date == last_date:
            # The bar for this session already exists → update it in place,
            # unless the price hasn't moved (nothing to refresh).
            if abs(close_v - last_close) < 1e-9:
                n_no_movement += 1
                continue
            idx = df.index[-1]
            df.at[idx, "open"] = open_v
            df.at[idx, "high"] = high_v
            df.at[idx, "low"] = low_v
            df.at[idx, "close"] = close_v
            df.at[idx, "volume"] = vol_v
            n_updated += 1
        else:
            # Append a new row for the quote's session date.
            new_row = {col: pd.NA for col in df.columns}
            new_row["date"] = bar_date
            new_row["open"] = open_v
            new_row["high"] = high_v
            new_row["low"] = low_v
            new_row["close"] = close_v
            new_row["volume"] = vol_v
            cache[ticker] = pd.concat(
                [df, pd.DataFrame([new_row])], ignore_index=True
            )
            n_appended += 1

    return (n_updated, n_appended, n_skipped_missing_cache, n_no_movement,
            n_invalid_quote, n_stale)


# ──────────────────────────────────────────────────────────────────
# ATOMIC PICKLE WRITE
# ──────────────────────────────────────────────────────────────────

def atomic_write_pickle(obj, target_path):
    """Write pickle to target_path atomically (tmp file + rename).

    Avoids the case where the dashboard observes a half-written file.
    """
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_intraday_", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
        # On Windows, os.replace overwrites atomically.
        os.replace(tmp, target_path)
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
    # Human label reflecting when the snapshot was actually taken, e.g.
    # "intraday 9:44am" for a morning manual refresh or "intraday 4:20pm" for
    # the scheduled close run — the dashboard header shows this verbatim.
    clock = now_et.strftime("%I:%M%p").lstrip("0").lower()
    meta = {
        "snapshot_et": f"{today_et.strftime('%Y-%m-%d')} {now_et.strftime('%H:%M:%S')} ET",
        "written_at": now_et.isoformat(),
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
    """Invoke theme_dashboard.main() with default args (bars=250, no --open).

    Done in-process for fast feedback in the scheduled-task log. If the
    dashboard module raises, the exception propagates to the caller —
    intraday pickle is already written, so a regen failure leaves the
    pickle in place for the next dashboard load.
    """
    _log("Regenerating theme_dashboard.html against intraday pickle…")
    # Save & restore sys.argv so the dashboard's argparse sees clean args.
    saved_argv = sys.argv
    try:
        # --skip-snapshot: the morning 7:15 task already rebuilt the
        # ext50-trendline snapshot for today's EOD. Intraday refresh
        # (4:20 PM) is the same trading day, so no new bar to fold in.
        sys.argv = ["theme_dashboard.py", "--bars", "250", "--skip-snapshot"]
        import theme_dashboard  # imported here so the import-time prints land in our log
        theme_dashboard.main()
    finally:
        sys.argv = saved_argv


# ──────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    _log("=" * 70)
    _log("ScanPerfect Intraday Refresh")
    _log("=" * 70)

    if not EODHD_API_TOKEN:
        _log("FATAL: EODHD_API_TOKEN env var not set. Cannot proceed.")
        return 2

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

    # Restrict the fetch to tickers that exist in the cache (the
    # `BF.B`-style cases that miss the cache get dropped, matching the
    # dashboard's own behavior).
    universe_in_cache = [t for t in universe if t in cache]
    missing_from_cache = [t for t in universe if t not in cache]
    if missing_from_cache:
        _log(f"  {len(missing_from_cache)} theme tickers missing from cache (skipped): "
             f"{missing_from_cache[:10]}{'...' if len(missing_from_cache) > 10 else ''}")
    _log(f"  {len(universe_in_cache)} theme tickers will be quoted")

    # Fetch quotes.
    t0 = time.time()
    quotes = fetch_realtime_quotes(universe_in_cache)
    fetch_dur = time.time() - t0
    _log(f"Fetch complete in {fetch_dur:.1f}s. ~{len(universe_in_cache) // BATCH_SIZE + 1} API calls.")

    valid_quotes = {t: q for t, q in quotes.items() if _quote_is_valid(q)}
    success_frac = len(valid_quotes) / max(1, len(universe_in_cache))
    _log(f"Valid quotes: {len(valid_quotes)} / {len(universe_in_cache)} "
         f"({success_frac*100:.1f}%)")

    if success_frac < MIN_SUCCESS_FRACTION:
        _log(f"FATAL: success rate {success_frac*100:.1f}% < threshold "
             f"{MIN_SUCCESS_FRACTION*100:.0f}%. Treating as outage. "
             f"Intraday pickle untouched.")
        return 3

    # Substitute.
    today_et = pd.Timestamp(datetime.now(ET).date())
    _log(f"Substituting last bars (today = {today_et.date()} ET)…")
    n_upd, n_app, n_skip, n_nomove, n_invalid, n_stale = substitute_last_bars(
        cache, valid_quotes, today_et)
    _log(f"  updated: {n_upd}  appended: {n_app}  skipped (not in cache): {n_skip}  "
         f"no-movement: {n_nomove}  stale (prior-session echo): {n_stale}  invalid: {n_invalid}")

    if n_upd + n_app == 0:
        _log("No bars were updated or appended (market closed / no movement / "
             "feed still echoing the prior session). Intraday pickle untouched.")
        return 4

    # Atomic write.
    _log(f"Writing intraday pickle: {INTRADAY_PICKLE}")
    t0 = time.time()
    atomic_write_pickle(cache, INTRADAY_PICKLE)
    write_marker(today_et, n_upd, n_app, len(valid_quotes))
    write_dur = time.time() - t0
    size_mb = os.path.getsize(INTRADAY_PICKLE) / (1024 * 1024)
    _log(f"  wrote {size_mb:.1f} MB in {write_dur:.1f}s")

    # Regen the dashboard against the freshly written intraday pickle.
    try:
        regenerate_dashboard()
    except Exception as e:
        _log(f"WARNING: dashboard regen failed: {e!r}")
        # Pickle is already on disk; user can manually re-run theme_dashboard.
        return 5

    _log("=" * 70)
    _log("Intraday refresh complete.")
    _log("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
