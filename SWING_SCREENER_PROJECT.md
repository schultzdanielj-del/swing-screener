# Swing Screener Project — State Document

**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener (branch: v2)
**Railway:** https://web-production-e3025.up.railway.app (seed vault / file mirror only)

---

## RULES — READ THESE FIRST

1. **NEVER proceed with work until user gives EXPLICIT go-ahead.** Wait for "go", "yes", "do it".
2. **If you need credentials, access, tokens — STOP and ask immediately.**
3. **Push all work to GitHub before ending a chat.**
4. **Don't repeat context the user already knows.** Be concise.
5. **NEVER dump large data (CSV, JSON) into context.** Process via scripts.
6. **GitHub token for bash git push:** Stored in Claude memory. Use bash `git push`.
7. **Before ANY TA work — READ `ta_knowledge.md` FIRST.**
8. **All data from local files.** SQLite (`data/scanperfect.db`), OHLCV pickle, local cache JSONs. Never Railway. OHLCV uses EODHD bulk + yfinance gap fill. yfinance also used for fundamentals and earnings dates.
9. **Break work into small tasks.** Update docs when finishing tasks that alter them.
10. **READ THE ACTUAL CODE before making claims.** Verify, don't guess.
11. **At session start:** Read the full codebase before proposing anything.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~11,500 tickers nightly (Common Stock + ETF on NYSE, NASDAQ, NYSE ARCA, BATS, NYSE MKT/AMEX, sourced from EODHD), finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## Current state (high level)

Detailed component status lives in the per-component specs. This section is a short overview; it will go stale fast, so treat it as a pointer, not a source of truth.

