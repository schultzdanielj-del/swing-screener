# ScanPerfect — Project Document

**Last updated:** 2026-03-30
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener (branch `v2`)
**Railway:** https://web-production-e3025.up.railway.app (seed vault + file mirror only)

---

## WHAT THIS IS

Fully automated nightly swing trade screener. Screens ~4,000 tradable tickers, finds the handful matching validated setup patterns, ranks by expected value, produces a watchlist of the mathematically strongest positions. Qullamaggie-style, 3-day to multi-week holds.

Runs entirely on Dan's local machine (i5-12600k, 32GB RAM, Windows). $0 additional cost — Claude Max + TC2000 + Railway + GitHub.

---

## HOW IT WORKS

Upload example trades for a setup type. Automated brute-force methods discover which technical conditions separate those examples from the full market, gated by anti-curve-fit statistical validation. Signal conditions are correlated against broad market conditions and ticker attributes to score expected value. A daily watchlist ranks signals highest EV to lowest. Exit management is optimized by the same brute-force algorithm.

### Pipeline Phases
1. **Causative filtering** — Signal grind finds conditions where all examples pass but most of the market fails. Entry/exit grind classifies winners and losers. Refinement grind eliminates losing clusters. Can run in EDA mode (manual, fast) or Consensus mode (overnight, anti-curve-fit).
2. **Correlative scoring** — EV grinder tests every market condition and ticker attribute against the signal set. Signals aren't removed, they're ranked by predicted WR, MFE, and EV.
3. **Profit optimization** — Brute-forces TA-expression-based exit conditions to capture optimal MFE.
4. **Live watchlist** — Updated nightly. Signals ranked by historical EV.

### Principles
- **Anti curve-fit:** Consensus pipeline runs on real and permuted data. Only conditions scoring z > 3 survive.
- **Consistency:** Same conditions produce same signals at every pipeline stage.
- **Reproducibility:** Same inputs → same outputs. Caches are append-only.
- **Self-referencing:** All conditions normalized by each ticker's own characteristics (ADR, percentile ranks). Never absolute prices.

---

## ARCHITECTURE

```
scanperfect.py             # PySide6 desktop app — pipeline controls, vetting, charts
audit.sh                   # Code auditor — manual, dependency-aware (see Code_Auditor.md)

local_runner/
├── nightly.py             # 10-step nightly pipeline (4:30pm ET via Task Scheduler)
├── cache_builder.py       # OHLCV caches — daily/weekly/monthly pickles (yfinance)
├── expr_cache_builder.py  # Expression series cache — per-ticker .npz (~21 GB)
├── vectorized_cache_builder.py  # Alternative fast expr cache builder (same output)
├── market_cache_builder.py      # 266 market instrument expression series
├── matrix_builder.py      # Universe matrix — last bar of every ticker
├── pyramid_grinder.py     # Signal grind + cluster gathering + refinement grind
├── brute_expressions.py   # Expression library generator (~15,805 expressions)
├── spiderweb.py           # Beam search algorithm (pure compute)
├── agent.py               # Polling agent for Railway job queue
├── pipeline_agent.py      # Pipeline step executor (Railway jobs)
├── file_mirror.py         # Mirrors files to Railway backup
├── grind_uploader.py      # Uploads grind results to Railway v2 cycles
└── cache/                 # All local caches + grind output JSONs

scripts/
├── signal_exit_grinder.py # Exit condition from signal bar (cache-compatible)
├── signal_filter.py       # Full universe scan with locked conditions
├── entry_grinder.py       # Stop placement + entry timing
├── entry_candle_scorer.py # Entry candle similarity scoring
├── ev_grinder.py          # Correlative EV scoring (setup + market features)
├── ev_tree_scorer.py      # XGBoost alternative scorer (called by ev_grinder)
├── profit_grinder.py      # Exit expression optimizer
├── profit_grinder_2stage.py  # 2-stage helper (called by profit_grinder)
├── consensus_engine.py    # Cross-run consensus analysis
├── exit_grinder.py        # Trade management exit (entry-relative, separate from signal exit)
├── expression_engine.py   # Core expression computation engine
├── backtest_conditions.py # Series computation from ExpressionEngine
├── profiling_engine.py    # TA indicator library (pandas)
├── exit_expressions.py    # Exit expression library (~6,410 expressions)
├── exit_compute.py        # Exit expression engine (entry-relative)
├── lsp_detector_v2.py     # Liquidity/structure pivot detection
├── algo_line_detector.py  # H-/L+ trendline detection
├── seed_vault.py          # Backup/restore SQLite + JSONs to Railway
├── fetch_fundamentals.py  # Yahoo Finance fundamentals (sector, float)
├── fetch_universe.py      # NASDAQ ticker list fetch (Railway-side)
├── build_tradable.py      # Tradable universe table builder
├── local_db.py            # SQLite helper functions
├── bulk_mirror.py         # Bulk upload all JSONs to Railway
└── analysis_api.py        # Profiling/discovery orchestration (legacy)

data/
├── scanperfect.db         # SQLite — examples, setups, earnings, vetting
├── signal_exit_grind/     # Exit condition outputs
├── signal_filter/         # Filtered signal outputs
├── exit_grind/            # Trade management exit outputs
├── profit_grind/          # Profit grinder outputs
├── vetting/               # Vetting decision JSONs
├── pipeline_state.json    # Pipeline step statuses
└── pipeline_logs.json     # Pipeline log output

server.py                  # FastAPI — Railway deployment + local dev server
```

