# CLAUDE.md — ScanPerfect Project Rules

## WORKFLOW
Session start: curl SWING_SCREENER_PROJECT.md and DEPENDENCY_MAP.md from the repo (never from project files — there are none). Read the .md spec of any component before working on it. Break into smallest testable increments. Read the spec, list every requirement as a numbered checklist. Propose with checklist visible, Dan vets, gives explicit go-ahead. No code without "go/yes/do it." After code, reconcile: spec says X, code does X in function Y. If sandbox passes, say ready to push.

## WORKFLOW BEHAVIORS
FULL STOP after presenting results/plans — no chaining into next steps, creating files, or running code. If you need credentials or access, STOP and ask. Never dump large data (CSV, JSON) into context — process via scripts. When Dan asks "can you do X" — honest answer with constraints and tradeoffs FIRST. If first attempt fails, STOP and explain before trying again.

## DEPENDENCY AWARENESS
Before changing any component, check DEPENDENCY_MAP.md for downstream consumers and read those files too. Before pushing, state what downstream consumers exist and confirm they won't break. The code auditor (audit.sh) runs on Dan's machine as a safety net — it is not a replacement for checking dependencies before coding.

## CODEBASE RULES
Read ta_knowledge.md before ANY TA work. Read pcf.md only when writing PCF. Read the .md spec of any component you are working on before any work on it. Check DATA_CONTRACT.md when changing file formats, schemas, or data flow.

## PERMANENT FACTS
Repo: schultzdanielj-del/swing-screener, branch v2. Railway: https://web-production-e3025.up.railway.app (seed vault + file mirror only). Large file push: python3 urllib.request with base64. Read private repo files: curl with Accept: application/vnd.github.v3.raw against API endpoint.

## CACHE / DATA RULES
- Expression cache: C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\expr_series\
- OHLCV cache: C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\
- Before any multi-ticker job: print the cache path, count tickers, confirm ~11,200+ before proceeding. STOP if count is wrong.
- Railway is NEVER a data source for OHLCV or expression cache. Local only. If a command fetches from Railway, it is the wrong command.
- OHLCV source: EODHD bulk + yfinance gap fill. Never Railway.
- Expected ticker counts: OHLCV daily ~11,523, expr cache ~11,201.
- Never trigger a full expression cache rebuild without auditing every change to the compute path since the last known-good rebuild.
- NEVER create junction links, symlinks, hard links, or any filesystem link that points to a live cache or data directory. Worktrees are isolated — keep them that way. If a script needs cache data, pass the real path as a read-only argument.
- NEVER run rmdir, rd, del, or git worktree remove on any path that touches cache or data directories. Check for links first.
- NEVER run --force on any cache builder (cache_builder.py, expr_cache_builder.py, market_cache_builder.py) without presenting what it will overwrite, the target path, and the ticker count. Wait for explicit go-ahead.
- Before any command that writes files, verify the resolved target path. If it resolves outside the worktree to the main repo's local_runner/cache/, STOP immediately.

## COMMUNICATION
Dan is sole developer, doesn't write code, can't read code fluently. No sycophantic language. Honest assessments over optimistic ones. Dan is direct, corrects errors immediately. Pseudocode and plain language only, no raw code blocks. Names OK but must be simple enough for Dan to spot problems.