- **Expression cache** (16,216 expressions × 11,534 tickers, float16 on disk; rebuilt 2026-04-25 in worktree): built and operational. Full rebuild via `expr_cache_builder.py --build`. Forward-prop append path exists for nightly updates but is **best-effort** and is NOT what the consensus pipeline uses — see `FORWARD_PROP_SPEC.md` and `CONSENSUS.md`.
  - **Shipped 2026-04-25 (worktree branch `feature-build-2026-04-24`):** 3 cache features rebuilt + forward-prop wired — Extension Chart Levels (12 D1 expressions; revised down from spec's original 36 because the feature is D1-only — D1 50/200 SMA carry the structural significance, weekly/monthly versions of those MAs do not), Extension Chart Trendlines (104; full ext history now, 260-bar cap removed), MOC (61). Total +177 expressions; cache 16,039 → 16,216. Built against post 2026-04-23 OHLCV distribution-fix. Universe rebuild 122.7 min; setup_forward_prop 14.2 min. Spec details + shipping notes in `EXPRESSION_ENGINE_V2.md` §6. Awaiting merge to main.
- **OHLCV caches** (daily/weekly/monthly): nightly append via `cache_builder.py`. Universe synced against EODHD exchange symbol list, IPOs added, delistings removed. See `OHLCV_CACHE.md`.
- **Nightly pipeline** (`local_runner/nightly.py`): uses the `.im` intermediate cache (`intermediate_cache_builder.py`) + `scan_engine.py` for the live-scan path. Independent of the grinder's `.npz` expression cache. See `NIGHTLY_REFRESH.md` + `DEPENDENCY_MAP.md` Chain 5.
- **Consensus pipeline** (`scripts/run_consensus_pipeline.py`): built through Increment 10 (Sessions 1–5). `test_consensus_pipeline.py --setup dtss` passes 9/9. Per-grind cost ~11–13 min at current defaults (prior BRKO benchmark). Full real run still pending — deferred for quality-preserving speed optimization work in the next session. Active tracker: `CONSENSUS.md`.
- **Grinders**: signal grind, signal exit grind, refinement grind (`pyramid_grinder.py`). EV grinder and profit grinder exist but are **deferred** from the consensus pipeline — they'll be wired back in during a future "live EV ranked watchlist" build. See the DEFERRED banner in `ENTRY_GRINDER.md` for the entry grinder status.
- **Native desktop UI** (`scanperfect.py`, PySide6): pipeline flowchart + workspaces. Replaces the legacy HTML UI. Implementation ongoing; design in `UI_FLOW.md`.

### Setup types in play

| Setup | Examples | Status |
|---|---|---|
| DTSS (Double Top Short Sell) | 73 | Primary benchmark for consensus pipeline |
| 3-4DB | 16 | Examples loaded, not yet ground |
| HTF | 32 | Bootstrap grind complete |
| BF | 45 | Bootstrap grind complete |
| BASE | 42 | Bootstrap grind complete |

---

## ARCHITECTURE

Everything runs locally on Dan's desktop (i5-12600k, 32GB RAM, Windows).

```
scanperfect.py             # PySide6 native desktop app (THE interface)
audit.py                   # Code auditor — separate Claude instance reviews every change

local_runner/              # Grinder system
├── nightly.py             # 10-step nightly pipeline (5:00pm ET)
├── pyramid_grinder.py     # Signal grind + refinement grind (--blackout)
├── cache_builder.py       # OHLCV caches (daily + HTF) — EODHD bulk + yfinance hybrid
├── expr_cache_builder.py  # Expression series cache (~111 GB, float16, 6yr)
├── market_cache_builder.py # 272 instrument expression series (EODHD + pickle + yfinance + FRED)
├── file_mirror.py         # Railway backup mirror
└── cache/                 # All local caches + grind output JSONs

scripts/                   # Analysis scripts
├── ev_grinder.py          # EV grinder (Phase 3) — COMPLETE
├── exit_grinder.py        # Exit condition optimizer
├── entry_candle_scorer.py # Vetting sort utility
├── seed_vault.py          # Backup/restore to Railway
├── fetch_fundamentals.py  # Yahoo Finance fundamentals
└── signal_filter.py       # Signal scan (standalone analysis)

data/                      # Local data
├── scanperfect.db         # SQLite DB (examples, setups, etc.)
├── pipeline_state.json    # Grinder run status
├── pipeline_logs.json     # Grinder log output
└── vetting/               # Vetting decision files

server.py                  # FastAPI (Railway deploy only — seed vault endpoints)
app/                       # HTML UI (LEGACY — replaced by scanperfect.py)
```

### Key docs:
- **`CLAUDE.md`** — operating rules for Claude agents working on this codebase (read first)
- **`PIPELINE_V2.md`** — authoritative pipeline architecture
- **`DATA_CONTRACT.md`** — schemas, cache file formats, data flow
- **`DEPENDENCY_MAP.md`** — per-component inputs, outputs, upstream callers, downstream consumers. Check before changing any component.
- **`SIGNAL_GRINDER.md`** — signal grinder + multi-run consensus pipeline spec
- **`REFINEMENT_GRINDER.md`** — refinement grinder spec
- **`CONSENSUS.md`** — active status tracker for the consensus pipeline
- **`NIGHTLY_REFRESH.md`** — nightly orchestration
- **`OHLCV_CACHE.md`** — OHLCV cache contract + tradable filter thresholds
- **`EXPRESSION_ENGINE_V2.md`** — expression library + .npz cache architecture
- **`FORWARD_PROP_SPEC.md`** — forward-prop incremental append engine (and why it's best-effort, not consensus-grade)
- **`LOCALIZE.md`** — local migration architecture + seed vault
- **`UI_FLOW.md`** — PySide6 flowchart UI design
- **`Code_Auditor.md`** — code auditor (`audit.sh` / `audit.py`) setup and usage
- **`BUGS.md`** — live bug tracker / session notes
- **`SHELVED.md`** — list of shelved / legacy scripts (reference only)
- **`ta_knowledge.md`** — TA concepts reference
- **`pcf.md`** — TC2000 PCF language reference
- **`ENTRY_GRINDER.md`** — entry grinder (deferred — see banner)

Deferred / archived on 2026-04-11 (in `archive/shelved_docs/`):
- `archive/shelved_docs/EV_GRINDER.md` — EV grinder spec (deferred to "live EV ranked watchlist" build)
- `archive/shelved_docs/PROFIT_GRINDER.md` — profit grinder spec (same)
- `archive/shelved_docs/ANALYSIS_SYSTEM.md` — v1 conceptual overview, superseded by `PIPELINE_V2.md`
- `archive/shelved_docs/HANDOFF_PARALLELIZATION.md` — superseded by `UNIVERSE_EXPANSION.md` Phase 2

---

## CODE AUDITOR

`audit.py` — runs a separate Claude Code instance (`claude -p`) that evaluates every code change against spec docs and project-wide rules. Checks four criteria: purpose, spec compliance, regression safety, and code quality. Returns PASS or FAIL with evidence.

Git hooks (local, not in repo):
- **post-commit** — fires on every local commit (Claude Code workflow)
- **post-merge** — fires on every `git pull`. Auto-reverts on FAIL. Paste failure output into Claude chat to fix.

The auditor maps changed files to their spec docs automatically (e.g. `ev_grinder.py` → `EV_GRINDER.md`). Project-wide rules (OHLCV uses EODHD bulk + yfinance gap fill, ProcessPoolExecutor only, del+gc patterns, etc.) are hardcoded in the auditor prompt.

---

## SETUP TYPES

| Setup | Status | Examples | Phase 2 Result |
|-------|--------|----------|----------------|
| **DTSS** | Phase 2+3 complete, Phase 4 done | 73 | 182 conditions, 78% WR, 6.4 ADR median |
| **3-4DB** | Examples loaded | 16 | Not yet ground |
| **HTF** | Bootstrap grind complete | 32 | 66 conds, 545 signals (5yr, peak_target=3) |
| **BF** | Bootstrap grind complete | 45 | 59 conds, 530 signals (5yr, peak_target=3) |
| **BASE** | Bootstrap grind complete | 42 | 78 conds, 646 signals (5yr, peak_target=3) |

---

## SUBSCRIPTIONS / TOOLS

- Claude Max — this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — seed vault / file mirror only
- Discord — trading community
- PySide6 — desktop UI framework (installed, in use)
- EODHD — EOD Historical Data API ($19.99/mo). Universe management (exchange symbol list, IPOs, delistings), bulk splits detection, full historical backfill, bulk daily bars. Limits: 100K API calls/day (EOD = 1 call/ticker, bulk = 100 flat). 1,000 HTTP requests/min. Daily counter resets on first request after midnight GMT. Note: bulk endpoint for current day is incomplete until ~6-8 hours after close; yfinance fills the gap. API key in Claude memory.
- yfinance — Free. Same-day OHLCV bars (available immediately after close), fundamentals (Yahoo quoteSummary API), earnings dates (yf.Ticker). Unofficial scraper — rate limits apply, use batch downloads with pauses.
