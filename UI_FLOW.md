# ScanPerfect UI — Design Document

**Updated:** 2026-03-17

---

## Architecture

Native PySide6 desktop app (`scanperfect.py`). No browser, no server, no tabs.

The pipeline flowchart IS the interface. Each node expands in place to become its own workspace. Setup selector dropdown in the top bar.

---

## Design System

- **Font:** DM Sans (all text — titles, labels, card content, detail panels)
- **Background:** #000000
- **Card colors:** Each node type has a distinct deep saturated background color
  - Examples: deep red (#2a1215 → #3d1b20 gradient)
  - Vetting: deep orange (#2a1f0d → #3d2e15)
  - Scan Tuning: deep yellow (#2a2610 → #3d3818)
  - Grinds (Causative, Correlative, Optimal Management): deep blue (#0d1a2a → #15263d)
  - Summary: deep green (#0d2a1a → #153d26)
- **Borders:** 2.5px, color-matched to card type
- **Locked nodes:** Nearly invisible (#060606 bg, #111 border)
- **Text:** #E0E0E0 primary, #888 secondary, #555 muted

---

## Pipeline Flowchart — 7 Nodes, Two Loops

```
        ┌──────────────────────────────┐
        │         LOOP 1               │
  [Examples] ──→ [Causative Processing] ──→ [Vetting]
        ↑                                      │
        └──────── add examples ────────────────┘
                                               │
                                    [Correlative Targeting]
                                               │
        ┌──────────────────────────────────────┤
        │         LOOP 2                       │
  [Scan Tuning] ──→ [Optimal Management]       │
        ↑                    │                 │
        └── tweak · re-run ──┘        [Summary]
```

**DO nodes (left column, warm colors, rounded corners):**
- Examples — manage example library, progress bar (examples / winner clusters)
- Vetting — review winners, bank new examples (navigates to vetting workspace)
- Scan Tuning — quality score + WR threshold sliders (future)

**RUN nodes (right column, blue, sharp corners):**
- Causative Processing — Signal Grind → Exit Grind → Refinement Grind (one Run button)
- Correlative Targeting — EV grinder scoring
- Optimal Management — exit strategy optimization via SQN (future)

**Summary (right side, green):**
- Setup readiness overview (future)

---

## Unlock Progression

Everything starts locked except Examples. Gates:
- **Causative Processing:** ≥20 examples
- **Vetting:** Causative complete (refinement_*_cl*.json exists)
- **Correlative Targeting:** ≥60 examples AND Causative complete
- **Scan Tuning:** Correlative complete (ev_*.json exists)
- **Optimal Management:** Correlative complete
- **Summary:** Optimal Management complete (future)

---

## Card Interaction

**DO nodes** (Examples, Vetting, Scan Tuning): Click navigates to that workspace (will expand in place when workspace is built).

**RUN nodes** (Causative, Correlative, Optimal Management): Click expands the card in place with animation — grows wider AND taller, centers on screen. Shows:
- Sub-step progress badges (Causative has 3: Signal Grind / Exit Grind / Refinement)
- Metrics row: Status, Last Run, Duration, Setup
- Run / Stop / Clear Log buttons
- Real-time log viewer (QProcess stdout streaming)

Click again to collapse.

---

## Examples Card

Shows progress bar: `66 / 365` (examples / winner clusters from latest refinement_*_cl*.json).
Progress bar is thick with rounded ends, gradient fill in the card's red accent color.
Count shown inline next to the title.

---

## Functional Color (within workspaces, not the flowchart)

- Candle up/down: green (#4ade80) / red (#f87171)
- Yes/No verdicts: green / red
- Earnings dates: red (#EF4444)
- Entry marker: white
- Exit marker: amber (#E8A735)
