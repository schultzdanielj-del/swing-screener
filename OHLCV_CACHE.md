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

Each pickle is a Python dict: `{ticker_string: pandas_DataFrame}`. Each DataFrame has columns: `date`, `open`, `high`, `low`, `close`, `volume`, `dvol_20d`. Under the **2026-04-22 OHLCV adjustment policy**, OHLC values are forward-split-adjusted (continuous across split boundaries, IBKR-style) but NOT dividend-adjusted. `volume` is forward-split-adjusted by EODHD/yfinance and passed through unchanged. Both close and volume share the same forward-split-adjusted scale, so `close × volume` is smooth across split boundaries and `dvol_20d` (the 20-bar rolling mean of that product) is correct notional dollar volume across the full history.

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

**Verify all costs at source:** https://eodhd.com/financial-apis/bulk-api-eod-splits-dividends and https://eodhd.com/pricing. The numbers below are EODHD's documented per-call weights. Do not rely on this table for budgeting — always re-verify on EODHD's site before high-volume operations, since their pricing changes and in-repo notes can drift.

| Endpoint | Purpose | Cost (verified weight) | Notes |
|----------|---------|------|-------|
| `exchange-symbol-list/US` | Universe sync — get all tickers, types, exchanges | 1 call | Filtered to Common Stock + ETF on `NYSE`, `NASDAQ`, `NYSE ARCA`, `BATS`, `NYSE MKT`, `AMEX` (the last two added in Session 5, recovered ~297 tickers including AMEX-listed names like EQX, UAMY, REPX, LEU, BTG). Returns ~11,500 tickers. |
| `eod/{ticker}.US` | Per-ticker historical OHLCV | 1 call each | Returns raw OHLC + `adjusted_close`. Under the 2026-04-22 policy we use raw `open/high/low/close` and ignore `adjusted_close`. Used for full rebuilds and validation retries. |
| `splits/{ticker}.US` | Per-ticker split history | 1 call each | Returns all splits for one ticker. Use this for split discovery instead of bulk splits — avoids the 100x weight penalty. |
| `eod-bulk-last-day/US` | All US tickers' OHLCV for one date | **100 calls per request** | Returns ~50K tickers in one response. Used for nightly append — one call per new trading day. **Daily quota at 100K means at most 1,000 bulk calls/day.** |
| `eod-bulk-last-day/US?type=splits` | All US splits on a given date | **100 calls per request** | One call per trading day in the gap. Used to detect tickers needing full refetch. **NOT 1 call** — earlier versions of this doc were wrong; the bulk-API pricing applies to every variant of `eod-bulk-last-day`. |
| `user` | Quota check | 1 call | Returns `apiRequests` (count used today) and `dailyRateLimit`. Poll this before/during high-volume runs to abort at 80% of cap. |

**Adjustment model (2026-04-22 policy):** EODHD's `close` field is RAW (TC2000-style: pre-split bars at their pre-split share price; no dividend back-adjustment). EODHD's `adjusted_close` is split-AND-dividend adjusted and is NOT used. To produce IBKR-style continuous prices we fetch the per-ticker splits list from `splits/{TICKER}.US`, compute a per-bar cumulative forward-split factor (product over all splits S where S.date > bar.date of `A/B` for split ratio `B/A`), and multiply raw OHLC by that factor. EODHD's `volume` is already forward-split-adjusted (verified empirically on RGTU's 3:1 / 1:3 pair and on NVDA's 2024-06-10 10:1 split) and is passed through unchanged. The `_eodhd_to_dataframe` parser accepts an optional `splits=` argument; `_eodhd_download` automatically attaches the per-ticker splits list (with a per-process `_splits_cache` so repeated fetches don't re-hit the splits endpoint).

**Distributions:** NOT back-adjusted. Sub-ADR distribution drops show as real ex-date price drops, matching TC2000 and IBKR. No distributions table is maintained.

