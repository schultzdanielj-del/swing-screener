# ScanPerfect Design System

> Solo-built quantitative swing trading toolkit.
> Information-dense desktop dashboards in dark themes — functional, no-fluff, Bloomberg/TradingView density rather than consumer-app whitespace.

---

## ⚠️ Source materials

This system was built **from a verbal description only** — no codebase, Figma, or screenshots were attached. Everything here is a directional proposal that should be validated against the real product. Concrete things to confirm with the maintainer:

- Actual product name and logotype (currently using a placeholder wordmark)
- Real color values (we picked a palette anchored in Bloomberg/TradingView conventions)
- Whether existing screens already use specific fonts/icons we should match
- The real surface list (we assumed a single desktop dashboard app)

If a codebase or Figma exists, re-attach via **Import → Local codebase / GitHub / Figma** and we'll align this system to the real source of truth.

---

## What ScanPerfect is

A solo-built toolkit for quantitative **swing traders** — people holding positions for days-to-weeks, scanning for setups, ranking ideas, and monitoring open risk. The product is desktop-first and information-dense: many panels visible at once, fast keyboard navigation, dense tables, embedded charts, and live numbers. The user is technical and self-directed; they don't want hand-holding, marketing copy, or animations that delay information.

Cultural references the design is anchored to:
- **Bloomberg Terminal** — keyboard-driven, monospace-heavy, function-codes, amber accent.
- **TradingView** — chart-first, dark by default, dense watchlists, "screener" patterns.
- **Koyfin / Trade Ideas / Finviz Elite** — desktop screener density.

The design system rejects: consumer-app whitespace, soft pastel palettes, big illustrations, emoji, and gradient-heavy hero sections.

---

## File index

```
README.md                  — this file (overview + foundations + index)
SKILL.md                   — agent skill entry point (Claude Code compatible)
colors_and_type.css        — CSS variables for color + type tokens (the source of truth)

Regime Meter.html          — the LIVE PROTOTYPE — daily pre-open sector cone dashboard
Cone.jsx                   — density-heatmap + median-overlay forward-path viz
Dashboard.jsx              — Regime Meter header + grid layout + tweaks wiring
regime-meter.css           — Regime Meter-specific layout styles
tweaks-panel.jsx           — in-page tweaks panel (host-managed persistence)

data/
  regime_meter_today.json  — sample input data (1 day · 11 sectors · 30 neighbors)

assets/
  logo.svg                 — wordmark
  mark.svg                 — square mark
  favicon.svg              — 32×32 favicon
  chart-sample.svg         — reference candlestick chart imagery

preview/                   — design-system cards (one concept per HTML file)
  brand-logo.html, brand-voice.html
  colors-{surfaces,foreground,signal,borders}.html
  type-{sans,mono,roles,fn}.html
  spacing-{scale,radii,shadows}.html
  components-{buttons,inputs,pills,table,tile,chart,menus}.html

ui_kits/scanperfect-desktop/
  ui-kit.css               — shared component CSS (panels, tables, pills, tabs, inputs…)
  Primitives.jsx           — React primitives (Panel, Pill, Num, Icon, Tabs, Button)
  README.md                — kit notes
```

> The `ui_kits/scanperfect-desktop/` folder holds the foundational component layer. The Regime Meter at the project root pulls from those tokens and CSS classes directly — open it in the preview to see them in use.

---

## CONTENT FUNDAMENTALS

**Voice.** Terse, technical, and verb-first. ScanPerfect speaks to a peer who already knows what RSI, ADR, and a 50-day MA are. No onboarding fluff, no "let's get started," no exclamation marks. Numbers and tickers are the content.

**Casing.** Sentence case for UI labels (`New scan`, `Save preset`). UPPER-CASE for column headers in tables and for status pills (`HALTED`, `OPEN`, `STOP HIT`). Tickers are always uppercase (`AAPL`, `NVDA`). Function codes that mirror Bloomberg conventions are uppercase + monospace (`HELP`, `GO`, `EQS`).

**Person.** Mostly imperative (`Set stop`, `Add to watchlist`). Never "we" — there is no "we", it's a solo product. Avoid "you" except in confirmation prompts (`Delete preset? You can't undo this.`).

**Numbers.**
- Prices in mono, right-aligned, fixed decimal: `184.27`, never `184.270` or `184.3`.
- Percentages always signed: `+2.84%` / `−1.12%` (use real minus `−`, not hyphen).
- Big numbers compacted: `$2.4B`, `12.7M shares`.
- Times are 24h + zone abbrev: `14:32 ET`. Dates: `2026-05-18` (ISO).

**Status language.**
- Long / Short / Flat
- Live / Paper
- Triggered / Pending / Cancelled
- Bullish / Bearish / Neutral / Mixed

**Tone examples.**
- ✅ "Scan returned 47 matches. 12 above ADR avg."
- ✅ "Stop set at 178.40. Risk: $312."
- ❌ "🎉 Awesome! We found 47 great matches for you!"
- ❌ "Looks like the market's a little choppy today!"

**Emoji.** Not used in product UI. The only exception is the favicon/app icon.

---

## VISUAL FOUNDATIONS

### Color philosophy
A near-black canvas with two darker panel tiers, off-white type, and three signal colors: **amber** (brand + warnings), **green** (up / long / good), **red** (down / short / bad). One supporting blue for info/links. No purples, no teals, no gradients except subtle 1-stop "protection" fades behind sticky table headers.

### Typography
- **Geist Sans** — UI labels, button text, body, headings. Set tight (`letter-spacing: -0.01em` on headings).
- **JetBrains Mono** — every number that represents a price, percentage, volume, time, P/L, or ticker. Also used for keyboard shortcuts and function codes.
- Hierarchy is mostly weight + size, not color. We rely on tabular numerals (`font-variant-numeric: tabular-nums`) everywhere mono numbers appear.

> ⚠️ **Font substitution flag** — We're using Geist Sans + JetBrains Mono from Google Fonts as the closest free match to a "terminal-but-modern" pairing. If you have a real font choice (Söhne, Berkeley Mono, etc.), swap the `@import` in `colors_and_type.css`.

### Spacing
4px base unit. Layouts are tight: most panel padding is 8 or 12px, gaps inside data rows are 4 or 6px. Whitespace is earned, not given.

### Backgrounds
**Solid colors only.** No gradients in panels, no background photography, no patterns. The one exception is a 1px-tall fade ("protection gradient") below sticky table headers to indicate scroll. Charts use a transparent background over the panel surface.

### Borders
1px hairlines in `--border` (`#2a313c`). Dividers inside dense tables are even softer (`#1f252e`). Cards do not have heavy shadows — a single 1px border is the primary container affordance.

