"""Export local_runner/theme_map.py as a shareable JSON file.

Produces local_runner/cache/theme_map_export.json with:
  - per-theme label + trader narrative
  - per-theme member list (each entry = {ticker, optional rationale})
  - the full UNIVERSE ticker list

Rationale comments (the inline `# rationale: ...` annotations on a
ticker's line in theme_map.py) are preserved as a per-member field
when present. This is the value-add over a generic sector list.

Re-run whenever theme_map.py changes.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "local_runner"))

from local_runner.theme_map import (  # noqa: E402
    THEMES,
    THEME_LABELS,
    THEME_NARRATIVES,
    UNIVERSE,
)

THEME_MAP_PATH = REPO_ROOT / "local_runner" / "theme_map.py"
OUTPUT_PATH = REPO_ROOT / "local_runner" / "cache" / "theme_map_export.json"

SCHEMA_VERSION = 1

# Parse rationale comments out of theme_map.py.
# Format inside a theme block: "TICKER",  # rationale: <text>
# Theme block boundary: "theme_id": [ ... ]
THEME_BLOCK_RE = re.compile(
    r'["\'](?P<theme>[a-zA-Z0-9_]+)["\']\s*:\s*\[(?P<body>[^\[\]]*?)\]',
    re.DOTALL,
)
RATIONALE_LINE_RE = re.compile(
    r'["\'](?P<ticker>[A-Z][A-Z0-9.\-]{0,7})["\']\s*,?\s*#\s*rationale:\s*(?P<text>[^\n]*)',
)


def parse_rationales() -> dict[str, dict[str, str]]:
    """Return {theme_id: {ticker: rationale_text}} from the .py source."""
    src = THEME_MAP_PATH.read_text(encoding="utf-8")
    rationales: dict[str, dict[str, str]] = {}
    for theme_match in THEME_BLOCK_RE.finditer(src):
        theme_id = theme_match.group("theme")
        body = theme_match.group("body")
        if theme_id not in THEMES:
            # Skip dicts other than the THEMES dict (e.g., THEME_LABELS)
            continue
        per_ticker: dict[str, str] = {}
        for line_match in RATIONALE_LINE_RE.finditer(body):
            ticker = line_match.group("ticker")
            text = line_match.group("text").strip()
            per_ticker[ticker] = text
        if per_ticker:
            rationales[theme_id] = per_ticker
    return rationales


def build_export() -> dict:
    rationales = parse_rationales()
    themes_out: dict[str, dict] = {}
    for theme_id, tickers in THEMES.items():
        members = []
        per_ticker_rat = rationales.get(theme_id, {})
        for tk in tickers:
            entry = {"ticker": tk}
            if tk in per_ticker_rat:
                entry["rationale"] = per_ticker_rat[tk]
            members.append(entry)
        themes_out[theme_id] = {
            "label": THEME_LABELS.get(theme_id, theme_id.replace("_", " ").title()),
            "narrative": THEME_NARRATIVES.get(theme_id, ""),
            "members": members,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "themes": themes_out,
        "universe": list(UNIVERSE),
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = build_export()
    OUTPUT_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    n_themes = len(data["themes"])
    n_members = sum(len(t["members"]) for t in data["themes"].values())
    n_with_rationale = sum(
        1 for t in data["themes"].values() for m in t["members"] if "rationale" in m
    )
    print(f"Wrote {OUTPUT_PATH}")
    print(f"  size: {size_kb:.1f} KB")
    print(f"  themes: {n_themes}")
    print(f"  member entries: {n_members}")
    print(f"  members with rationale: {n_with_rationale}")
    print(f"  universe size: {len(data['universe'])}")


if __name__ == "__main__":
    main()
