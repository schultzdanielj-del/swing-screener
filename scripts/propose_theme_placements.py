"""Auto-classify ungrouped tickers into existing themes.

One-shot classifier. Two signals: (1) Yahoo industry matches the
dominant industry-signature of an existing theme, (2) the ticker's
business summary hits >=2 keywords from a hand-curated per-theme
keyword list. Placement requires the top theme's score to beat the
runner-up by >= 2 points — anything ambiguous stays ungrouped.

Prints proposals grouped by destination theme. Does NOT mutate
theme_map.py — a separate apply step does that after Dan has
eyeballed the output.
"""
import sys, json
from collections import Counter, defaultdict

sys.path.insert(0, 'local_runner')
from theme_map import THEMES, UNIVERSE

fund = json.load(open('local_runner/cache/fundamentals_cache.json',
                     'r', encoding='utf-8')).get('tickers', {})
meta = json.load(open('local_runner/cache/company_meta.json',
                     'r', encoding='utf-8')).get('tickers', {})


THEME_KEYWORDS = {
    'cybersecurity': ['cyber', 'cybersecurity', 'vulnerability', 'threat detection',
                       'identity management', 'zero trust', 'siem', 'endpoint protection',
                       'firewall', 'malware', 'phishing', 'security exposure', 'cyberattack'],
    'cloud_infra': ['cloud computing', 'cloud platform', 'cloud infrastructure', 'hyperscale',
                     'iaas', 'paas', 'content delivery network', 'edge computing'],
    'ai_apps_platforms': ['generative ai', 'large language model', 'llm', 'ai assistant',
                           'ai-powered', 'ai platform', 'artificial intelligence platform', 'chatbot'],
    'vertical_saas': ['vertical software', 'industry-specific software', 'restaurant software',
                       'construction software', 'practice management software'],
    'productivity_saas': ['productivity software', 'workflow automation', 'collaboration platform',
                           'document management', 'workspace platform'],
    'biotech': ['gene therapy', 'gene editing', 'mrna', 'antibody', 'oncology',
                 'immunotherapy', 'biopharmaceutical', 'crispr', 'monoclonal',
                 'rare disease', 'biologic'],
    'medtech_devices_consumer_health': ['medical device', 'surgical robot', 'orthopedic',
                                         'cardiovascular device', 'implant', 'diabetes device',
                                         'continuous glucose', 'wearable health'],
    'diagnostics_tools': ['molecular diagnostic', 'liquid biopsy', 'genomic test',
                           'biomarker', 'lab diagnostics'],
    'defense_tech': ['defense contractor', 'military', 'missile', 'electronic warfare',
                      'national security', 'munitions'],
    'ev_makers': ['electric vehicle manufacturer', 'plug-in hybrid', 'bev manufacturer'],
    'ev_supply_chain': ['lithium-ion battery', 'cathode', 'anode', 'ev charging'],
    'critical_minerals': ['rare earth', 'lithium mining', 'copper mining',
                           'nickel mining', 'cobalt mining', 'graphite mining'],
    'nuclear_renaissance': ['nuclear reactor', 'small modular reactor', 'smr', 'nuclear power'],
    'uranium_pure_plays': ['uranium mining', 'yellowcake'],
    'solar': ['solar panel', 'photovoltaic', 'solar inverter', 'solar installer'],
    'oil_gas_ep': ['oil and gas exploration', 'upstream oil', 'crude oil production',
                    'natural gas production'],
    'oil_gas_services': ['oilfield services', 'drilling services', 'pressure pumping'],
    'gold_silver_miners': ['gold mining', 'silver mining', 'precious metals miner'],
    'crypto_miners': ['bitcoin mining', 'crypto mining', 'hashrate'],
    'crypto_platforms': ['cryptocurrency exchange', 'digital asset platform', 'blockchain platform'],
    'bitcoin_treasury': ['bitcoin treasury'],
    'fintech_disruptors': ['fintech', 'digital payments', 'neobank', 'buy now pay later'],
    'financial_services_specialty': ['specialty finance', 'consumer finance', 'equipment leasing'],
    'alt_asset_managers': ['alternative asset', 'private equity', 'private credit'],
    'data_analytics_terminals': ['data analytics platform', 'financial data', 'market data'],
    'data_devops_platforms': ['devops', 'observability', 'application performance monitoring',
                               'developer platform'],
    'datacenter_buildout': ['data center', 'colocation', 'datacenter infrastructure'],
    'memory_cycle': ['dram', 'hbm', 'nand flash', 'memory chip'],
    'semi_equipment': ['wafer fab', 'lithography', 'etch equipment',
                        'semiconductor capital equipment'],
    'semiconductor_testing': ['semiconductor test', 'automated test equipment'],
    'electronic_components': ['passive components', 'connectors', 'capacitors'],
    'robotics_automation': ['industrial robot', 'collaborative robot', 'factory automation'],
    'autonomous_mobility': ['autonomous driving', 'self-driving', 'lidar', 'adas'],
    'drones': ['unmanned aerial vehicle', 'uav', 'drone'],
    'space_economy': ['satellite communications', 'space launch', 'spaceflight'],
    'quantum_computing': ['quantum computing', 'quantum processor', 'qubit'],
    'auto_parts_tech': ['automotive supplier', 'auto parts', 'powertrain'],
    'transports': ['trucking', 'freight transportation', 'less-than-truckload',
                    'logistics services'],
    'airlines': ['airline operator', 'air carrier', 'commercial aviation'],
    'travel_services': ['online travel', 'cruise line', 'lodging'],
    'restaurants_fast_casual': ['restaurant operator', 'fast casual restaurant'],
    'gaming_betting': ['online gambling', 'sports betting', 'casino operator'],
    'streaming_media': ['streaming service', 'streaming media', 'video on demand'],
    'social_media': ['social network', 'social media platform'],
    'ad_tech_marketing': ['ad tech', 'advertising platform', 'programmatic advertising'],
    'ecommerce': ['e-commerce platform', 'online marketplace'],
    'beauty': ['cosmetics', 'beauty products', 'skincare'],
    'footwear_apparel': ['footwear', 'athletic apparel'],
    'energy_drinks_wellness': ['energy drink', 'sports drink', 'functional beverage'],
    'energy_storage': ['energy storage system', 'battery storage', 'grid-scale battery'],
    'electrical_grid': ['electrical grid', 'transmission infrastructure',
                         'utility infrastructure'],
    'hvac_cooling': ['hvac', 'data center cooling', 'thermal management',
                      'refrigeration systems'],
    'building_products': ['building products', 'home improvement materials',
                           'construction materials'],
    'reshoring_construction': ['industrial construction', 'infrastructure construction'],
    'consulting_govt': ['government consulting', 'professional services for government'],
    'industrial_distribution': ['industrial distribution', 'industrial supplies distributor'],
    'industrial_metals': ['steel producer', 'aluminum producer'],
    'fuel_cells_hydrogen': ['fuel cell', 'hydrogen power', 'green hydrogen'],
    'managed_care': ['health insurance plan', 'managed care', 'health benefits'],
    'discount_retail': ['discount retailer', 'off-price retail', 'dollar store'],
}