**Limitations:**
- Bulk endpoint for the current day is incomplete until ~6-8 hours after market close. Tickers missing from bulk are backfilled via yfinance.
- Rate limit: 1,000 calls/min. The batched fetch system (`_batched_fetch`) adapts worker count and sleep time based on failure rate — backs off when rate limited, ramps up when clean.
- Per-ticker endpoint returns `None` on any HTTP error (including 429 rate limit). `_batched_fetch` retries these internally.
- Some tickers on the EODHD exchange list have no price data at all (truly dead but not yet delisted). These return `None` and are tracked as permanently failed.

### yfinance (gap fill only, plus full-history sweep)

Used in two places under the 2026-04-22 policy:

1. **Current-day gap fill** (`_yfinance_fill_gaps` / `_yf_apply_one`) — for tickers missing from the EODHD bulk endpoint on the current trading day. Fetches in batches of 80 with ~6s pauses. Uses `auto_adjust=False` and takes raw `Open/High/Low/Close + Volume` directly. yfinance under `auto_adjust=False` returns split-forward-adjusted-not-dividend-adjusted OHLC and forward-split-adjusted Volume — verified empirically on NVDA's 2024-06-10 10:1 split — which matches the cache-wide convention so values stitch directly into the EODHD-derived series with no further transformation.

2. **Full-history sweep at end of `--daily --force`** (`yfinance_full_history_sweep`) — final-resort recovery for any ticker EODHD permanently failed across all retries. Same `auto_adjust=False` + raw values approach. Without this sweep, EODHD-failed tickers would silently disappear from the rebuilt cache.

Not used for HTF (weekly / monthly).

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
4. **Pre-fetch splits** for every ticker via per-ticker `/splits/{TICKER}.US`, populating an in-process splits cache so the OHLCV phase below is single-endpoint
5. Fetch full history for every ticker via the per-ticker EODHD `/eod/{TICKER}.US` endpoint, from `HISTORY_START` to present. Each per-ticker fetch applies the new policy: raw `close` × cumulative-future-split factor → forward-split-adjusted OHLC; volume passed through unchanged
6. Validate all tickers (same checks as nightly append)
7. **yfinance full-history sweep** for any ticker EODHD permanently failed across all retries — final-resort recovery so EODHD-failed tickers do not silently disappear from the rebuild
8. Save the daily pickle

Uses `_batched_fetch` with **conservative pacing** (20 workers, 8s min-sleep between batches of 100, 5 retry sweeps) so the per-min EODHD request rate stays well under the 1000/min limit and the adaptive-backoff branch should not fire under normal conditions. Timing: ~17 min splits prefetch + ~22 min OHLCV main + ~3 min validation = **~45 min for ~11,800 tickers**.

### Weekly + monthly rebuild

`python local_runner/cache_builder.py --htf --force`

Fetches weekly and monthly bars for every ticker in the daily cache via the per-ticker EODHD endpoint with `period=w` or `period=m`. Uses EODHD's server-side aggregation, not resampled from daily. The new policy is applied identically (raw close × forward-split factor; pass-through volume). Each timeframe runs the same conservative pacing as the daily rebuild; the splits cache populated by weekly is reused by monthly in the same process.

The HTF nightly **append** path also detects splits over the gap window (via `detect_splits` over the daily SPY trading calendar) and routes affected tickers to a full-history HTF refetch — without this, HTF would silently produce a discontinuity at the split boundary because the cached historical bars carry the old cumulative-future-split factor.

### Full rebuild (all three)

`python local_runner/cache_builder.py --all --force`

Runs daily rebuild, then weekly + monthly rebuild sequentially. Frees daily data from memory before starting HTF to manage RAM.

---

## Adjustment policy — broker comparison

The cache stores **forward-split-adjusted, NOT dividend-adjusted** OHLC + Volume (effective 2026-04-22). This matches IBKR's display convention: continuous through splits, real ex-date drops on distributions.

