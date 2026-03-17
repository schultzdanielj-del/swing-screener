# Swing Screener Project — State Document

**Last updated:** 2026-03-17
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
8. **All data from local files.** SQLite (`data/scanperfect.db`), OHLCV pickle, local cache JSONs. Never Railway. Never yfinance.
9. **Break work into small tasks.** Update docs when finishing tasks that alter them.
10. **READ THE ACTUAL CODE before making claims.** Verify, don't guess.
11. **At session start:** Read the full codebase before proposing anything.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~4,000 tradable tickers nightly, finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## CURRENT STATE (2026-03-17)

### DTSS (Double Top Short Sell) — first setup:
- **Examples:** 66 (65 with valid scan bars in clusters, out of ~365 winner clusters)
- **Causative (Phase 2):** Complete — 87 signal conditions + 100 refinement conditions, 182 combined, 78% WR, median winner 6.4 ADR
- **Correlative (Phase 3):** Complete — EV Grinder inc 1-6 done. 1,816 pre / 1,940 post features. D1→D10 spread +45pp WR. RMSE 0.090
- **Profit (Phase 4):** Not started
- **Live Watchlist (Phase 5):** Not built

### Native Desktop UI (Phase 6):
- PySide6 app (`scanperfect.py`) — IN PROGRESS
- Pipeline flowchart with 7 color-coded nodes, two feedback loops, unlock progression
- Animated card expansion with Run/Stop/Log for grind nodes
- Replaces browser-based HTML UI (server.py + app/*.html)
- Next: expand DO nodes (Examples, Vetting) into full workspaces within the flowchart

---

## ARCHITECTURE

Everything runs locally on Dan's desktop (i5-12600k, 32GB RAM, Windows).

```
scanperfect.py             # PySide6 native desktop app (THE interface)

local_runner/              # Grinder system
├── nightly.py             # 9-step nightly pipeline (4:30pm ET)
├── pyramid_grinder.py     # Signal grind + refinement grind (--blackout)
├── cache_builder.py       # OHLCV caches (daily + 5yr)
├── expr_cache_builder.py  # Expression series cache (~21 GB)
├── market_cache_builder.py # 256 instrument expression series
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
- **`TODO.md`** — task list, pipeline status, immediate work items
- **`LOCALIZE.md`** — local migration (complete) + UI status
- **`UI_FLOW.md`** — PySide6 flowchart UI design
- **`PIPELINE_V2.md`** — pipeline architecture
- **`EV_GRINDER.md`** — EV grinder spec
- **`ANALYSIS_SYSTEM.md`** — repeatable process for building any setup
- **`DATA_CONTRACT.md`** — schema + data flow
- **`ta_knowledge.md`** — TA concepts reference
- **`pcf.md`** — TC2000 PCF language reference

---

## SETUP TYPES

| Setup | Status | Examples | Phase 2 Result |
|-------|--------|----------|----------------|
| **DTSS** | Phase 2+3 complete, Phase 6 UI in progress | 66 | 182 conditions, 78% WR, 6.4 ADR median |
| **3-4DB** | Examples loaded | 21 | Not yet ground |
| **HTF** | Scaffolded | None | — |

---

## SUBSCRIPTIONS / TOOLS

- Claude Max — this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — seed vault / file mirror only
- Discord — trading community
- PySide6 — desktop UI framework (installed, in use)
