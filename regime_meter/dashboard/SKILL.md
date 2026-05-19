---
name: scanperfect-design
description: Use this skill to generate well-branded interfaces and assets for ScanPerfect, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping quantitative trading dashboards in the Bloomberg/TradingView-density visual idiom.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Quick orientation

1. **`colors_and_type.css`** is the source of truth for tokens — link it from every HTML artifact and reference variables (`var(--bg-panel)`, `var(--up)`, `var(--font-mono)`) rather than hard-coding values.
2. **`Regime Meter.html`** is the live reference prototype — a daily pre-open dashboard rendering 11 sector "cones" of similar-day forward log-return paths. Inspect it to understand what production output looks like.
3. **`Cone.jsx`** is the visualization primitive — density heatmap + median overlay over forward paths. Reusable for any "many-paths over time" chart.
4. **`assets/`** holds the logotype, mark, favicon, and reference chart imagery. Don't draw new SVGs; copy from here.

## Visual hard rules

- Dark only. `--bg-canvas` (`#0a0d12`) on the body.
- Mono font for every number — wrap prices/percentages/volumes/times in `<span class="num">`.
- 6px radius on panels, 4px on controls, 0 on tables and ticker tiles.
- 1px hairline borders are the primary container affordance — not drop shadows.
- No gradients in panels, no emoji, no animations beyond 120ms hover transitions.
- Up = green `#26d07c`, Down = red `#ff4d5e`, Brand accent = amber `#ffb648`.

## Voice

Terse, imperative, numbers-first. "Scan returned 47 matches." not "We found 47 matches!" Real minus sign `−` not hyphen. ISO dates. UPPER case for column headers and status pills.
