"""Co-movement audit of every theme's narrative-zone placement. For each theme,
correlate its composite's daily moves against (a) the undisputed AI-buildout core
and (b) SPY; the AI-tilt (corrAI - corrSPY) isolates AI-specific co-movement from
plain market beta. Flag themes whose tape disagrees with their assigned zone:
filed-as-connected (Buildout/Output) but trading independent, or filed-as-off-
narrative (Adjacent/Noise) but trading with the AI cluster. Read-only. ASCII."""
import sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

# --- proposed zone placement (theme -> zone); Output is ONE group per Dan ---
ZONE = {}
for t in ["mag7", "ai_compute", "datacenter_buildout", "ai_compute_clouds"]:
    ZONE[t] = "Hub"
for t in ["ai_optics", "ai_networking", "ai_connectivity", "semiconductor_ip",
          "memory_cycle", "semi_equipment", "semiconductor_testing", "electronic_components"]:
    ZONE[t] = "Infrastructure"
for t in ["power_demand_for_ai", "nuclear_renaissance", "uranium_pure_plays",
          "energy_storage", "electrical_grid", "hvac_cooling", "fuel_cells_hydrogen"]:
    ZONE[t] = "Power"
ZONE["critical_minerals"] = "Materials"
for t in ["quantum_computing", "robotics_automation", "space_economy", "drones",
          "autonomous_mobility", "ai_apps_platforms", "defense_tech", "biotech",
          "diagnostics_tools", "cybersecurity", "cloud_infra", "data_devops_platforms",
          "productivity_saas", "vertical_saas", "data_analytics_terminals", "ad_tech_marketing"]:
    ZONE[t] = "Output"
for t in ["solar", "ev_supply_chain", "reshoring_construction", "building_products",
          "industrial_distribution", "gold_silver_miners", "industrial_metals", "industrial_materials"]:
    ZONE[t] = "Adjacent"
for t in ["crypto_platforms", "bitcoin_treasury", "crypto_miners"]:
    ZONE[t] = "Crypto"
for t in ["ev_makers", "fintech_disruptors", "financial_services_specialty", "alt_asset_managers",
          "managed_care", "medtech_devices_consumer_health", "transports", "airlines",
          "oil_gas_ep", "oil_gas_services", "energy_drinks_wellness", "restaurants_fast_casual",
          "footwear_apparel", "beauty", "discount_retail", "ecommerce", "travel_services",
          "streaming_media", "gaming_betting", "social_media", "real_estate_proptech",
          "auto_parts_tech", "consulting_govt", "consumer_retail", "latam_em", "china"]:
    ZONE[t] = "Noise"

MACRO = {"Hub": "Buildout", "Infrastructure": "Buildout", "Power": "Buildout", "Materials": "Buildout",
         "Output": "Output", "Adjacent": "Adjacent", "Crypto": "Crypto", "Noise": "Noise"}

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print(f"cache tickers: {len(cache)}")
missing_zone = [k for k in THEMES if k not in ZONE]
if missing_zone:
    print(f"WARNING: themes with no zone assigned: {missing_zone}")

def dret(tk):
    if tk not in cache:
        return None
    d = cache[tk].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    s = d[~d.index.duplicated(keep="last")]["close"].astype(float)
    return s.pct_change()

def theme_ret(members):
    rs = [dret(m) for m in members if dret(m) is not None]
    return pd.concat(rs, axis=1).mean(axis=1, skipna=True) if rs else None

def cum(members, n):
    rs = theme_ret(members)
    if rs is None:
        return np.nan
    r = rs.dropna().iloc[-n:]
    return ((1 + r).prod() - 1) * 100 if len(r) >= n * 0.6 else np.nan

AI_CORE = ["NVDA", "AVGO", "AMD", "MU", "CRWV", "NBIS", "VRT", "ANET", "AMAT", "LRCX"]
ai = theme_ret(AI_CORE)
spy = dret("SPY")
spy_cum63 = ((1 + spy.dropna().iloc[-63:]).prod() - 1) * 100

def corr(a, b, n):
    m = pd.concat([a, b], axis=1).dropna().iloc[-n:]
    return m.iloc[:, 0].corr(m.iloc[:, 1]) if len(m) >= n * 0.6 else np.nan

rows = []
for k in THEMES:
    r = theme_ret(THEMES[k])
    if r is None:
        continue
    cai = corr(r, ai, 63)
    csp = corr(r, spy, 63)
    if np.isnan(cai) or np.isnan(csp):
        continue
    rs63 = cum(THEMES[k], 63)
    rs_vs_spy = rs63 - spy_cum63 if not np.isnan(rs63) else np.nan
    rows.append(dict(k=k, zone=ZONE.get(k, "?"), macro=MACRO.get(ZONE.get(k, "?"), "?"),
                     cai=cai, csp=csp, tilt=cai - csp, rs=rs_vs_spy))

rows.sort(key=lambda x: -x["tilt"])
print(f"\nSPY 63-bar return: {spy_cum63:+.1f}%   (RS column = theme 63d return minus this)")
print("=" * 92)
print(f"{'theme':26s} {'zone':14s} {'corrAI':>6s} {'corrSPY':>7s} {'tilt':>6s} {'RSvsSPY':>8s}")
print("=" * 92)
for x in rows:
    print(f"{THEME_LABELS.get(x['k'], x['k'])[:26]:26s} {x['zone']:14s} "
          f"{x['cai']:6.2f} {x['csp']:7.2f} {x['tilt']:6.2f} {x['rs']:+8.1f}")

# ---- discrepancy flags ----
print("\n" + "=" * 92)
print("DISCREPANCIES  (tape disagrees with filed zone)")
print("=" * 92)
HI, LO = 0.12, 0.0
flagged = 0
print("\n[A] filed OFF-NARRATIVE (Adjacent/Noise) but trades WITH the AI cluster (tilt high):")
for x in sorted(rows, key=lambda r: -r["tilt"]):
    if x["macro"] in ("Adjacent", "Noise", "Crypto") and x["tilt"] >= HI:
        print(f"    {THEME_LABELS.get(x['k'], x['k'])[:30]:30s} {x['zone']:12s} tilt {x['tilt']:+.2f}  RS {x['rs']:+.0f}")
        flagged += 1
print("\n[B] filed CONNECTED (Buildout/Output) but trades INDEPENDENT of the AI cluster (tilt <= 0):")
for x in sorted(rows, key=lambda r: r["tilt"]):
    if x["macro"] in ("Buildout", "Output") and x["tilt"] <= LO:
        print(f"    {THEME_LABELS.get(x['k'], x['k'])[:30]:30s} {x['zone']:12s} tilt {x['tilt']:+.2f}  RS {x['rs']:+.0f}")
        flagged += 1
print(f"\ntotal flagged: {flagged}")
