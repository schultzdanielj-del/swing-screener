# TODO

## DTSS Pipeline Status (as of 2026-02-22)

| Step | Status | Notes |
|------|--------|-------|
| 1 Load | ✅ Done | Data + TA knowledge loaded |
| 2 Receive | ✅ Done | 26 examples with LSP data (`data/dtss_lsp_data.json`) |
| 3 Profile | **⚠️ IN PROGRESS** | Old profiler (528 Python features) abandoned. New PCF-native profiler being built: ~660 PCF expressions, 4,167 tradable tickers, 5 min budget. Rules defined, implementation next. |
| 4 Collaborate | Not started | Blocked on Step 3 |
| 5 Backtest | Not started | |
| 6 Market Context | Not started | |
| 7 EV Optimize | Not started | |

## Other Priorities

| # | Task | Description |
|---|------|-------------|
| 1 | **3-4DB backtest → optimizer** | Run 800+ backtest signals through outcome precomputation + management optimizer. Get real EV numbers, not just example-only. |
| 2 | **Market regime analysis (Step 6)** | Build the "when to trade it" filter. 3-4DB showed 6-7x signal spikes during stage transitions — quantify which market conditions produce winners vs losers. |
| 3 | **Daily scan automation** | Nightly job: run scan conditions against today's data, surface tomorrow's candidates. The whole point of the project. |
| 4 | **HTF setup examples** | Third setup type has zero examples. Need to collect and load them before any analysis can run. |
