# OHLCV Cache

**File:** `local_runner/cache_builder.py`

---

## Purpose

Provide the most accurate, nightly-updating daily, weekly, and monthly OHLCV data for all US common stocks and ETFs. This is the foundational data layer consumed downstream by the expression cache, matrix builder, market cache, grinders, signal filter, and the PySide6 desktop app.

Updates to the data do not destroy the ability for setup scan conditions to find the same historical signals and setup examples every day. The only exception is delisting — when a ticker is removed from the EODHD exchange list, it is dropped from the cache permanently.

---

## Storage

All files live in `local_runner/cache/`.

| File | Contents | Size (approx) |
|------|----------|---------------|
| `universe_ohlcv_daily.pkl` | Full daily OHLCV for ~11,500 tickers from 2016-01-01 to present | ~920 MB |
| `universe_ohlcv_weekly.pkl` | Weekly OHLCV for same universe, same date range | ~170 MB |
| `universe_ohlcv_monthly.pkl` | Monthly OHLCV for same universe, same date range | ~45 MB |
| `ticker_reference.json` | First trade date per ticker (used for validation) | ~1 MB |
| `cache_daily_meta.txt` | ISO timestamp of last daily cache save | tiny |
| `cache_weekly_meta.txt` | ISO timestamp of last weekly cache save | tiny |
| `cache_monthly_meta.txt` | ISO timestamp of last monthly cache save | tiny |
| `universe_ohlcv.pkl` | Legacy 300-bar daily cache (kept for backward compat) | deprecated |

Each pickle is a Python dict: `{ticker_string: pandas_DataFrame}`. Each DataFrame has columns: `date`, `open`, `high`, `low`, `close`, `volume`, `dvol_20d`. All OHLC values are fully adjusted (split + dividend). Volume is raw. `dvol_20d` is the 20-bar rolling mean of `close * volume`, computed in-place on every save.

`HISTORY_START = "2016-01-01"` is the single constant that controls the start date for all three timeframes.

---

## Tradable universe filter (applied by grinder and live scan)

The full OHLCV cache contains ~11,500 tickers. The grinder and live-scan paths apply an additional **per-bar liquidity filter** to produce the effective tradable universe (~3,500–4,000 tickers with qualifying bars). This filter lives in `pyramid_grinder.compute_tradable_masks()` and must match the corresponding thresholds in any live-scan code.

A bar is considered tradable if the ticker met ALL of the following criteria **at that bar** (not just at today):

- Close ≥ $1.00
- 20-day average dollar volume ≥ $4,000,000 (uses the `dvol_20d` column stored in the OHLCV pickle)
- 20-bar ADRP ≥ 1.8% (TC2000-style: `(mean(H/L) − 1) × 100`)

The filter is **per-bar, not per-ticker**. A ticker that is a penny stock today but was tradable in 2022 still contributes its 2022 bars to historical search. A ticker that is tradable today but wasn't in 2020 only contributes bars from dates where it qualified. This mirrors historical scan behavior and prevents injecting illiquid-historical signals into condition derivation.

**Thresholds are authoritative in `pyramid_grinder.compute_tradable_masks()`** (as of 2026-04-11). When the doc and the code disagree, the code wins — update the doc, not the code.

---

## Data Sources

### EODHD (primary)

API base: `https://eodhd.com/api`
Plan: $19.99/month EOD Historical Data, 100K calls/day, 1,000/min.

**Endpoints used:**

| Endpoint | Purpose | Cost | Notes |
|----------|---------|------|-------|
| `exchange-symbol-list/US` | Universe sync — get all tickers, types, exchanges | 1 call | Filtered to Common Stock + ETF on `NYSE`, `NASDAQ`, `NYSE ARCA`, `BATS`, `NYSE MKT`, `AMEX` (the last two added in Session 5, recovered ~297 tickers including AMEX-listed names like EQX, UAMY, REPX, LEU, BTG). Returns ~11,500 tickers. |
| `eod/{ticker}.US` | Per-ticker historical OHLCV | 1 call each | Returns unadjusted OHLC + `adjusted_close`. Used for full rebuilds and validation retries. |
| `eod-bulk-last-day/US` | All US tickers' OHLCV for one date | 100 calls | Returns ~50K tickers in one response. Used for nightly append — one call per new trading day. |
| `eod-bulk-last-day/US?type=splits` | All US splits on a given date | 1 call | One call per trading day in the gap. Used to detect tickers needing full refetch. |

