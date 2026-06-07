# NARRATIVE_MAP

New dashboard component (a 5th Themes sub-view — Chart / Heatmap / History / Rotation / Map)
+ the macrotheme tracker built on top of it. This doc also captures the full research session
(2026-06-06) that motivated it —
findings, methods, and what was ruled out. Findings are a dated point-in-time record,
not live system state.

## Purpose

Arrange every theme by **where it sits in the market story** (the AI-buildout chain →
outputs → adjacents → off-narrative noise), colored by current strength, so leadership
can be read in *story-space* instead of only strength-space (the existing Rotation/RRG
view). Then aggregate the story-zones into **macrothemes** and track money pouring into
a macrotheme vs distributing out of it. Goal = a fast filter: is the strength I'm
seeing **connected** (chase) or an **orphan** (noise)?

## Design — categorization + money-flow (settled 2026-06-06; not yet built into the live dashboard)

The map's categorization and money-flow math are **designed and the decisions are settled** (Dan
signed off on the calls below). A first prototype was built in a git worktree that turned out to
sit on a **stale copy of `theme_dashboard.py` (~2,000 lines behind the live file, missing the
whole sub-view system)** — so the prototype is thrown out and rebuilt against the **live**
`theme_dashboard.py`. What carries forward is the design, not that code:

**The map's vocabulary — three new data dicts in `theme_map.py`** (hand-edited like
`THEME_NARRATIVES`; additive — no existing consumer touched):
- `MACROTHEMES` — four macrothemes: **Buildout** (the AI build) · **Output** (what AI enables) ·
  **Adjacent** (real narratives, not the build, not noise) · **Noise** (everything else).
- `NARRATIVE_ZONES` — nine story-zones under the macros, each with a label + parent macro +
  display order. Buildout keeps its chain-walk: **Hub** (AI compute / datacenters) → **AI
  infrastructure** (chips, optics, memory, networking) → **Power** (nuclear, grid, storage,
  cooling) → **Raw materials** (critical minerals, metals). Output = **Frontier** (explosive:
  robotics, space, autonomy, quantum, AI apps, defense-tech, biotech) + **Steady grinders** (cyber,
  cloud/devops software). Adjacent = **Adjacent** (solar, EV supply chain, metals hedge, reshoring)
  + **Crypto**. Noise = off-narrative quarantine band.
- `THEME_CHAIN_POSITION` — every theme → its zone. Author-assigned; Dan does not hand-classify.
  **Must cover every theme in the live `THEMES`** — reconcile any themes added since.
- `TICKER_ZONE_OVERRIDE` — per-ticker zone fixes for the map only, for tickers a cross-listing
  would misroute. Each entry carries an inline reason.

**The money-flow math** (reuses the dashboard's existing equal-weight-composite + RS-vs-universe
machinery — the same `build_composite` and benchmark the themes already use):
- Every ticker → exactly one (macro, zone), **deduped by narrative priority** (buildout > output >
  adjacent > noise) with `TICKER_ZONE_OVERRIDE` applied — no double-counting across macros.
- Equal-weight composite per macrotheme + per Buildout chain-layer + the crypto cluster, each
  scored for RS vs the equal-weight universe over 5 / 20 / 65-day windows. Positive + rising =
  money pouring **in**; rolling over while another climbs = **distributing out**.
- One node per theme for the map (its zone, members, RS-vs-universe tint, narrative line).

**Settled categorization calls — do NOT re-litigate (these are Dan's):**
- **Narrative > adjacent** for a cross-listed ticker (a name in both a buildout theme and an
  adjacent theme counts as buildout).
- **Crypto miners that genuinely repurposed to AI datacenters = Buildout/Hub; miners with only HPC
  *ambition* stay in Crypto.**
- **TSLA = Output** (robotaxi / Optimus / FSD = AI application, not compute-build).
- **Metals / precious-metals miners = Adjacent** (hedge vs the narrative / inflation, not an AI input).
- **Biotech = Output/Frontier** (the bet is AI raising drug-success rates + pipeline size).
- **Solar = Adjacent** (joined the power leg 2025-26 but policy-driven, own engine).

## Updates — decided during the build (2026-06-07)

These refine / override the design text above; they are Dan's calls, settled while building.

- **Benchmark = SPY, not the equal-weight universe.** The money-flow math above said RS-vs-universe;
  that was a display-convenience assumption, never robustness-tested. Every script in the 2026-06-06
  research benchmarks vs SPY, and the one head-to-head that exists (`rs_elbow_probe.py`: SPY vs
  equal-weight cohort under a window/smoothing grid) kept **SPY** (lowest elbow-date jitter). So the
  Map's strength tint + money-flow use **RS vs SPY** (simple cumulative-return ratio, theme return
  minus SPY, over 5/20/65d). The other sub-views keep their universe yardstick; the Map's colours
  therefore won't match the Heatmap's, which is the correct disagreement.
