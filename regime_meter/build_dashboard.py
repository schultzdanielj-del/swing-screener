"""Build a self-contained dashboard HTML that opens via file:// with no server.

Reads:
  regime_meter/cache/regime_meter_today.json     (latest refresh output)
  regime_meter/dashboard/Regime Meter.html       (Claude Design template)
  regime_meter/dashboard/tweaks-panel.jsx        (React: Tweaks panel + helpers)
  regime_meter/dashboard/Cone.jsx                (React: per-sector cone viz)
  regime_meter/dashboard/Dashboard.jsx           (React: layout + header)

Writes:
  regime_meter/dashboard/index.html              (single self-contained file)

The output inlines every JSX file as <script type="text/babel"> blocks and embeds
the JSON as window.REGIME_DATA, so browsers don't need to fetch() anything at
runtime. That removes the CORS block that prevents Claude Design's stock HTML
from loading via file://.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
DASH = ROOT / "dashboard"

JSON_PATH = CACHE / "regime_meter_today.json"
TEMPLATE_PATH = DASH / "Regime Meter.html"
OUT_PATH = DASH / "index.html"

JSX_FILES = ["tweaks-panel.jsx", "Cone.jsx", "Dashboard.jsx"]


def main() -> int:
    if not JSON_PATH.exists():
        print(f"ERROR: missing {JSON_PATH}", file=sys.stderr)
        return 1
    if not TEMPLATE_PATH.exists():
        print(f"ERROR: missing {TEMPLATE_PATH}", file=sys.stderr)
        return 1

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    data_json = json.dumps(data).replace("</", "<\\/")

    html = TEMPLATE_PATH.read_text(encoding="utf-8")

    for jsx in JSX_FILES:
        path = DASH / jsx
        if not path.exists():
            print(f"ERROR: missing {path}", file=sys.stderr)
            return 1
        src = path.read_text(encoding="utf-8")
        external = f'<script type="text/babel" src="{jsx}"></script>'
        inline = f'<script type="text/babel" data-source="{jsx}">\n{src}\n</script>'
        if external not in html:
            print(f"ERROR: template missing reference to {jsx}", file=sys.stderr)
            return 1
        html = html.replace(external, inline)

    data_block = f'<script>window.REGIME_DATA = {data_json};</script>\n</head>'
    html = html.replace("</head>", data_block, 1)

    fetch_block = (
        "const res = await fetch('data/regime_meter_today.json');\n"
        "      const data = await res.json();"
    )
    if fetch_block not in html:
        print("ERROR: template fetch block not found; aborting", file=sys.stderr)
        return 1
    html = html.replace(fetch_block, "const data = window.REGIME_DATA;")

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"built {OUT_PATH}  ({OUT_PATH.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