**Adjustment model:** EODHD returns unadjusted OHLC + `adjusted_close`. The ratio `adjusted_close / close` is applied to all four OHLC columns to produce fully-adjusted prices matching the format downstream consumers expect.

**Limitations:**
- Bulk endpoint for the current day is incomplete until ~6-8 hours after market close. Tickers missing from bulk are backfilled via yfinance.
- Rate limit: 1,000 calls/min. The batched fetch system (`_batched_fetch`) adapts worker count and sleep time based on failure rate — backs off when rate limited, ramps up when clean.
- Per-ticker endpoint returns `None` on any HTTP error (including 429 rate limit). `_batched_fetch` retries these internally.
- Some tickers on the EODHD exchange list have no price data at all (truly dead but not yet delisted). These return `None` and are tracked as permanently failed.

### yfinance (gap fill only)

Used only to backfill tickers missing from the EODHD bulk endpoint on the current day. Fetches in batches of 80 with ~6 second pauses between batches. Uses `auto_adjust=False` to get raw OHLC + Adj Close, then applies the same `adjusted_close / close` ratio as EODHD.

Not used for historical backfill, weekly, or monthly data.

### Ticker Reference

`ticker_reference.json` stores the first trade date for every ticker, built by fetching a narrow date range from EODHD per ticker. For tickers that existed before 2016-01-01, the reference date is 2016-01-04 (first SPY trading day in range). For IPOs after that date, it's their actual first bar date.

Used by validation to confirm data starts at the right date. Updated automatically during full builds and when new tickers are added.

---

## Universe Management (IPOs and Delistings)

Every nightly append run syncs against the EODHD exchange symbol list:

**IPOs/new listings:** Tickers in the EODHD list but not in the cache are detected as new. They get a full historical fetch from `HISTORY_START` and are added to the daily pickle. The ticker reference is updated with their first trade date. Weekly and monthly caches pick them up on their next sync cycle.

**Delistings:** Tickers in the cache but not in the EODHD list are removed from the daily pickle immediately. Weekly and monthly caches trim orphan tickers (tickers not in the daily cache) on their next sync cycle.

There is no bar minimum — new IPOs are included immediately regardless of how much history they have.

A standalone universe sync command is also available: `python local_runner/cache_builder.py --sync`. This adds/removes tickers without appending new bars, and runs a full sweep on weekly + monthly to catch up.

---

## Nightly Append

Called by `nightly.py` steps 2-4. The append path updates existing caches with new trading days' data without rebuilding from scratch.

### Daily append (`append_daily_cache()`, step 2)

1. **Freshness check:** Fetch SPY from EODHD. Compare SPY's last bar date to the cached SPY last bar date. If no new trading day, return early.

2. **Universe sync:** Fetch the EODHD exchange symbol list. Add new tickers (IPOs), remove delisted tickers.

3. **Split detection:** Hit the EODHD bulk splits endpoint for each trading day in the gap (typically 1 day). Any ticker that split gets flagged for full refetch because its entire adjusted price history changed.

4. **Bulk append:** For each new trading day, fetch the EODHD bulk endpoint (one API call per day, returns all ~50K US tickers). Apply the adjustment ratio and append the new bar to each ticker's DataFrame. Tickers already current (from a partial prior run) are skipped.

5. **yfinance backfill:** Tickers missing from the EODHD bulk response get their new bar from yfinance in batches of 80.

6. **Split refetch:** Tickers that split get a full historical refetch from EODHD (per-ticker endpoint).

7. **New ticker fetch:** IPO tickers get a full historical fetch from EODHD.

8. **Validation:** Every ticker in the cache (except yfinance-filled ones) is validated:
   - First bar date must be within 5 SPY trading days of `max(firstTradeDate, HISTORY_START)`
   - Last bar date must equal SPY's last date
   - No duplicate dates in the DataFrame
   - Tickers failing the last-date check are "stale" and get refetched
   - If a refetch returns valid data with the same stale last date, the ticker is accepted — it genuinely hasn't traded on recent days (this is not rate limiting, which returns `None`)

9. **Save:** The daily pickle is saved. Always saves, even on interrupt (partial progress is resumable — already-updated tickers are detected and skipped on the next run).

### Weekly append (`append_weekly()`, step 3)

Fetches new weekly bars for each ticker from the day after its last cached weekly bar. Uses EODHD's server-side `period=w` aggregation — not resampled from daily. Tickers not in the daily cache are trimmed.

### Monthly append (`append_monthly()`, step 4)

