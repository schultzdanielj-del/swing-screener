# TODO — UI & Pipeline Rebuild (2026-03-02)

## Current State

**Pipeline is working end-to-end:**
- Signal grind → signal exit grind → signal filter → chart vetting → example creation
- 36 DTSS examples (23 original + 14 from first vetting pass, minus 1 removed)
- 8 rejected signals in DB
- Expression cache: 4,119 tickers × 12,175 expressions (~50 GB)
- Ready for re-grind with expanded example set

**What needs building:** Unified frontend that replaces the current scattered pages (pipeline.html, vetting.html, index.html) with a single app.

---

## Frontend Architecture

### Top-Level Navigation

```
┌──────────────────────────────────────────────────────────┐
│  NIGHTLY REFRESH  │  SETUP ANALYSIS  │  DAILY WATCHLIST  │
└──────────────────────────────────────────────────────────┘
```

### 1. Nightly Refresh
Single page. Shows cache/data freshness status, run button.
- OHLCV append status (last run, rows added)
- Daily cache / 5yr cache / expression cache / matrix status
- One-click "Run Nightly" button → agent executes

### 2. Setup Analysis
Setup selector at top: **DTSS** | **3-4DB** (only two for now).

#### 2a. Setup Home (default view)
- Current best conditions (from latest pyramid result)
- Stats: n_examples, n_signals, peak/day, avg/day
- Setup description, anti-curve-fit score, ideal market conditions

#### 2b. Steps (sub-nav within a setup)

**Step 1: Examples**
- Gallery of validated examples + rejected signals
- Toggle charts on/off
- Add/remove examples
- Stats: count, date range, ticker diversity

**Step 2: Signal Grinder**
- Run → agent executes pyramid_grinder
- Live logs, result summary (conditions, signal count, peak/day)

**Step 3: Signal Exit Grinder**
- Run → agent executes signal_exit_grinder
- Result: best exit condition, median capture eff

**Step 4: Signal Filter**
- Run → agent executes signal_filter
- Result: N filtered signals, ADR distribution
- Auto-uploads to Railway when done

**Step 5: Chart Vetting + AI Review**
- Manual vetting UI (YES/NO/MAYBE)
- Live stats: vetted/total, yes/no/maybe
- **AI Review (final sub-step):**
  - `claude -p` via agent checks each YES/NO against examples + setup definition
  - Flags disagreements for human review
  - Must pass before re-grind

**Step 6: Exit Grinder**
- Choose single-stage or multi-stage
- Run → agent executes chosen grinder
- Keep/discard results

**Step 7: Market Grinder**
- Cluster outcomes vs market regime (SPY, VIX, breadth)
- Find optimal market conditions for entry

**Steps 2-7 independently runnable.**

### 3. Daily Watchlist (future, placeholder)
- Nightly scan → today's signals ranked by EV

---

## Build Plan

### Phase 1: Core UI Shell ⬜
- [ ] Single-page app with top nav (Nightly / Setup Analysis / Watchlist)
- [ ] Setup selector within Setup Analysis
- [ ] Sub-nav for steps 1-7
- [ ] Hash-based routing, dark theme

### Phase 2: Wire Existing Functionality ⬜
- [ ] Nightly page: status + run button
- [ ] Examples page: gallery + rejected list
- [ ] Steps 2-4: run/logs/results (reuse pipeline endpoints)
- [ ] Step 5: embed vetting UI
- [ ] Step 6: exit grinder with single/multi toggle

### Phase 3: Setup Home ⬜
- [ ] Latest pyramid result display
- [ ] Anti-curve-fit metrics
- [ ] Setup description, signal distribution chart

### Phase 4: AI Vetting Review ⬜
- [ ] `scripts/ai_vet_review.py` — loads decisions, builds prompts, calls `claude -p`
- [ ] Saves ai_verdict + reasoning per signal
- [ ] Pipeline step in agent
- [ ] Frontend: show agree/disagree, flag conflicts

### Phase 5: Market Grinder ⬜
- [ ] Step 7 implementation
- [ ] Blocked by: enough vetted examples with outcomes

### Phase 6: Daily Watchlist ⬜
- [ ] Blocked by: market grinder (need EV for ranking)

---

## Key Files

| File | Purpose |
|------|---------|
| `app/index.html` | Main frontend (will become unified app) |
| `app/pipeline.html` | Current pipeline dashboard (merge into unified) |
| `app/vetting.html` | Current vetting UI (embed in step 5) |
| `server.py` | Railway FastAPI backend |
| `local_runner/agent.py` | Desktop agent |
| `local_runner/pyramid_grinder.py` | Signal grinder |
| `scripts/signal_exit_grinder.py` | Signal exit grinder |
| `scripts/signal_filter.py` | Signal filter |
| `scripts/exit_grinder.py` | Trade exit grinder (shelved) |
| `scripts/multistage_exit_grinder.py` | Multi-stage exit |
| `ta_knowledge.md` | TA concepts, setup definitions |

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% of examples must pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