- **Output is ONE group ("What AI Enables").** The Frontier vs Steady-grinders split is dropped:
  explosive-vs-steady is high-ADR vs low-ADR — a *volatility* axis, not a narrative one. The only
  narrative line inside Output (AI-native vs own-engine-with-tailwind) wasn't worth a sub-band.
- **Crypto routing is by co-movement, not press releases** (the chart is the tell; `scripts/
  crypto_comovement.py` + `crypto_rip_check.py`). Confirmed under the 2026-06 crypto drawdown (a
  clean stress test): names that rip as AI infra while Bitcoin bleeds → **Hub**; names that still
  track Bitcoin day-to-day but rip → **straddle Hub + Crypto** (`TICKER_ZONE_OVERRIDE` list):
  **MARA, CLSK, HIVE, GLXY, BTBT**. The `crypto_miners` theme → Hub. The **Crypto** zone is what's
  left: exchanges + treasuries.
- **Four orphan-SaaS → Noise.** A co-movement audit of all themes (`scripts/theme_placement_audit.py`)
  found `data_analytics_terminals`, `productivity_saas`, `vertical_saas`, `ad_tech_marketing` have
  near-zero-to-negative correlation with the AI core *and* weak RS — the AI-disrupted SaaS the
  research calls noise. (Cyber / Cloud / DevOps / AI-Apps are weak links but stay in Output.)
- **The audit metric must be market-neutral.** `corrAI − corrSPY` false-flags any big market name
  (Defense, MAG7, Robotics read 0.6–0.8 corr to the AI core but get flagged because they also track
  SPY). Raw `corrAI` is the honest read; the permanent drift-alarm strips SPY beta first.
- **Drift-alarm.** Placements are hand-set and drift as companies pivot (miners→datacenters),
  clustering around earnings. Rather than a calendar chore, the co-movement audit is **baked into the
  build** to flag any theme/ticker whose tape has wandered from its filed zone. Structure stays
  stable; strength/flow recompute every build; only genuine pivots need a human, and the alarm
  surfaces those.
- **Renderer (still being iterated).** Data engine done; the *drawing* has churned: tiles →
  rejected → stacked bands → rejected (read as a heatmap) → a **node-link graph** (zone hubs pinned
  into the backbone, theme bubbles sized by member count + tinted vs SPY, force-relaxed; "AI enables"
  arc + dashed Crypto↔Hub straddle edge). The graph layout is **not yet usable** (clutter, labels,
  the 30-bubble Noise blob, hub positions) and is the focus of the next session. `MAP_DATA` is stable;
  this is a `renderMap()` draw-layer rework. Also fixed in passing: the filter panel's theme rows
  dropped their dominant-sector chip, which had been collapsing long-sector theme names to invisible.

## Pending build — the Map sub-view

A **5th Themes sub-view**: the switcher becomes Chart / Heatmap / History / Rotation / **Map**,
persisted in `localStorage` like the others. Built **inside the live `theme_dashboard.py`**, the
exact way the existing sub-views are — there is **no separate render module**.

**How it slots in (mirrors Heatmap / Rotation, verified against the live file):**
- Add a `data-tv="map"` button to the `.rm-view-btns` switcher; allow `'map'` in `setThemesView`.
- Emit a `.map-page` full-width container (sibling of `.heatmap-page` / `.rotation-page`); show it
  when `themesView === 'map'` via the same CSS mechanism the other pages use.
- Add a `renderMap()` that paints from a Python-injected `MAP_DATA` global, called from the
  `themesSub` dispatch alongside `renderHeatmap()` / `renderHistory()` / `renderRotation()`.
- Compute `MAP_DATA` in `build_dashboard` next to where `HEATMAP_DATA` / `ROTATION_DATA` are built.

