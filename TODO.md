# TODO — Swing Screener (2026-03-02)

## Current State

**Unified UI: ✅ BUILT**
- Single-page app at root URL, rail nav (Nightly / Setup Analysis / Watchlist)
- Setup selector: DTSS | 3-4DB
- Steps 1-6: Examples → Signal Grinder → Signal Filter → Chart Vetting → Exit Grinder → Market Grinder
- Vetting UI embedded with earnings overlay, auto-example creation, rejected signals DB
- Agent offline hint with click-to-copy terminal command

**Pipeline: ✅ WORKING END-TO-END**
- Signal grind + signal exit grind combined into one step (agent runs back-to-back)
- Signal filter → chart vetting → example creation/rejection
- 36 DTSS examples (23 original + 14 from vetting pass 1, minus 1 removed)
- 8 rejected signals in `rejected_signals` DB table
- Ready for re-grind with expanded example set

**Data:**
- Expression cache: 4,119 tickers × 12,175 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tradable tickers

---

## Architecture (as built)

### Frontend: `app/index.html` (unified SPA)

**Rail navigation:** Nightly Refresh | Setup Analysis | Daily Watchlist

**Setup Analysis steps:**
1. **Examples** — gallery with mini chart toggle, rejected signals list, stats
2. **Signal Grinder** — runs pyramid_grinder.py then signal_exit_grinder.py back-to-back
3. **Signal Filter** — dedup, exit condition, ADR floor, rank for vetting
4. **Chart Vetting** — full candlestick charts with EMAs/SMAs, earnings overlay (purple E markers), YES/NO/MAYBE verdicts, "Submit for Audit" button for AI review
5. **Exit Grinder** — single-stage vs multi-stage toggle, finds best exit for max MFE capture
6. **Market Grinder** — placeholder (clusters outcomes vs market regime)

**Key features:**
- Agent status dot (green=online, red=offline) with click-to-copy start command
- Live log streaming for all grinder steps
- Setup selector (DTSS / 3-4DB)
- Keyboard shortcuts in vetting (↑↓ nav, 1/2/3 for yes/maybe/no)

### Backend: `server.py`

**Vetting endpoints:**
- `GET /api/vetting/{setup}/signals` — filtered signals, excludes examples within ±5 days
- `GET /api/vetting/{setup}/ohlcv/{ticker}` — OHLCV centered on signal date
- `POST /api/vetting/{setup}/decide` — saves verdict, creates example (YES) or rejected_signal (NO)
- `GET /api/vetting/earnings/{ticker}` — Yahoo Finance earnings dates
- `GET /api/vetting/{setup}/rejected` — all rejected signals

**Pipeline endpoints:**
- `GET /api/pipeline/steps` — step states + vetting stats + agent status
- `POST /api/pipeline/run/{step_id}` — queue job for agent
- `POST /api/pipeline/stop` — stop running job
- `POST /api/pipeline/reset/{step_id}` — reset step state

**DB tables added this session:**
- `rejected_signals` (setup_type, ticker, signal_date)

### Agent: `local_runner/agent.py`

- Multi-command step support: signal_grind runs pyramid_grinder then signal_exit_grinder sequentially
- Streams logs back to Railway in batches of 20 lines
- Reports per-command progress headers for combined steps

### Old files (backed up, not served):
- `app/index_old.html` — original ScanPerfect frontend
- `app/pipeline_old.html` — standalone pipeline dashboard
- `app/vetting_old.html` — standalone vetting UI

---

## Pipeline Steps (server-side definition)

| Step ID | Name | Description |
|---------|------|-------------|
| `nightly` | Nightly Refresh | OHLCV append, cache rebuild, matrix rebuild |
| `signal_grind` | Signal Grinder | pyramid_grinder.py → signal_exit_grinder.py (combined) |
| `signal_filter` | Signal Filter | Dedup, exit condition, ADR floor, rank |
| `vetting` | Chart Vetting | Manual YES/NO/MAYBE + AI audit (manual step) |
| `exit_manage` | Exit Grinder | Single or multi-stage MFE capture optimization |
| `market_grind` | Market Grinder | Outcome clustering vs market regime |

---

## Build Plan — What's Left

### Phase 3: Setup Home ⬜
- [ ] Latest pyramid result display (conditions, signal count, peak/day)
- [ ] Anti-curve-fit metrics (examples vs conditions ratio, time spread)
- [ ] Setup description from ta_knowledge.md

### Phase 4: AI Vetting Review ⬜
- [ ] `scripts/ai_vet_review.py` — loads YES/NO decisions + existing examples + DTSS definition
- [ ] For each decision, builds prompt with OHLCV context
- [ ] Shells out to `claude -p` (uses Max subscription, $0 cost)
- [ ] Compares against established examples — flags disagreements
- [ ] Saves ai_verdict + reasoning per signal
- [ ] Wire into "Submit for Audit" button in vetting UI
- [ ] Agent runs as pipeline step after manual vetting

### Phase 5: Exit Grinder Backend ⬜
- [ ] Wire exit_manage step to correct scripts in agent
- [ ] Single-stage: scripts/signal_exit_grinder.py (already built)
- [ ] Multi-stage: scripts/multistage_exit_grinder.py (already built)
- [ ] Pass mode selection from frontend → agent job → script

### Phase 6: Market Grinder ⬜
- [ ] Step 6 implementation
- [ ] Blocked by: enough vetted examples with win/loss outcomes

### Phase 7: Daily Watchlist ⬜
- [ ] Nightly scan → today's signals ranked by EV
- [ ] Blocked by: market grinder

---

## Immediate Next Action

**Re-grind with 36 examples:**
1. Start agent locally: `python local_runner/agent.py`
2. Open UI → Setup Analysis → DTSS → Signal Grinder → RUN
3. Wait for grind + exit grind to complete (~10-15 min)
4. Signal Filter → RUN
5. Chart Vetting → vet new signals
6. Repeat until convergence

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% of examples must pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
