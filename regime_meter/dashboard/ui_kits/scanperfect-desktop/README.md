# ScanPerfect Desktop — UI Kit (foundational layer)

This folder holds the foundational CSS and React primitives used by ScanPerfect interfaces.

It does **not** ship as a standalone storybook — there is no `index.html` here. Instead, the live reference for "what a ScanPerfect screen looks like" is **`Regime Meter.html`** at the project root, which consumes the tokens and classes defined in this folder.

## Files

- **`ui-kit.css`** — every component class used across the product (`.sp-panel`, `.sp-table`, `.sp-pill`, `.sp-tabs`, `.sp-btn`, `.sp-input`, `.sp-ticker-tile`, etc.). Token-based; depends on `colors_and_type.css` at the project root.
- **`Primitives.jsx`** — React wrappers around the above: `<Panel>`, `<PanelHeader>`, `<Pill>`, `<FnKey>`, `<Num>`, `<ChangePct>`, `<Button>`, `<IconBtn>`, `<Tabs>`, plus an inline `<Icon name="…">` for the small Lucide-style icon set.

## Using primitives

Load order in a host HTML file:

```html
<link rel="stylesheet" href="/colors_and_type.css">
<link rel="stylesheet" href="/ui_kits/scanperfect-desktop/ui-kit.css">
<!-- React + Babel CDN imports (see project README) -->
<script type="text/babel" src="/ui_kits/scanperfect-desktop/Primitives.jsx"></script>
```

`Primitives.jsx` attaches all components to `window`, so any subsequent `<script type="text/babel">` can reference them directly.

## Open question

A full click-thru prototype (login → scan → chart → alert flow) was deferred when the project pivoted to the Regime Meter dashboard. If you want it built out, point me at any existing screenshots or codebase you have for ScanPerfect's other surfaces.