### Shadows
Minimal. Floating menus and modals use a single soft shadow:
`0 8px 24px rgba(0,0,0,0.45)`. No layered shadows, no glow effects.

### Corner radii
**4px** for most controls (inputs, buttons, pills). **6px** for panels and modals. **0px** (square) for dense table cells, chart toolbars, and ticker tiles — the "terminal" parts of the UI are deliberately sharp.

### Cards / panels
A panel is `--bg-panel` with a `--border` 1px border and 6px radius. No drop shadow. Header strip inside a panel is `--bg-elevated` with a bottom border. Titles in panel headers are 12px UPPER, `letter-spacing: 0.08em`, color `--fg-secondary`.

### Animation
Almost none. Hover state transitions are `120ms ease-out` on background-color only. No bounce, no scale-on-press, no skeleton shimmer, no Lottie. Charts and live ticks update without animation — the new number simply appears. The only motion permitted: a 600ms color-flash on a price cell when it updates (green→neutral or red→neutral fade-back).

### Hover / press states
- Hover: background lifts one tier (`--bg-panel` → `--bg-elevated`).
- Press: background goes one tier brighter and border becomes `--border-strong`. No scale transforms.
- Disabled: 40% opacity, no pointer events.
- Focus: 1px `--accent` outline with 1px offset. Never the browser-default blue glow.

### Transparency / blur
Almost never. Modals use a flat `rgba(0,0,0,0.6)` scrim — no backdrop-blur. The product is meant to feel like native software, not a glassy web app.

### Imagery
Chart screenshots are the only "imagery." Dark backgrounds, candle bodies in `--up` / `--down`, faint gridlines at 8% white. No stock photos, no illustrations of people, no abstract 3D renders.

### Layout rules
- Persistent **left rail** (48px collapsed, 220px expanded) for top-level nav.
- Optional **top bar** (40px) for global search ("ticker / function") and account.
- Main area is a **grid of resizable panels**. Each panel has a 28px header strip with title (UPPER 12px), a kebab menu, and a maximize toggle.
- Right-side **inspector** panel (320px) for selected-row detail.
- Status bar at bottom (24px) showing connection state, server time, P/L summary.

---

## ICONOGRAPHY

ScanPerfect uses **Lucide** icons via CDN (`https://unpkg.com/lucide@latest`) — they match our 1.5px stroke / square-cap aesthetic and have wide finance/data coverage (trending-up, trending-down, candlestick proxies via `bar-chart-3`, alert bells, filters, etc.).

Sizes: **14px** in dense table headers and inline buttons, **16px** in the left rail collapsed state, **18px** in toolbars. Stroke is left at default (1.5px) for 14–18px, but bumped to `stroke-width: 2` when rendered at 12px to keep edges crisp.

Color: icons inherit `currentColor`, defaulting to `--fg-secondary`. Active/hover state moves to `--fg-primary`. Destructive icons (trash, close-position) use `--down` only on hover.

> ⚠️ **Icon substitution flag** — If ScanPerfect ships with its own icon set or uses a different library (Tabler, Phosphor, custom), point us at the source and we'll swap.

**Emoji / unicode:** Not used in the product. The favicon is a stylized amber square with a glyph; see `assets/favicon.svg`.

**Logo:** A wordmark + a square mark (the "SP" monogram with an embedded amber tick). See `assets/logo.svg` and `assets/mark.svg`.

---

## UI Kits

- **`ui_kits/scanperfect-desktop/`** — foundational CSS classes + React primitives used across the product. Consumed by the Regime Meter; full click-thru prototype deferred.

## Live prototype

**`Regime Meter.html`** is the working reference design. It renders a daily pre-open dashboard of 11 SPDR sector ETFs — each cell shows a 2D density "cone" of the 30 nearest-similar historical days' forward log-return paths over the picked horizon (5/10/20/40 days, whichever has the largest KS-divergence from baseline), with the median path overlaid. Header strip surfaces target date, picked horizon, KS match strength, sector bias breakdown, and a divergence-by-horizon micro chart. The 12th grid cell holds a legend + a neighbor-distance histogram. Toggle between **DENSITY**, **FAN** (quantile bands), and **PATHS** (spaghetti) views; sort sectors by canonical order or by today's median bias.

Input data: `data/regime_meter_today.json`. Drop in a new file with the same shape and the dashboard re-renders.

---

## Quick start for an agent

1. Load `colors_and_type.css` at the top of any new HTML file.
2. Use `data-theme="dark"` on `<html>` (it's the only mode for now).
3. Pull components from `ui_kits/scanperfect-desktop/` — they expect the CSS vars defined here.
4. For any number that represents a price, P/L, %, volume, or time → wrap in `<span class="num">…</span>`.
5. Keep panels at 6px radius, 1px border, no shadow. Keep tables at 0 radius.
