# -*- coding: utf-8 -*-
"""Append batch 91 placements (WOK -> ZTS, final 17) to theme_placement_output.json and advance queue."""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
OUT = ROOT / "local_runner" / "cache" / "theme_placement_output.json"
QUEUE = ROOT / "local_runner" / "cache" / "theme_placement_queue.json"

BATCH = [
    {
        "ticker": "WOK",
        "company_name": "Cohen Circle Acquisition Corp. I",
        "business_summary": "Special purpose acquisition company (SPAC) sponsored by Cohen Circle, formed to identify and merge with a target business with a stated focus on the fintech and financial services sectors. No commercial operations; holds trust proceeds from its IPO pending a business combination.",
        "current_theme": "medtech_devices_consumer_health",
        "proposed_theme": "fintech_disruptors",
        "secondary_themes_add": [],
        "bucket": "C_review_required",
        "theme_reasoning": "Current medtech_devices_consumer_health placement is wrong — WOK is a fintech-focused SPAC, not a medical device company. The auto-placed label is incorrect. Recommend moving primary placement to fintech_disruptors (consistent with sponsor's stated focus and prior track record: Paya, Perella Weinberg, INX).",
        "bull_case": "If WOK announces a high-quality fintech or financial services target, shares can re-rate quickly toward the deal's pro-forma valuation, with trust-account downside protection until the merger vote. Cohen Circle's prior SPAC track record in fintech gives the sponsor credibility for sourcing differentiated private targets. Pre-deal, holders effectively own a near-cash instrument with optionality on a transaction.",
        "bear_case": "SPACs broadly have a poor post-merger return profile, with sponsor promote and warrant dilution eroding common shareholder value once a deal closes. If no acceptable target is identified before the deadline, the vehicle liquidates and returns trust capital with minimal upside above the trust value. Any announced target carries integration, regulatory, and valuation risk that often disappoints versus initial deal projections."
    },
    {
        "ticker": "WOLF",
        "company_name": "Wolfspeed, Inc.",
        "business_summary": "Pure-play silicon carbide (SiC) semiconductor company that designs and manufactures SiC substrates, materials, and power devices used primarily in electric vehicles, industrial power, and renewable energy applications. Operates a 200mm SiC fab in Mohawk Valley, NY; has restructured significantly amid weak EV demand.",
        "current_theme": "ai_compute",
        "proposed_theme": "ai_compute",
        "secondary_themes_add": ["ev_supply_chain"],
        "bucket": "B_cross_listing_added",
        "theme_reasoning": "Ai_compute captures the SiC-for-AI-racks identity. Adding ev_supply_chain reflects that EV powertrains and fast chargers remain the dominant end market for SiC — same theme home as ON/NVTS where the equity story includes both AI power and EV traction inverter SiC demand.",
        "bull_case": "Wolfspeed is the most vertically integrated Western SiC player with first-mover 200mm wafer capacity, positioning it to benefit as EV powertrains, fast chargers, and industrial drives transition from silicon to SiC. CHIPS Act funding and large multi-year design wins with automotive OEMs and Tier 1s provide a backlog visibility tailwind once volumes ramp. Operating leverage from Mohawk Valley utilization could drive a sharp inflection in gross margin if EV demand recovers.",
        "bear_case": "Wolfspeed has burned heavy cash funding capacity ahead of demand, leading to a strained balance sheet, dilution risk, and a restructuring (including a Chapter 11 filing) that wiped out prior equity holders. EV demand softness and aggressive SiC capacity additions from Chinese competitors threaten pricing and share. Yield ramp issues at Mohawk Valley have repeatedly pushed out the path to profitability, undermining management credibility."
    },
    {
        "ticker": "WPM",
        "company_name": "Wheaton Precious Metals Corp.",
        "business_summary": "Precious metals streaming company that provides upfront capital to mining companies in exchange for the right to purchase a percentage of future gold, silver, palladium, and cobalt production at fixed low prices. Does not operate mines itself, instead holding a portfolio of streaming agreements across high-quality assets globally.",
        "current_theme": "gold_silver_miners",
        "proposed_theme": "gold_silver_miners",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Precious metals streamer — squarely gold_silver_miners (the existing rationale correctly notes the royalty/streaming model).",
        "bull_case": "WPM offers leveraged exposure to gold and silver prices with materially lower operating and capex risk than miners, given fixed per-ounce purchase costs and no direct mining liabilities. Its diversified, long-life streaming portfolio with high-quality operators provides decades of visible production growth. The asset-light model yields industry-leading margins and free cash flow, supporting a growing dividend and continued accretive stream acquisitions.",
        "bear_case": "Earnings are highly leveraged to gold and silver prices, which can compress on a stronger dollar, rising real yields, or risk-on rotations. Counterparty operating issues at partner mines (delays, lower grades, permit problems) directly hit attributable production. The premium valuation relative to producers leaves little margin for disappointment if metal prices stall or growth streams underperform."
    },
    {
        "ticker": "WSM",
        "company_name": "Williams-Sonoma, Inc.",
        "business_summary": "Multi-brand specialty retailer of home furnishings and housewares, operating Pottery Barn, West Elm, Williams Sonoma, Rejuvenation, Mark & Graham, and a growing B2B/contract business. Sells primarily through digital channels (~65%+ of revenue) supplemented by a global retail footprint, with a vertically integrated design and supply chain.",
        "current_theme": "consumer_retail",
        "proposed_theme": "consumer_retail",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Specialty home furnishings retailer — squarely consumer_retail alongside M/KSS/DDS/SIG.",
        "bull_case": "WSM has demonstrated best-in-class execution among home furnishings retailers, with structurally higher operating margins (mid-to-high teens) versus historical norms thanks to digital mix, full-price selling discipline, and supply chain efficiency. The B2B segment is a growing high-ROIC adjacency tapping commercial, hospitality, and trade channels. Strong free cash flow generation funds aggressive buybacks and a growing dividend.",
        "bear_case": "Demand for big-ticket home furnishings remains cyclically pressured by weak existing-home sales and elevated mortgage rates, with comp sales running negative. New tariffs on imported home goods and freight inflation could compress gross margins from peak levels. Competition from Wayfair, Restoration Hardware, and lower-cost online players, plus a potential reversion of margins toward pre-COVID levels, leaves valuation exposed."
    },
    {
        "ticker": "WSO",
        "company_name": "Watsco, Inc.",
        "business_summary": "Largest distributor of HVAC/R (heating, ventilation, air conditioning, and refrigeration) equipment, parts, and supplies in North America, serving contractor customers through ~700 locations. Distributes products from OEMs including Carrier (its largest supplier and JV partner), Rheem, and others; leader in digital tools for the HVAC contractor channel.",
        "current_theme": "hvac_cooling",
        "proposed_theme": "hvac_cooling",
        "secondary_themes_add": ["industrial_distribution"],
        "bucket": "B_cross_listing_added",
        "theme_reasoning": "Hvac_cooling captures the HVAC end-market identity. Adding industrial_distribution reflects the actual operating model — wholesale distributor at scale with route density advantage — same equity story as WCC/POOL/SITE/SUNB in industrial_distribution.",
        "bull_case": "Watsco is the scaled consolidator in a fragmented, recession-resilient HVAC distribution market with a long runway of bolt-on M&A. The regulatory transition to A2L refrigerants and higher-SEER systems drives unit price increases and replacement demand, while electrification of heating (heat pumps) expands the addressable market. Strong free cash flow, conservative balance sheet, and a growing dividend support compounding through cycles.",
        "bear_case": "Residential HVAC volumes are tied to housing turnover, repair-vs-replace decisions, and consumer financing, all of which can weaken in a downturn. The Carrier JV concentration is a single-supplier risk, and Carrier's own go-to-market or product strategy shifts could pressure terms. The stock trades at a premium multiple to industrial distributors, leaving limited cushion if same-store sales stagnate or refrigerant-transition pricing benefits fade."
    },
    {
        "ticker": "WULF",
        "company_name": "TeraWulf Inc.",
        "business_summary": "Owns and operates zero-carbon digital infrastructure facilities in New York (Lake Mariner) and previously Pennsylvania (Nautilus), historically focused on bitcoin mining powered largely by nuclear and hydro energy. The company is pivoting capacity and new buildout toward high-performance computing (HPC) and AI hosting alongside its mining operations.",
        "current_theme": "ai_compute_clouds",
        "proposed_theme": "ai_compute_clouds",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Already cross-listed in ai_compute_clouds (primary, for HPC/AI hosting pivot) + crypto_miners. Coverage is comprehensive.",
        "bull_case": "TeraWulf's zero-carbon, low-cost power profile and existing grid interconnects position it to monetize stranded capacity by hosting AI/HPC tenants at significantly higher margins than bitcoin mining. The Lake Mariner site has multi-hundred-megawatt expansion potential adjacent to nuclear baseload, a scarce asset class as hyperscaler power demand surges. Successful HPC contracts would re-rate the stock toward a data-center comp multiple.",
        "bear_case": "Earnings remain heavily tied to bitcoin price and post-halving hash economics, both of which are volatile and structurally challenged. The pivot to AI hosting requires substantial capex, executes against competition from established colo and hyperscaler-aligned developers, and may require equity dilution. Customer concentration, construction execution risk, and uncertain long-term contract terms could disappoint optimistic AI-data-center expectations baked into the stock."
    },
    {
        "ticker": "XNDU",
        "company_name": "Xanadu Quantum Technologies Inc.",
        "business_summary": "Canadian quantum computing company developing photonic quantum hardware and the PennyLane open-source software framework for quantum machine learning. Offers cloud access to its photonic quantum processors with a stated long-term goal of building a fault-tolerant, million-qubit photonic quantum computer.",
        "current_theme": "quantum_computing",
        "proposed_theme": "quantum_computing",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Photonic quantum-computing pure-play — squarely quantum_computing alongside IONQ/QBTS/RGTI/QUBT.",
        "bull_case": "Photonic quantum computing offers a potentially room-temperature, networkable path to scaling qubits, differentiating Xanadu from superconducting and trapped-ion peers. Open-source PennyLane has built developer mindshare in quantum machine learning, creating a software flywheel that could anchor an enterprise and government customer base. Government quantum funding (Canadian and allied) plus partnerships with national labs give early non-dilutive revenue and credibility.",
        "bear_case": "Useful, fault-tolerant quantum computing remains years to decades away, and Xanadu's commercial revenue is small relative to ongoing R&D burn. Competitors with deeper pockets (IBM, Google, IonQ, Quantinuum, PsiQuantum) and alternative qubit modalities could win key benchmarks or contracts. As a relatively early-stage company, Xanadu faces material financing, technical scaling, and dilution risk before reaching commercial quantum advantage."
    },
    {
        "ticker": "XP",
        "company_name": "XP Inc.",
        "business_summary": "Leading Brazilian investment platform and financial services firm offering brokerage, wealth management, asset management, retirement, insurance, banking, and corporate/issuer services. Pioneered the independent financial advisor (IFA) model in Brazil and serves retail and institutional clients largely through digital channels.",
        "current_theme": "latam_em",
        "proposed_theme": "latam_em",
        "secondary_themes_add": ["fintech_disruptors"],
        "bucket": "B_cross_listing_added",
        "theme_reasoning": "Latam_em captures the Brazilian geography. Adding fintech_disruptors gives the equity its real business-theme footing — XP is a dominant Brazilian fintech disrupting incumbent banks (consistent with STNE/VIST/SGML cross-listing pattern earlier in the audit).",
        "bull_case": "XP is the dominant disruptor in Brazil's historically oligopolistic banking sector, with a long runway to capture share of high-net-worth and mass-affluent investable assets from incumbents. Diversification into credit cards, insurance, retirement, and corporate services broadens revenue mix and reduces reliance on equities trading. Brazilian rate-cut cycles tend to boost equity flows and risk asset volumes, lifting fee revenue and net new money.",
        "bear_case": "Earnings are sensitive to Brazilian macro and FX volatility, with high local interest rates pushing flows into fixed income and away from higher-take-rate products. Competition has intensified from Itaú, BTG Pactual, Nubank, and smaller IFA platforms, pressuring net new money growth and advisor economics. Regulatory changes in commissions, taxes, or capital requirements could compress margins."
    },
    {
        "ticker": "XPO",
        "company_name": "XPO, Inc.",
        "business_summary": "North American less-than-truckload (LTL) carrier (the third-largest in the U.S.) following the 2022/2023 spin-offs of GXO (contract logistics) and RXO (brokerage). Operates ~290 service centers and is executing a multi-year operational improvement program (LTL 2.0) focused on service quality, network density, and margin expansion.",
        "current_theme": "transports",
        "proposed_theme": "transports",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "LTL carrier — squarely transports alongside ODFL/SAIA/KNX/WERN.",
        "bull_case": "XPO has structural margin upside as it closes the operating-ratio gap to best-in-class LTL peers (Old Dominion, Saia), with self-help levers in pricing, insourcing linehaul, and service center additions from the Yellow estate. LTL is a consolidated, high-barrier industry with rational pricing and pricing power tied to weight-based shipping demand. A freight cycle recovery would provide volume tailwinds layered on top of self-help.",
        "bear_case": "Freight demand remains soft, with tonnage and shipment counts under pressure as industrial and goods-economy activity stays weak. Competitive capacity additions following Yellow's exit, including from Saia and Estes, threaten to absorb share and limit pricing gains. Execution risk on the multi-year LTL 2.0 plan, plus elevated capex for service centers and equipment, could disappoint margin expectations if volumes do not recover."
    },
    {
        "ticker": "XRAY",
        "company_name": "DENTSPLY SIRONA Inc.",
        "business_summary": "Global manufacturer of professional dental products and technologies, including imaging systems, CAD/CAM, treatment centers, implants, orthodontic aligners (SureSmile), endodontics, and consumables. Serves dentists, dental specialists, and dental laboratories worldwide.",
        "current_theme": "medtech_devices_consumer_health",
        "proposed_theme": "medtech_devices_consumer_health",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Dental devices + consumables — squarely medtech_devices_consumer_health alongside NVST/PODD/PRCT.",
        "bull_case": "Dentsply Sirona is one of two scaled global dental platforms with a broad portfolio spanning equipment and high-margin consumables, giving it cross-sell leverage across the operatory. Long-term tailwinds from aging demographics, rising dental insurance penetration, and demand for cosmetic and implant procedures support unit growth. A new management team has been executing on cost cuts, portfolio rationalization, and capital returns.",
        "bear_case": "Dental capital equipment demand has been weak amid soft practice economics, higher rates pressuring financing, and post-COVID over-purchasing, hitting near-term revenue. The company has had recurring accounting/restatement issues, executive turnover, and credibility concerns that weigh on the multiple. Competition from Envista, Align, Straumann, and regional Asian players is intensifying across most product lines."
    },
    {
        "ticker": "XSD",
        "company_name": "SPDR S&P Semiconductor ETF",
        "business_summary": "Exchange-traded fund managed by State Street Global Advisors that tracks the S&P Semiconductor Select Industry Index, an equal-weighted index of U.S.-listed semiconductor stocks. Because of equal weighting, exposure is skewed toward small- and mid-cap chip names relative to market-cap-weighted peers like SOXX or SMH.",
        "current_theme": "ai_compute",
        "proposed_theme": "ai_compute",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Equal-weight semi ETF — squarely ai_compute alongside SOXL/USD/PSI/TSMX cohort.",
        "bull_case": "XSD offers broad, equal-weighted exposure to the semiconductor industry, capturing AI-driven demand for compute, networking, memory, and equipment without concentration in mega-caps like NVIDIA or TSMC. Equal weighting allows higher participation from smaller, higher-growth chip names that can outperform in cyclical upswings. Secular tailwinds from AI, automotive electrification, reshoring (CHIPS Act), and IoT support long-term industry growth.",
        "bear_case": "Semiconductors are deeply cyclical, and equal weighting amplifies exposure to small/mid-cap names that are more volatile and earnings-vulnerable in downturns. Underweighting of dominant AI beneficiaries (NVIDIA, Broadcom) can cause material underperformance vs. cap-weighted peers when leadership narrows. Geopolitical risks and capex digestion cycles can hit the entire group simultaneously."
    },
    {
        "ticker": "Z",
        "company_name": "Zillow Group, Inc.",
        "business_summary": "Operates the largest U.S. real estate marketplace (Zillow.com, Trulia, StreetEasy), monetizing through Premier Agent advertising, mortgage origination (Zillow Home Loans), rentals, and a growing 'enhanced markets' agent partnership program. Also offers tools like 3D Home tours, dotloop, ShowingTime, and Follow Up Boss to power its 'housing super app' strategy.",
        "current_theme": "real_estate_proptech",
        "proposed_theme": "real_estate_proptech",
        "secondary_themes_add": ["ad_tech_marketing"],
        "bucket": "B_cross_listing_added",
        "theme_reasoning": "Real_estate_proptech captures the housing-super-app identity. Adding ad_tech_marketing reflects the Premier Agent advertising business that is the largest revenue line — same theme home as TTD/PINS/ZETA where the equity story is digital ad inventory monetization.",
        "bull_case": "Zillow is the dominant traffic destination in U.S. residential real estate, giving it durable lead-generation power monetizable across advertising, mortgage, rentals, and adjacent services. Its 'enhanced markets' / Touring Agreement strategy expands monetization per customer toward a more recurring transaction-tied revenue model. Rentals and mortgage are growing double-digit-plus, diversifying away from cyclical agent ad spend, and a housing volume recovery would provide an additional tailwind.",
        "bear_case": "U.S. existing-home transaction volumes remain near multi-decade lows, capping the Premier Agent funnel. NAR commission settlement changes could pressure agent economics and ad budgets over time. Competition from CoStar's Homes.com (with aggressive marketing spend), Redfin, and brokerage-direct platforms threatens lead share, and Zillow trades at a premium multiple that depends on execution of multi-product monetization."
    },
    {
        "ticker": "ZBRA",
        "company_name": "Zebra Technologies Corporation",
        "business_summary": "Provides enterprise asset intelligence solutions including barcode scanners, mobile computers, RFID readers, specialty printers, and software/services for retail, e-commerce fulfillment, transportation & logistics, manufacturing, and healthcare. Global leader in rugged enterprise mobile computing and barcode printing.",
        "current_theme": "robotics_automation",
        "proposed_theme": "robotics_automation",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Enterprise asset intelligence + warehouse robotics — squarely robotics_automation.",
        "bull_case": "Zebra is the scaled leader in enterprise data capture and mobile computing, with installed-base lock-in and recurring software/services tailwinds from its 'Zebra ONE Care' and analytics offerings. Post-destocking, demand should normalize across retail, warehousing, and manufacturing, with longer-term growth from RFID, machine vision, and AI-enabled enterprise workflows. Operating leverage on volume recovery could drive significant EPS upside given the recent margin trough.",
        "bear_case": "Zebra's customers (retail, T&L) over-bought during COVID and have been working through inventory, leading to a deep cyclical downturn that may take longer to recover than expected. Exposure to enterprise capex cycles, especially in retail tech budgets, leaves the company vulnerable to a consumer slowdown. Competition from Honeywell, Datalogic, and lower-cost Chinese vendors plus tariff exposure on hardware supply chains pressure margins."
    },
    {
        "ticker": "ZETA",
        "company_name": "Zeta Global Holdings Corp.",
        "business_summary": "AI-powered marketing technology company providing the Zeta Marketing Platform (ZMP), which combines a proprietary data set on hundreds of millions of consumers with AI/ML to enable multichannel customer acquisition, engagement, and retention. Serves enterprise marketers across financial services, telecom, travel, healthcare, and retail.",
        "current_theme": "ai_apps_platforms",
        "proposed_theme": "ai_apps_platforms",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Already cross-listed in ai_apps_platforms (primary) + ad_tech_marketing. Coverage captures both the AI-driven marketing identity and the ad-tech franchise.",
        "bull_case": "Zeta benefits from secular shifts toward first-party data, marketing consolidation onto AI-native platforms, and erosion of third-party cookies, all favoring its owned-data + AI model. Strong net revenue retention and large-customer growth show platform stickiness and meaningful land-and-expand dynamics. Operating leverage is improving rapidly, with adjusted EBITDA margins expanding and free cash flow inflecting positively.",
        "bear_case": "Zeta has faced a short-seller report alleging deceptive consent practices and revenue quality issues, raising governance and regulatory questions. Marketing budgets are cyclical and exposed to a slower consumer, with rivals like Salesforce, Adobe, Braze, and HubSpot competing for the same enterprise wallet. Heavy stock-based compensation, customer concentration, and reliance on agency channel partnerships create dilution and revenue durability risks."
    },
    {
        "ticker": "ZM",
        "company_name": "Zoom Communications, Inc.",
        "business_summary": "Provides cloud-based unified communications, including video meetings, Zoom Phone (cloud PBX), Team Chat, Zoom Contact Center, Zoom Rooms, and a growing AI Companion / Workplace suite. Serves enterprises, SMBs, and consumers globally, having become a standard video conferencing platform during the pandemic.",
        "current_theme": "productivity_saas",
        "proposed_theme": "productivity_saas",
        "secondary_themes_add": ["ai_apps_platforms"],
        "bucket": "B_cross_listing_added",
        "theme_reasoning": "Productivity_saas captures the video/UC platform identity. Adding ai_apps_platforms reflects the AI Companion + Custom AI Companion paid tiers being rolled out — same theme home as NOW/TEAM/VEEV/WDAY where AI is monetized on top of a productivity seat base.",
        "bull_case": "Zoom generates substantial free cash flow with a fortress balance sheet (>$7B net cash), supporting buybacks and reinvestment into platform expansion. Zoom Phone and Contact Center are credible growth vectors as enterprises consolidate UCaaS/CCaaS spend onto an integrated platform. The AI Companion bundled free with paid seats could drive ARPU expansion via paid AI tiers and reduce churn through differentiated productivity features.",
        "bear_case": "Core online (consumer/SMB) video meetings revenue is shrinking as post-COVID demand normalizes and remote-work tailwinds fade. Microsoft Teams' bundled distribution remains an existential competitive threat to Zoom's enterprise expansion, with Google Meet and Cisco Webex also vying for share. Revenue growth has decelerated to low single digits, leaving the stock dependent on margin sustainability and capital returns rather than top-line growth."
    },
    {
        "ticker": "ZS",
        "company_name": "Zscaler, Inc.",
        "business_summary": "Operates a cloud-native Secure Access Service Edge (SASE) / Zero Trust platform, with flagship products Zscaler Internet Access (ZIA) and Zscaler Private Access (ZPA) that inspect and secure user-to-app traffic via its global cloud rather than legacy on-prem appliances. Also offers data protection, deception, posture management, and AI-powered analytics modules.",
        "current_theme": "cybersecurity",
        "proposed_theme": "cybersecurity",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Cloud-delivered zero-trust/SASE cybersecurity — squarely cybersecurity alongside PANW/CRWD/FTNT/OKTA.",
        "bull_case": "Zscaler is a clear leader in cloud-delivered zero-trust networking, with strong secular tailwinds from VPN replacement, SD-WAN/firewall consolidation, and AI-driven traffic growth. Large enterprises increasingly standardize on Zscaler as a strategic security platform, driving multi-product attach rates, high net retention, and a growing $1M+ ARR customer cohort. Free cash flow margins are robust, and a path to GAAP profitability gives the business a self-funding posture even at premium growth multiples.",
        "bear_case": "Zscaler faces intensifying competition from Palo Alto Networks (Prisma), Cisco (with Splunk), Cloudflare, Netskope, and Microsoft (Entra), with consolidation pressure on best-of-breed SASE point vendors. Sales-cycle elongation has pressured billings growth, and stock-based compensation remains very high, weighing on shareholder value. The stock trades at a premium SaaS multiple that depends on sustained 20%+ growth and successful new-product attach."
    },
    {
        "ticker": "ZTS",
        "company_name": "Zoetis Inc.",
        "business_summary": "World's largest animal health company, developing and selling medicines, vaccines, diagnostics, and genetic tests for livestock (cattle, swine, poultry, fish) and companion animals (dogs, cats, horses). Key franchises include the dermatology portfolio (Apoquel, Cytopoint), parasiticides (Simparica Trio), and OA pain therapies (Librela/Solensia).",
        "current_theme": "medtech_devices_consumer_health",
        "proposed_theme": "medtech_devices_consumer_health",
        "secondary_themes_add": [],
        "bucket": "D_confirmed_no_change",
        "theme_reasoning": "Animal health pharmaceuticals — squarely medtech_devices_consumer_health (the broad health-products theme home).",
        "bull_case": "Zoetis benefits from durable secular tailwinds in companion animal health, including humanization of pets, rising vet visit spend, and an expanding chronic-care portfolio (dermatology, pain, parasiticides). Innovative franchises like Librela/Solensia and Simparica Trio drive double-digit growth and pricing power with strong patent protection. The business compounds at attractive margins with consistent free cash flow, modest cyclicality, and a clean balance sheet supporting buybacks and tuck-in M&A.",
        "bear_case": "Heightened safety concerns and FDA scrutiny around Librela adverse events could pressure key growth driver if labeling changes or vet sentiment shifts. Generic and biosimilar entrants in dermatology and parasiticides could compress pricing as franchises mature. Livestock segment exposure to commodity producer economics and global protein cycles adds volatility, and the stock's premium multiple leaves little room for execution missteps."
    },
]


def main():
    out = json.loads(OUT.read_text(encoding="utf-8"))
    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    tickers = [p["ticker"] for p in BATCH]
    out["placements"].extend(BATCH)
    q["remaining"] = [t for t in q["remaining"] if t not in tickers]
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    QUEUE.write_text(json.dumps(q, indent=2), encoding="utf-8")
    buckets = {}
    for p in BATCH:
        buckets[p["bucket"]] = buckets.get(p["bucket"], 0) + 1
    print(f"Added {len(BATCH)}; output={len(out['placements'])}, queue remaining={len(q['remaining'])}")
    print(f"Bucket: {buckets}")


if __name__ == "__main__":
    main()
