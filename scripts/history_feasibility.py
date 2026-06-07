"""Feasibility probe for a 2020-> multi-cycle map. How far back does the cache go,
and which bias-FREE proxies (broad indices + sector/thematic ETFs) are present with
history back to 2020? ETFs don't suffer survivorship / current-ADR selection bias the
way single-name theme composites do. Read-only."""
import pickle
from pathlib import Path
import pandas as pd

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")
with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print("tickers in cache:", len(cache))

# theme -> representative bias-free ETF proxy (point-in-time-correct NAV history)
PROXIES = {
    "broad market":        ["SPY", "QQQ", "RSP", "IWM", "MDY"],
    "semis":               ["SMH", "SOXX"],
    "software":            ["IGV", "WCLD"],
    "cybersecurity":       ["CIBR", "HACK", "BUG"],
    "cloud/internet":      ["SKYY", "FDN"],
    "robotics/AI":         ["BOTZ", "ROBO", "ARKQ"],
    "innovation/spec":     ["ARKK", "ARKW"],
    "solar":               ["TAN"],
    "clean energy":        ["ICLN", "PBW", "QCLN"],
    "uranium/nuclear":     ["URA", "URNM", "NLR"],
    "lithium/battery":     ["LIT", "BATT"],
    "grid/energy storage": ["GRID"],
    "crypto-adjacent":     ["BITQ", "WGMI", "GBTC", "BLOK"],
    "biotech":             ["IBB", "XBI"],
    "gold miners":         ["GDX", "GDXJ"],
    "metals/materials":    ["XLB", "XME", "COPX"],
    "energy/oil svcs":     ["XLE", "OIH", "XOP"],
    "financials/banks":    ["XLF", "KRE"],
    "china internet":      ["KWEB", "FXI"],
    "homebuilders":        ["XHB", "ITB"],
    "travel/airlines":     ["JETS"],
    "defense/space":       ["ITA", "PPA", "ARKX", "UFO"],
    "quantum":             ["QTUM"],
    "sectors (SPDR)":      ["XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLRE", "XLC"],
}

def span(tk):
    if tk not in cache:
        return None
    d = pd.to_datetime(cache[tk]["date"])
    return d.min(), d.max(), len(d)

print("\nBROAD INDEX history depth:")
for tk in ["SPY", "QQQ", "RSP", "IWM"]:
    s = span(tk)
    if s:
        print(f"  {tk:5s}  {s[0].date()} -> {s[1].date()}  ({s[2]} bars)")

print("\nETF THEME-PROXY availability (✓ = present, with earliest date):")
have2020, total = 0, 0
for theme, etfs in PROXIES.items():
    cells = []
    for e in etfs:
        s = span(e)
        total += 1
        if s is None:
            cells.append(f"{e}:--")
        else:
            pre2020 = s[0] <= pd.Timestamp("2020-01-01")
            if pre2020:
                have2020 += 1
            cells.append(f"{e}:{s[0].date()}{'*' if pre2020 else ''}")
    print(f"  {theme:20s}  " + "  ".join(cells))
print(f"\n* = has history back to before 2020.  {have2020}/{total} proxy ETFs cover the full 2020-> window.")
