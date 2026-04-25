# Earnings-cap audit — `scripts/signal_exit_grinder.py` lines 210–245

## What the current code does

Two functions form the current earnings-cap path:

**`_load_earnings_map()`** (lines 210–228) reads `(ticker, earnings_date)` from `scanperfect.db.earnings_dates`, groups into `{ticker: [sorted list of earnings-date strings]}`. Dates remain as strings; no datetime parsing. One dict-build pass at startup — this part is fine.

**`_bars_to_next_earnings(df, scan_idx, earnings_list)`** (lines 231–245) returns "trading bars from `scan_idx + 1` to the first earnings date strictly after the signal date." Steps:

1. `signal_date = str(df["date"].values[scan_idx])[:10]` — take the 10-char YYYY-MM-DD prefix of the signal bar's date.
2. `dates_after = [str(d)[:10] for d in df["date"].values[scan_idx + 1:]]` — rebuild a Python list of YYYY-MM-DD prefixes for every bar after the signal. **This materializes ~100s of strings per call just to look up one earnings date.**
3. Outer loop: for each `ed` in `earnings_list`, if `ed > signal_date` (string compare), break into inner loop.
4. Inner loop: enumerate `dates_after`, find the first `d >= ed`, return `i + 1`.
5. If loop ends with no match: return None.

## Where it can drift

**(1) String-compare + re-enumerate is O(E × N) per signal.** For each signal, worst case every earnings date and every forward bar is walked. With thousands of signals × dozens of earnings × hundreds of forward bars, this is a real CPU cost. Not a correctness bug, but it's the "wild approximation" feel — doing dictionary-style work each call.

**(2) Trading-day vs calendar-day mismatch is silent.** `earnings_dates` from yfinance are stored as calendar dates. Some announcements are scheduled on weekends or holidays (rare but happens — some shareholder meetings, foreign listings). If `ed` falls on a Sunday, the inner loop finds the first bar with `d >= ed`, which would be Monday. That's the right intent. But if `ed` falls on a trading holiday inside a weekend-like block, the behavior is `>= ed` picks the next trading day. This is typically what we want — but it's implicit, not asserted.

**(3) Casing / whitespace drift.** No `.strip()`, no `.upper()`. If any row in `earnings_dates` has a trailing space, time component, or different case, the comparison fails silently (the earnings date is effectively skipped). With ~2,500 rows in the table it's worth checking once, not assuming.

**(4) `ed > signal_date` is a string compare, not a date compare.** Python string compare on YYYY-MM-DD is order-preserving **if the strings are well-formed and zero-padded**. Any non-ISO format (e.g., `2025-1-5` unpadded) breaks the ordering silently. yfinance output is normally ISO, so this is a defensive concern more than a known bug.

**(5) Signal at last bar of cache.** If `scan_idx == len(df) - 1`, `dates_after` is empty; the inner loop never executes; returns None. Handled correctly — no earnings cap applied, which is the right behavior (can't race forward anyway).

**(6) Early-return miss.** When `ed > signal_date` matches, the inner loop searches for `d >= ed`. If it finds nothing (earnings beyond OHLCV window), the outer function returns None instead of trying the next earnings date. That's fine for this use case (one future earnings is the target) but worth flagging: the first-match-only behavior means the "next earnings after signal that's within the OHLCV window" isn't found if the nearest earnings is out-of-window.

## Proposed replacement (pseudocode)

This is the clean pattern already used by `research/07_variant_sweep.py`'s `earnings_cap()`. Drop in as a replacement for both helpers, no signature change needed if we rename inputs.

```
at startup, build:
  earnings_map_np[ticker] = np.array(sorted(set(all earnings_date strings for ticker)), dtype='<U10')

per call (ticker, scan_idx, df_dates_str_array):
  ern = earnings_map_np.get(ticker)
  if ern is None or len(ern) == 0:
      return None
  signal_date_str = df_dates_str_array[scan_idx]     # already cached as array
  pos = np.searchsorted(ern, signal_date_str, side='right')
  if pos >= len(ern):
      return None
  next_ern = ern[pos]
  bp = np.searchsorted(df_dates_str_array, next_ern, side='left')
  if bp <= scan_idx:
      return None
  return bp - scan_idx   # trading-bars-until-earnings (bar index of earnings)
```

Two benefits:

1. O(log E + log N) per call via numpy binary search, not O(E × N).
2. One helper instead of two. No Python list materialization per signal.

## What to do

No code edit this session. The plan's E5 note defers the actual port to the grinder rebuild. This audit documents the bug surface + proposed fix so when the rebuild happens, the fix goes in as one clean change rather than an afterthought.

Verification after porting: run with the old and new helper in parallel for one setup, compare the per-signal cap-bar values — they should agree 100% for correct inputs. Any discrepancy indicates string-form drift in the DB worth a one-time cleanup.

## Note on correctness implication for prior session results

The string-compare implementation is **probably correct** on well-formed ISO dates, which is what yfinance produces. No known miscounts in prior session outputs. The flag "wild approximation" from MFE_CAPTURE_PROJECT.md refers more to the implementation shape (per-call list rebuild, string compare where ISO-lex-order is relied on implicitly) than a known off-by-one. The replacement is a hygiene win, not a bugfix.