Same as weekly but with `period=m`. Monthly bars only close at month-end, so this often has nothing to fetch mid-month.

---

## Full Rebuild

Rebuilds the entire cache from scratch by fetching full history for every ticker.

### Daily rebuild

`python local_runner/cache_builder.py --daily --force`

1. Fetch SPY as ground truth
2. Fetch the EODHD universe (~11,500 tickers)
3. Build/update the ticker reference (first trade dates)
4. Fetch full history for every ticker via the per-ticker EODHD endpoint, from `HISTORY_START` to present
5. Validate all tickers (same checks as nightly append)
6. Save the daily pickle

Uses `_batched_fetch` with 80 concurrent workers, adaptive backoff, and up to 3 retry rounds. Timing depends on EODHD API responsiveness — expect 15-30 minutes for ~11,500 tickers.

### Weekly + monthly rebuild

`python local_runner/cache_builder.py --htf --force`

Fetches weekly and monthly bars for every ticker in the daily cache via the per-ticker EODHD endpoint with `period=w` or `period=m`. Uses EODHD's server-side aggregation, not resampled from daily.

### Full rebuild (all three)

`python local_runner/cache_builder.py --all --force`

Runs daily rebuild, then weekly + monthly rebuild sequentially. Frees daily data from memory before starting HTF to manage RAM.

**After any full rebuild:** The expression cache must also be fully rebuilt, because minor price differences between the old and new data will cause expression values to differ. This is expected and correct.

---

## Commands

All commands run from the repo root. EODHD API token must be set: `set EODHD_API_TOKEN=69caeae1b24de8.25880244`

| Command | What it does |
|---------|-------------|
| `python local_runner/cache_builder.py --daily` | Full daily rebuild (skips if fresh, use `--force` to override) |
| `python local_runner/cache_builder.py --daily --force` | Force full daily rebuild |
| `python local_runner/cache_builder.py --htf` | Full weekly + monthly rebuild |
| `python local_runner/cache_builder.py --htf --force` | Force full weekly + monthly rebuild (discards existing) |
| `python local_runner/cache_builder.py --weekly --force` | Rebuild weekly only |
| `python local_runner/cache_builder.py --monthly --force` | Rebuild monthly only |
| `python local_runner/cache_builder.py --all --force` | Rebuild daily + weekly + monthly |
| `python local_runner/cache_builder.py --sync` | Universe sync only (add IPOs, remove delisted, no bar append) |
| `python local_runner/cache_builder.py --build-reference` | Rebuild ticker reference file |
| `python local_runner/cache_builder.py --status` | Show daily + HTF cache status |
| `python local_runner/cache_builder.py --daily-status` | Show daily cache status only |
| `python local_runner/cache_builder.py --htf-status` | Show HTF cache status only |

---

## Downstream Consumers

These files read the OHLCV pickles directly. Changes to the pickle schema or file paths require updating all of them.

| Consumer | What it reads | How |
|----------|--------------|-----|
| `expr_cache_builder.py` | Daily + weekly + monthly pickles | Loads all three for expression computation |
| `matrix_builder.py` | Daily pickle (fallback path) | `load_daily_cache()` |
| `market_cache_builder.py` | Daily pickle | Reads ~227 ETF/stock instruments from it |
| `scanperfect.py` | Daily pickle | Loads full pickle into memory for charting |
| `signal_filter.py` | Daily pickle | For OHLCV lookups during scan |
| `vectorized_cache_builder.py` | Daily pickle | Reads for vectorized indicator computation |
| `fetch_fundamentals.py` | Daily pickle | For ticker list only |

---

## Validation Design

Validation exists to catch one specific failure mode: rate-limited or partial API responses that return truncated DataFrames that look valid but are missing bars. This happened early in development and caused a wasted multi-hour cache rebuild.

The validation checks are:

1. **First bar date:** Must be within 5 SPY trading days of `max(firstTradeDate, HISTORY_START)`. Catches data that starts too late (truncated front).
2. **Last bar date:** Must equal SPY's last trading date. Catches data that's missing recent bars (truncated end).
3. **No duplicate dates:** Catches data corruption from bad appends.

Tickers that fail the last-date check after a refetch that returns valid data with the same last date are accepted — they genuinely don't trade every day (thinly traded names, halted stocks). This is distinguished from rate limiting because rate-limited requests return `None`, not valid DataFrames.

The previous validation design (exact bar count match against SPY) was removed because ~1,800 tickers legitimately trade fewer days than SPY. These would fail validation on every run, triggering pointless refetches.