| Source | Splits | Distributions |
|---|---|---|
| TC2000 | shows as gap on chart | real ex-date price drop |
| IBKR | continuous (forward-split-adjusted) | real ex-date price drop |
| **This cache** | **continuous (forward-split-adjusted)** | **real ex-date price drop (no back-adjustment)** |

For non-distribution days the cache equals TC2000's raw display exactly (within rounding). On a dividend ex-date the cache shows the real price drop, while TC2000 shows the same and IBKR shows the same. Across split boundaries the cache and IBKR are continuous; TC2000 shows a gap.

The earlier `backfill_raw_close.py` repair script is superseded under this policy (the new write-time logic produces correct forward-split-adjusted OHLC at the moment data lands in the cache; no missed-split repair pass is ever needed). It has been moved to `archive/shelved_scripts/backfill_raw_close.py` for historical reference only — do not run.

---

## Commands

All commands run from the repo root. EODHD API token must be set in your shell environment: `set EODHD_API_TOKEN=<your_token>` (Windows) or `export EODHD_API_TOKEN=<your_token>` (bash). Never paste the literal token into any file in the repo — it leaks to anyone with read access. If you suspect leak, rotate at https://eodhd.com immediately.

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

---

## Pending research

### Delisted-ticker retention policy change (shelved 2026-04-24)

Current policy deletes delisted tickers on universe sync (`sync_universe()` lines 2354-2358, duplicated in `append_daily_cache()` lines 1547-1549). Two consequences identified 2026-04-24:

1. **Orphaned examples.** Three curated examples (EXAS HTF 2021-01-19, PSTG BF 2024-03-22, NGD BASE 2026-01-05) are now unreadable by the grinder and classifier — their tickers were dropped in the most recent cache rebuild.

2. **Survivorship bias in the training pool.** Every historical bar of a delisted ticker that would have qualified under the per-bar tradable filter disappears from the grinder's scan universe. Setups get trained on "tickers that survived," so backtested WR/EV look better than they will perform forward on a universe that includes eventually-failing names.

**Verified via EODHD probe (2026-04-24):** pre-delisting EOD history is retained and retrievable via `/eod/{TICKER}.US`. TWTR canary returns 9 years of history 3.5 years post-buyout. EXAS/PSTG/NGD all return full history up to their respective last-trade dates (2025-11-19 / 2026-04-16 / 2026-03-23). Recovery is mechanically possible.

**Full 6-phase plan drafted:** `C:\Users\Dan\.claude\plans\curried-booping-key.md`

Phase summary:
- **B.** Policy change in `cache_builder.py`: parallel `ticker_status.json` + `ticker_anomalies.json` files; mark terminal instead of delete; skip terminals in fetch/validate; handle edge cases (NGD-class anomalies, ticker reuse, reversal of delisting).
- **A.** Recover the three orphaned examples using Phase B's `insert_terminal_ticker()` helper.
- **C.** One-time backfill script `backfill_delisted_tickers.py` — fetch EODHD's delisted list filtered to 6 tracked exchanges × Common Stock/ETF × last-trade ≥ 2016-01-01; insert each. Estimated ~12-32K EODHD calls, fits 100K daily quota.
- **D.** `load_live_tickers()` helper wired into live-scan-path consumers (`intermediate_cache_builder`, `signal_filter`, `scan_engine`, `fetch_fundamentals`). Training-pool consumers intentionally left unfiltered.
- **E.** Per-ticker expression-cache compute for newly-added terminals via existing `_compute_and_save_ticker()`. No `.state`/`.lookback` needed (forward-prop never triggers for terminals).
- **F.** Grind provenance tagging — add `training_pool: "survivors_only" | "full"` to grind metadata; retroactively tag existing results as `survivors_only`.

**Not currently committed to.** Decision pending on whether survivorship bias matters enough to pay the complexity cost. The plan file contains the full edge-case table, phase dependencies, verification steps, and open decision points.