**Layout:** zones as labeled regions (Buildout chain top-to-bottom in walk order; then Output's
Frontier + Grinders, Adjacent, Crypto, Noise as a quarantine band). Each theme = a node in its
zone, tinted by its RS vs the universe. Each macrotheme region shows its strength + in/out flow.
**Click a theme → its member thumbnails** by cloning the theme `<section>`'s `.member-grid` — the
exact pattern `hmOpenExpand()` uses in the Heatmap view. Hover → its narrative line. Uses the
dashboard's own tokens (pure-black, gray-gradient chrome, cyan `#4dd0ff` / gold `#ffcc00`, green
`#1eff1e` / red `#ff3030`, Consolas/Segoe, sharp corners). Statistical only — no trade commentary
or signals.

**Post-mortem of the failed first attempt (do NOT repeat):**
- It was built in a worktree whose `theme_dashboard.py` was ~2,000 lines behind the live file and
  missing the sub-view system, and the live file was never diffed. **Build against the live file —
  or diff worktree-vs-live before writing a line.** Abandon that worktree; do not build on it.
- The first renderer (`narrative_map_view.py`, a standalone module of crude ad-hoc flexbox chips)
  is scrap. The real build is a `renderMap()` inside `theme_dashboard.py` styled with the tokens.
- Don't verify by rendering the full ~55MB dashboard + headless screenshots — open the real
  dashboard HTML on Dan's desktop and eyeball the Map sub-view.

---

## Research findings — 2026-06-06 session

### 1. The x-ADR cycle model (Dan's two-phase bull cycle), reconstructed + checked
- **Extension oscillator = distance from the 50SMA in ADRs, NOT raw %.** `ADR% = 20-period mean of (high/low − 1) × 100`; `x-ADR = (ext% above 50SMA) / ADR%`. Using raw % mis-dates the peak (volatility-driven); the ADR version is the right one and matches Dan's TC2000 "X ADR to 50sma" panel.
- **The cycle:** Phase 1 = tight thrust, rising x-ADR to a global max. A **lower pivot high** on x-ADR → **range-expansion (big red candle) soon** → reset, *but the bull continues*. Phase 2 = buy 21EMA dips; x-ADR makes **lower highs while price makes higher highs** (a predictable downsloping trendline). A steeper inner line breaking up *before* the chop zone = a **second wind** (capped by the original line). A **reset below the 50SMA** (x-ADR < 0) starts a new cycle.
- **2025 markers (validated):** cycle start ~5/1; **x-ADR peak 7/3 (8.06 ADR)**; range-expansion reset 8/1 (~4 weeks after the peak, NOT 10 — the % version's error); second-wind takeoffs **9/8–9/12** (metals/minerals/quantum — matched Dan's "9/11" recall); x-ADR sub-zero ~11/17.
- **2026:** cycle start **4/8**; **x-ADR peak 5/14 (10.71 ADR**, stronger than 2025); as of 6/5 ~4.0 and rolling over — **past the peak, no clean reset candle yet.**