### Nightly Pipeline (10 steps, 4:30pm ET)
1. yfinance freshness check (gate)
2. Daily OHLCV cache append
3. Weekly OHLCV cache append
4. Monthly OHLCV cache append
5. Expression cache append (blocker — CPU intensive)
6. Universe matrix rebuild
7. Earnings dates refresh (via Railway)
8. Market cache append (266 instruments)
9. Fundamentals cache refresh
10. Seed vault backup to Railway

### Data Storage
- **Daily OHLCV:** Full available history, append-only pickle. Source of truth for all price data.
- **Weekly/Monthly OHLCV:** 10yr lookback, append-only pickles.
- **Expression cache:** ~15,805 expressions per ticker. Per-ticker .npz files (~21 GB total).
- **Market cache:** 266 broad market instruments with same expression set.
- **SQLite:** Examples, setups, vetting decisions, earnings dates, tradable universe.

---

## KEY DOCS

| Document | Purpose |
|----------|---------|
| `DEPENDENCY_MAP.md` | Per-component inputs, outputs, callers, consumers. **Check before changing any component.** |
| `DATA_CONTRACT.md` | Schemas, file formats, data flow rules |
| `Code_Auditor.md` | Auditor setup and usage |
| `PIPELINE_V2.md` | Pipeline architecture |
| `SIGNAL_GRINDER.md` | Signal + refinement grind spec |
| `ENTRY_GRINDER.md` | Entry/stop grind spec |
| `EV_GRINDER.md` | EV grinder spec |
| `PROFIT_GRINDER.md` | Profit grinder spec |
| `EXPRESSION_ENGINE_V2.md` | Expression cache + computation spec |
| `NIGHTLY_REFRESH.md` | Nightly pipeline spec |
| `LOCALIZE.md` | Local migration spec |
| `CONSENSUS_SPEC.md` | Consensus pipeline spec |
| `UI_FLOW.md` | PySide6 desktop app spec |
| `ANALYSIS_SYSTEM.md` | Repeatable process for building any setup |
| `ta_knowledge.md` | TA concepts reference — read before any TA work |
| `pcf.md` | TC2000 PCF language reference |

---

## CODE AUDITOR

`audit.sh` — manual code review tool. Run `./audit.sh` in Git Bash when ready. Uses Claude Code (`claude -p`) as a separate instance.

- **Manual trigger only** — no git hooks, no auto-fire
- **Batch diffing** — diffs from last audited commit to current HEAD
- **Dependency-aware** — reads DEPENDENCY_MAP.md, pulls downstream consumer files
- **Four criteria:** purpose, spec compliance, regression safety, code quality
- **On PASS:** advances bookmark to current commit
- **On FAIL:** bookmark stays put, re-audits same block on next run

See `Code_Auditor.md` for full details.

---

## SETUP TYPES

| Setup | Direction | Status |
|-------|-----------|--------|
| **DTSS** | Short | Phase 2-4 complete. Primary setup. |
| **BRKO** | Long | Phase 3 in progress. |
| **3-4DB** | Long | Examples loaded. Not yet ground. |
| **HTF** | — | Scaffolded. No examples. |

---

## SHELVED / LEGACY (do not modify or reference as active)

**Scripts:** `dartboard_grinder.py`, `hybrid_grinder.py`, `setup_refiner.py`, `outcome_grinder.py`, `outcome_engine.py`, `multistage_exit_grinder.py`, `classify_universe.py`, `condition_pruner.py`, `proximity_grinder.py`, `market_grinder.py`, `setup_grinder.py`, `pipeline_agent.py`, `run_nightly.py`

**Docs:** `DARTBOARD_DESIGN.md`, `MULTISTAGE_EXIT_GRINDER.md`

**UI:** `app/index.html`, `app/vetting.html` — legacy browser UI, replaced by `scanperfect.py`
