# ScanPerfect — Swing Screener (v2)

Expression-library-based swing trade signal grinder, scanner, and live watchlist. Runs locally on Windows/Linux; a desktop PySide6 app drives the pipeline. Railway is a seed-vault/file-mirror backup only.

## Entry points for new sessions

- **`CLAUDE.md`** — Operating rules for Claude agents working on this codebase. Read first.
- **`SWING_SCREENER_PROJECT.md`** — Project-level status and goals.
- **`PIPELINE_V2.md`** — Authoritative architecture overview of the full analysis pipeline.
- **`DEPENDENCY_MAP.md`** — What every `.py` file reads, writes, and is consumed by. Read before changing any component.
- **`DATA_CONTRACT.md`** — File formats, schemas, and data flow rules.

## Component specs

- `SIGNAL_GRINDER.md` — Signal grinder + multi-run consensus pipeline
- `REFINEMENT_GRINDER.md` — Refinement grinder (cluster-aware loser elimination)
- `CONSENSUS.md` — Active status tracker for the consensus pipeline build
- `NIGHTLY_REFRESH.md` — Nightly pipeline orchestration
- `OHLCV_CACHE.md` — Daily/weekly/monthly OHLCV cache contract
- `EXPRESSION_ENGINE_V2.md` — Expression library + .npz cache architecture
- `FORWARD_PROP_SPEC.md` — Forward-propagation incremental append engine
- `ENTRY_GRINDER.md` — Entry/stop optimization (deferred — see banner inside)

## Reference

- `ta_knowledge.md` — TA concepts and indicator formulas
- `pcf.md` — TC2000 PCF syntax reference
- `BUGS.md` — Known issues and session notes
- `SHELVED.md` — Abandoned scripts kept for reference only