def main():
    # Industry signatures per theme (industries shared by >=3 of the theme's members).
    theme_industries = {}
    for theme, members in THEMES.items():
        cnt = Counter((fund.get(t) or {}).get('industry') for t in members)
        theme_industries[theme] = {ind for ind, n in cnt.items() if ind and n >= 3}

    tickers_in_themes = {t for tl in THEMES.values() for t in tl}
    ungrouped = [t for t in UNIVERSE if t not in tickers_in_themes]

    placements = {}
    ambig = weak = 0
    for tk in ungrouped:
        industry = ((fund.get(tk) or {}).get('industry') or '').strip()
        summary = ((meta.get(tk) or {}).get('longBusinessSummary') or '').lower()
        scores = []
        for theme, sig_inds in theme_industries.items():
            s = 0
            rs = []
            if industry in sig_inds:
                s += 2
                rs.append(industry)
            kws = [k for k in THEME_KEYWORDS.get(theme, []) if k in summary]
            if len(kws) >= 2:
                s += len(kws)
                rs.append('+'.join(kws[:2]))
            elif len(kws) == 1 and industry in sig_inds:
                s += 1
                rs.append(kws[0])
            if s > 0:
                scores.append((theme, s, '; '.join(rs)))
        if not scores:
            continue
        scores.sort(key=lambda x: -x[1])
        top = scores[0]
        if top[1] < 3:
            weak += 1
            continue
        if len(scores) >= 2 and top[1] - scores[1][1] < 2:
            ambig += 1
            continue
        placements[tk] = (top[0], top[2])

    by_theme = defaultdict(list)
    for tk, (theme, reason) in placements.items():
        by_theme[theme].append((tk, reason))

    print(f'Total ungrouped: {len(ungrouped)}')
    print(f'HIGH-confidence placements: {len(placements)}')
    print(f'Rejected (ambiguous): {ambig}   Rejected (weak): {weak}')
    print(f'Remain ungrouped after: {len(ungrouped) - len(placements)}')
    print('\n=== PROPOSED PLACEMENTS BY THEME ===')
    for theme in sorted(by_theme.keys()):
        print(f'\n[{theme}] +{len(by_theme[theme])}')
        for tk, reason in sorted(by_theme[theme]):
            nm = ((meta.get(tk) or {}).get('longName') or '')[:50]
            nm = nm.encode('ascii', 'replace').decode()
            print(f'  {tk:6s}  {nm:50s}  ({reason})')
    return placements


if __name__ == '__main__':
    main()
