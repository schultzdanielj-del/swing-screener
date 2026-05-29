"""
Compact view of company_meta.json for theme-consolidation analysis.

Reads:  local_runner/cache/company_meta.json
        local_runner/theme_map.py (THEMES + UNIVERSE)

Writes: local_runner/cache/company_meta_compact.txt

For each ticker in UNIVERSE, emits one line:
    TICKER | current_theme(s) | longName | first sentence of longBusinessSummary

Sorted alphabetically. Lets a reader scan all 488 names in a few KB of text
rather than reading the full 640 KB JSON.
"""

import os
import sys
import json
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
META_FILE = os.path.join(CACHE_DIR, "company_meta.json")
OUT_FILE = os.path.join(CACHE_DIR, "company_meta_compact.txt")


def first_sentence(text):
    """Return the first sentence of a Yahoo longBusinessSummary."""
    if not text:
        return ""
    # Yahoo summaries always start with "<Co Name> Inc., ..." or similar.
    # Cut at the first period followed by a space + capital letter (or end).
    m = re.search(r"\.\s+(?=[A-Z])", text)
    if m:
        return text[: m.start() + 1].strip()
    return text.strip()


def main():
    from local_runner.theme_map import THEMES, UNIVERSE

    with open(META_FILE, encoding="utf-8") as f:
        meta = json.load(f)
    tickers_data = meta.get("tickers", {})

    # Reverse index: ticker -> list of themes it appears in
    ticker_to_themes = {}
    for theme_key, ticker_list in THEMES.items():
        for t in ticker_list:
            ticker_to_themes.setdefault(t, []).append(theme_key)

    lines = []
    n_meta_missing = 0
    n_summary_missing = 0
    n_ungrouped = 0

    for ticker in sorted(set(UNIVERSE)):
        themes = ticker_to_themes.get(ticker, [])
        themes_str = "+".join(themes) if themes else "[UNGROUPED]"
        if not themes:
            n_ungrouped += 1

        entry = tickers_data.get(ticker, {})
        if "error" in entry or not entry:
            n_meta_missing += 1
            lines.append(
                f"{ticker:<6} | {themes_str:<60} | (no meta: "
                f"{entry.get('error', 'missing')})"
            )
            continue

        ln = entry.get("longName") or ""
        bs = entry.get("longBusinessSummary") or ""
        if not bs:
            n_summary_missing += 1
        fs = first_sentence(bs)

        lines.append(
            f"{ticker:<6} | {themes_str:<60} | {ln:<45} | {fs}"
        )

    header = [
        f"# Compact view of {len(lines)} tickers in theme_map.UNIVERSE",
        f"# Source meta: {META_FILE}",
        f"# Missing meta: {n_meta_missing}  Missing summary: "
        f"{n_summary_missing}  Ungrouped: {n_ungrouped}",
        f"# Format: TICKER | current_theme(s) | longName | first sentence",
        "",
    ]

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(header + lines))

    print(f"Wrote {OUT_FILE} ({os.path.getsize(OUT_FILE) / 1024:.1f} KB, "
          f"{len(lines)} rows)")
    print(f"  Missing meta: {n_meta_missing}")
    print(f"  Missing summary: {n_summary_missing}")
    print(f"  Ungrouped (in UNIVERSE but no theme): {n_ungrouped}")


if __name__ == "__main__":
    main()