### 2. Theme lifecycle labels (deterministic — the rules to keep)
- **START = RS chord-elbow** — the max-distance-from-chord point on the theme composite's RS-vs-SPY line. Validated: 2025 take-offs ordered correctly; the 9/8–12 second-wind cluster matched Dan's memory.
- **END = the highest close before the first 10/20-SMA bear cross after the price peak.** The 10/20 bear cross only *confirms* the top happened; the label is the peak itself (the cross lags the top by a **median ~17%**, so it's a confirmer, not an exit).
- Don't score exit "lag" in days — the metric that matters is **profit given back**. (Lesson logged after an EMA21-exit mistake.)

### 3. Fake themes / the fizzle zone
- **"Fake" = emerged then died fast** (short lifespan from take-off to 10/20 bear cross). **2025: only 2** over the full 6-month cycle (durable bull). **2026: 6 already in ~2 months** (Gaming, Real Estate, E-commerce, Fintech, Energy Drinks, Crypto Platforms) — all consumer/financial/spec.
- **Around a range-expansion reset, ~66% of themes flash strength and ~70% of those fail to become durable** — the head-fake zone. A theme can flash-and-fizzle, then take off for real later (Quantum: false flash 8/15 → real launch 9/10).

### 4. Leadership map (2023→now) + the supply-chain rotation
- **One supercluster (AI buildout + crypto-infra + power + hard assets) led the entire 3.5 years.** Crypto Miners = #1 total RS; Bitcoin Treasury / crypto in the top-5 of *almost every* half-year (the most persistent leader).
- **Leadership walked DOWN the chain over time:** AI software/compute (2023) → infra / networking / optics (2023–24) → power / nuclear / grid (2024) → materials / critical minerals (2025) → then out to applications (robotics, space).
- **Soldiers lead, not generals** — MAG7 in the top-5 only once (2024 H1).
- **GLP-1 = proof of the taxonomy bias:** a #2-in-the-market theme (2024 H1, +115 RS) is invisible on the map because it's buried in Biotech. A one-leg wonder — topped 2024 H2, dead since.

### 5. What we RULED OUT (do not use)
- **"Defensives rotate in after the x-ADR peak ⇒ reset coming" FAILED validation.** Across 2023–2024's five x-ADR peaks, defensives rotated in after only **1 of 5** (the one before the Aug-2024 crash). The 2025 instance was a near-one-off. **Do not treat defensive rotation as a reliable timing signal.**
- **Last-rung check:** the chain has **no clean lower rung** — gas/water/datacenter-land are not leading; critical minerals are fading; the strongest post-peak movers are **off-chain scatter** (shipping, banks). The supply-chain walk has hit its floor and rolled over; leadership is **scattering**. (The *observation* is solid; the "reset imminent" *interpretation* is the unreliable part.)
- **Forward estimates / fundamentals as a "credibility" layer: rejected** — estimate *levels* are already priced in; current fundamentals are backwards for the secular themes (would rank specialty-retail above quantum). The credibility signal that works is **co-movement with the live cluster**, not fundamentals.

### 6. The filtering model (what the whole session was building toward)
- **Master gate — connected vs orphan:** a theme is *connected* if it moves with the live cluster OR sits on the chain OR is an application branch. *Orphan* = pops on its own → **noise, skip no matter how clean the chart.** This one test screens ~80% of the noise.
- **Aggression dial (for connected themes):** 🟢 fresh + chain-layer/frontier/grinder = chase hard · 🟡 post-reset flash or late re-test = demand more confirmation · 🔴 already topped + slept = skip even if connected (crypto is the one serial-reloader exception).
- **Always-noise:** orphans, defensives/safety, the "everything scattering" pops (health + banks + shipping together), slept themes, AI-disrupted SaaS bouncing **below the 200** (e.g. MDB/WDAY cohort bounces).
- **Connected-but-broken (below 200):** MSFT/ORCL pass the gate but a flag-pop below the 200 is a flash until the **200 reclaim**; **MSFT's 200 is the read on the whole AI core's health.**
- **Hunt the soldiers, not the generals.**

### 7. The narrative web (the structure the map encodes)
- **Hub = AI compute.** Input chain = infra → power → materials. Output = frontier (robotics, space, autonomy, quantum, AI apps, biotech-AI-drug-discovery) + steady grinders (cyber, cloud/infra software, dev tools). Adjacent/half-in = solar (power leg), cyber, comms/SaaS-infra. Off-narrative = consumer, AI-disrupted SaaS, defensives, banks, shipping.
- **Output side has two ends:** *pure AI-applications* (only matter because of AI — robotics/space — explosive, story-driven) vs *AI-adjacent with their own engine* (cyber, software — steady grind).
- **Place software by relationship-to-AI, not XLK sub-sector:** DDOG = monitors the build (beneficiary) · FTNT/TENB/TWLO = half-in grinders · GTLB/TEAM = tailwind-or-disrupted (TEAM riskiest). EV makers = off-hub laggard; Solar = half-in (joined the power leg in 2025–26).

## Details you need to know
- The session's analysis scripts live in `scripts/` (e.g. `theme_leadership_map.py`, `validate_cycle_pattern.py`, `fake_themes.py`, `last_rung_check.py`, `narrative_map.py` [static concept diagram], `spy_xadr_2025.py`). They are ad-hoc research, not pipeline components.
- The static concept diagram is at `research/narrative_map.png` — the interactive build supersedes it.
- Theme composites, RS-vs-equal-weight-universe, and the RRG rotation data already exist in `theme_dashboard.py` and are reused, not rebuilt.

## Caveats & boundaries
- **Survivorship + taxonomy bias:** themes are built from *current* members and the *2026* taxonomy. Trust **rotation timing**, not "what dominated" (GLP-1 missing is the proof). The macrotheme tracker inherits this.
- **N=1:** most cycle findings are 2025 + in-progress 2026; the one cross-cycle validation (defensives) **failed.** Treat the cycle model as a lens, not a law.
- **No discretionary TA** ([[feedback_no_discretionary_ta]]): deterministic labels + correlations only; all trade decisions are Dan's.
- **Open the rendered HTML on Dan's desktop** — he's on Chrome Remote Desktop, so `Start-Process` surfaces on the screen he's viewing; SendUserFile chips are not clickable in his view. See [[user_workflow_remote_desktop]].
