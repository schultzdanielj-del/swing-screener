"""Theme to ticker mapping for the hot theme dashboard.

THEMES are NARRATIVES — where money is rotating, not GICS sectors.
A ticker can (and usually should) appear in multiple themes.
Order below = order in the dashboard sidebar.

UNIVERSE is the full hand-picked ticker list from Dan (~470).
Any ticker in UNIVERSE that doesn't appear in any THEMES list shows
up in the "Ungrouped" section at the bottom of the dashboard.
"""

THEMES = {
    # ═══════════════════════════════════════════════════
    #  MEGA-CAP TECH — the index-anchor names
    # ═══════════════════════════════════════════════════
    "mag7":                     ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA",
        "AMZU",   # rationale: BUCKET C -- proposed primary-theme CHANGE from ecommerce to mag7 to match AMZN underlying placement. AMZU tracks AMZN at 200% daily exposure; for theme-dashboard consistency the...
        "GGLL",   # rationale: BUCKET C -- proposed primary-theme CHANGE from ad_tech_marketing to mag7 to match GOOGL underlying placement. GGLL tracks GOOGL at 200% daily exposure; for theme-dashboard consi...
        "METU",   # rationale: BUCKET C -- proposed primary-theme CHANGE from social_media to mag7 to match META underlying placement. METU tracks META at 200% daily exposure; for theme-dashboard consistency ...
        "MSFU",   # rationale: BUCKET C -- proposed primary-theme CHANGE from cloud_infra to mag7 to match MSFT underlying placement. MSFU tracks MSFT at 200% daily exposure; for theme-dashboard consistency t...
    ],

    # ═══════════════════════════════════════════════════
    #  AI BUILDOUT — the dominant 2024-2026 narrative
    # ═══════════════════════════════════════════════════
    "ai_compute":               [
        "NVDA", "AMD", "AVGO", "MRVL", "TXN", "QCOM", "INTC", "MU", "NXPI",
        "ON",     # rationale: power-management semiconductors for AI server boards
        "GFS",    # rationale: non-leading-edge foundry capacity (analog/RF/power) supporting AI buildout
        "WOLF",   # rationale: silicon-carbide and GaN power devices for high-density AI racks
        "ALAB",   # rationale: cross-listed in ai_networking + ai_connectivity; included for end-market AI exposure
        "RMBS",   # rationale: memory interface IP (DDR5 / HBM controllers) tied directly to AI memory demand
        "MPWR",   # rationale: high-density analog power-management ICs designed into GPU servers
        "LSCC",   # rationale: low-power FPGAs for AI server board management and control
        "SWKS", "QRVO",
        "SITM",   # rationale: precision MEMS timing ICs for high-speed AI interconnects
        "SMTC",
        "SYNA",
        "TSEM",   # rationale: specialty analog foundry feeding the AI semiconductor ecosystem
        "CRDO",   # rationale: cross-listed in ai_networking + ai_optics + ai_connectivity; high-speed serdes/AECs
        "NVTS",   # rationale: GaN power-conversion ICs for high-efficiency AI racks
        "MXL",
        "TSMX",   # rationale: 2x leveraged TSM ETN (directional foundry exposure)
        "SOXL",   # rationale: 3x leveraged semiconductor ETF (directional theme exposure)
        "USD",    # rationale: 2x leveraged semiconductor ETF
        "XSD",    # rationale: equal-weight semiconductor ETF
        "PSI",    # rationale: Invesco semiconductor ETF,
        "AMBA",   # rationale: ai_compute is the primary placement — AMBA is an AI-inference SoC designer alongside MRVL/AVGO/NVDA in the existing roster. Direct theme fit on the 80% edge-AI revenue mix. Cros...
        "AOSL",   # rationale: ai_compute captures power semis explicitly via MPWR (Monolithic Power), NVTS (Navitas — GaN power conversion), MXL — peers to AOSL in AI server power. AOSL fits cleanly. electro...
    ],
    "ai_optics":                [
        "AAOI", "COHR", "LITE", "POET", "MTSI",
        "AXTI",   # rationale: indium phosphide substrates feeding optical transceiver supply chain
        "CIEN",   # rationale: optical networking gear including WAN coherent + DC interconnect
        "CRDO",   # rationale: high-speed optical DSPs for 800G/1.6T transceivers (cross-listed)
        "FN",     # rationale: contract manufacturer of optical modules for Cisco/Ciena/Lumentum
        "GLW",    # rationale: Corning optical fiber + photonics glass feedstock
        "LWLG",   # rationale: electro-optic polymer modulators (next-gen photonic IC)
        "MKSI",   # rationale: photonics + vacuum tooling for optical-component manufacturing
        "VIAV",   # rationale: optical network test/measurement instruments
        "MRVL",   # rationale: optical DSPs for transceivers (cross-listed)
        "AVGO",   # rationale: optical PHYs/DSPs and silicon photonics roadmap (cross-listed)
        "NOK",    # rationale: Infinera acquisition (Feb 2024, $2.3B) added ICE6 800G + ICE7 1.6T coherent optical engines; Network Infrastructure segment ships DWDM + DCI line systems for hyperscaler datacenter interconnect alongside CIEN,
        "ADTN",   # rationale: ai_optics is the right primary theme via direct peer parity with NOK (which I just added there) — both are non-pure-play optical transport vendors that became coherent-optical c...
        "IPGP",   # rationale: ai_optics is the right fit — peer roster includes AAOI (Applied Optoelectronics), COHR (Coherent — DIRECT peer; same fiber-laser + photonics business), LITE (Lumentum — DIRECT p...
        "LASR",   # rationale: Cross-listing defense_tech + ai_optics captures both narrative drivers exactly the dual-identity case. defense_tech (KTOS/AVAV/BWXT/AXON/RCAT/ONDS/OSK/KRMN/RKLB/TTMI) is the def...
    ],
    "ai_networking":            [
        "ANET", "CRDO", "AAOI",
        "ALAB",   # rationale: Scorpio smart fabric switch entered $20B AI switching market in early 2026
        "FN",     # rationale: contract mfr of networking + optical gear for top OEMs
        "CIEN",   # rationale: optical networking that stitches DC fabric across buildings (WAN/metro)
        "NET",    # rationale: Cloudflare global edge network adjacent to AI inference traffic
        "AKAM",   # rationale: CDN + edge compute infrastructure
        "UI",     # rationale: Ubiquiti enterprise networking (less DC-fabric, more SMB/edge),
        "EXTR",   # rationale: ai_networking is the right fit — peer roster includes ANET (Arista — DIRECT peer at hyperscaler tier; same data-center/campus switching identity), CRDO + AAOI (interconnect), AL...
    ],
    "ai_connectivity":          [
        "ALAB",   # rationale: PCIe 6 / CXL / NVLink Fusion retimers and Aries smart cable modules
        "MRVL",   # rationale: Alaska P PCIe Gen 6 retimers + custom interconnect ASICs
        "AVGO",   # rationale: PCIe and high-speed serdes for hyperscaler custom silicon
        "MXL",    # rationale: high-speed communications SoCs (PAM4, broadband)
        "CRDO",   # rationale: AECs / PCIe retimers for short-reach high-speed links
        "SLAB",   # rationale: mixed-signal MCUs (more IoT/edge than DC; included for connectivity overlap)
        "MPWR",   # rationale: low-noise power for high-speed serdes/PCIe rails
        "ADI",    # rationale: high-speed converters and analog front-ends for AI signal paths
        "TXN",    # rationale: power and analog support for AI accelerator cards,
        "POWI",   # rationale: ai_connectivity is the right fit — peer roster includes ALAB (Astera Labs), MRVL (Marvell), AVGO (Broadcom), MXL (MaxLinear), CRDO (Credo), SLAB (Silicon Labs), MPWR (Monolithic...
        "AKAN",   # rationale: BUCKET C -- proposed REMOVAL from biotech (the entity is no longer a cannabis/drug-manufacturer business; reverse merger Aug 2025 made it a Mexican dark-fiber telecom-infrastruc...
        "APH",   # rationale: Cross-listing added: APH's IT Datacom segment growth +127% YoY in late 2025 / early 2026 driven by hyperscaler AI buildout supplying critical NVIDIA GPU connectors + $10.5B Comm...
        "MTSI",   # rationale: Cross-listing added: MTSI's Lightwave + Photonics segment for AI fabric optical interconnect + 800G + 1.6T transceivers + AI hyperscaler optical applications materializes ai_con...
    ],
    "semiconductor_ip":         [
        "ARM", "SNPS", "CDNS",
        "CEVA",   # rationale: DSP / wireless / AI inference IP licensed to chipmakers (smaller royalty model),
        "ADEA",   # rationale: semiconductor_ip captures IP-licensing royalty engines on semiconductor designs. Existing members are ARM (mobile/server CPU IP) + SNPS + CDNS (EDA tools) + CEVA (DSP IP). ADEA ...
        "DLB",   # rationale: semiconductor_ip is the closest fit — peer roster includes ARM (chip architecture IP), SNPS (Synopsys EDA + IP), CDNS (Cadence EDA + IP), CEVA (audio + connectivity IP licensing...
        "IDCC",   # rationale: BUCKET C -- proposed primary-theme CHANGE from electronic_components to semiconductor_ip. IDCC is fundamentally an intellectual property (IP) licensing company (patent licensing...
    ],
    "memory_cycle":             [
        "MU", "SNDK",
        "STX",    # rationale: high-capacity HDDs for AI inference cold storage (mass-capacity supercycle)
        "WDC",    # rationale: post-Sandisk spinoff WD now pure-play hard drives + enterprise storage
        "MRAM",   # rationale: Everspin MRAM — niche persistent memory adjacent to AI workloads
        "P",      # rationale: Everpure data-storage software (purity SaaS layer over memory hardware)
        "DRAM",   # rationale: Roundhill Memory ETF (basket exposure to memory cycle)
    ],
    "semi_equipment":           [
        "LRCX", "AMAT", "KLAC", "ONTO",
        "ENTG",   # rationale: ultra-pure materials and consumables for wafer fabs (high-volume razor)
        "FORM",   # rationale: probe cards for wafer test (capex-correlated)
        "TER",    # rationale: ATE for chip test (cross-listed with semiconductor_testing)
        "NVMI",   # rationale: process-control / metrology for fab yield
        "AEHR",   # rationale: SiC + burn-in test systems (cross-listed with semiconductor_testing)
        "ACLS",   # rationale: ion-implant systems for mature-node + power semi fabs
        "MKSI",   # rationale: vacuum + power-delivery subsystems for fab tools
        "ICHR",   # rationale: precision fluid-delivery subsystems for fab equipment
        "UCTT",   # rationale: gas-delivery and chamber subsystems for fab equipment
        "Q",      # rationale: Qnity Electronics — materials/solutions supplied directly into semi-fab and electronics manufacturing,
        "ACMR",   # rationale: semi_equipment captures wafer fab capex — exact match. Roster includes LRCX/AMAT/KLAC (the US majors ACMR is partially substituting for in China) + ENTG/FORM/ICHR/UCTT (smaller ...
        "KLIC",   # rationale: semi_equipment is the exact fit — peer roster includes LRCX (Lam Research), AMAT (Applied Materials), KLAC (KLA — wafer inspection + metrology, similar customer base), ONTO (Ont...
        "PDFS",   # rationale: semi_equipment is the closest fit — peer roster includes LRCX (Lam Research), AMAT (Applied Materials), KLAC (KLA — DIRECT peer; KLA also offers semi yield-management + process-...
        "SKYT",   # rationale: Cross-listing semi_equipment + quantum_computing captures both narrative drivers. semi_equipment roster (LRCX/AMAT/KLAC/ONTO/ENTG/FORM/TER/NVMI/AEHR/ACLS + recent placement KLIC...
        "TRT",   # rationale: semi_equipment is the right fit -- peer roster includes LRCX, AMAT, KLAC, ONTO, ENTG, FORM, TER, NVMI, AEHR (DIRECT peer; semiconductor burn-in + reliability test specialist), A...
        "VECO",   # rationale: semi_equipment is the exact fit -- peer roster includes LRCX, AMAT, KLAC, ONTO, ENTG, FORM, TER, NVMI, AEHR, ACLS (DIRECT peer; Veeco's pending merger partner) + recent placemen...
        "AMKR",   # rationale: BUCKET C -- proposed REMOVAL from ai_compute (incorrect placement; AMKR is an OSAT services + advanced packaging + test provider, NOT an AI compute / processor / GPU company). P...
    ],
    "datacenter_buildout":      [
        "VRT", "DELL", "HPE", "SMCI", "ANET",
        "HPQ",    # rationale: workstations + commercial PC supply chain into hyperscaler office/edge fleets
        "CLS",    # rationale: Celestica builds custom hyperscaler racks (e.g. Google TPU board assembly)
        "JBL",    # rationale: Jabil Intelligent Infrastructure segment — server/network EMS for hyperscalers
        "FLEX",   # rationale: Flex data-center supply chain + manufacturing for cloud customers
        "SANM",   # rationale: Sanmina integrated manufacturing for cloud/data-center hardware
        "PENG",   # rationale: Penguin Solutions Advanced Computing + Integrated Memory for enterprise/cloud
        "CRDO",   # rationale: AECs inside the rack (cross-listed in ai_networking/optics/connectivity)
        "MOD",    # rationale: Modine — liquid-cooling thermal solutions for AI racks (mission-critical)
        "AAON",   # rationale: precision cooling units for high-density data centers
        "BE",     # rationale: Bloom Energy solid-oxide fuel cells for on-site DC power
        "MPWR",   # rationale: rack power management (cross-listed in ai_compute/connectivity)
        "NVT",    # rationale: nVent electrical connection + thermal management products for DC
        "AEIS",   # rationale: Advanced Energy precision power-conversion for data centers
        "CDW",    # rationale: IT-solution channel into hyperscalers + enterprise data centers
        "LUMN",   # rationale: fiber + private connectivity stitching DCs (PrivateConnect for AI)
        "FPS",    # rationale: Forgent Power Solutions — electrical distribution gear for DCs and grid
        "VISN",   # rationale: Vistance Networks infrastructure for comms + data center sites,
        "ATMU",   # rationale: datacenter_buildout is the right primary placement — the Industrial Solutions segment growth narrative is explicitly built on data center cooling/HVAC filtration demand, peer to...
        "BDC",   # rationale: datacenter_buildout captures the data center buildout ecosystem (servers + cooling + racks + fiber + cabling). Roster includes VRT (Vertiv cooling) + DELL/HPE/SMCI (servers) + A...
        "BELFB",   # rationale: Cross-listing defense_tech + datacenter_buildout captures both narrative drivers. defense_tech is the largest segment (55% of revenue) and peer to TTMI (defense electronics PCBs...
        "BHE",   # rationale: datacenter_buildout captures the EMS contract manufacturers that build hyperscaler server racks + networking gear. Roster includes CLS (Celestica — direct peer to BHE in scale-d...
        "BIPC",   # rationale: Cross-listing power_demand_for_ai + datacenter_buildout captures both narrative drivers. power_demand_for_ai contains CEG/VST/TLN (nuclear + gas IPPs with hyperscaler PPAs) + GE...
        "CC",   # rationale: Cross-listing industrial_materials + datacenter_buildout captures both narrative drivers. industrial_materials roster (DOW/CE/WLK/ESI/IP/SW/CF/MOS) is the chemicals/packaging/fe...
        "FRMI",   # rationale: Cross-listing datacenter_buildout + power_demand_for_ai captures both narrative drivers — Fermi sits exactly at the intersection. datacenter_buildout roster (VRT/DELL/HPE/SMCI/A...
        "MAIR",   # rationale: Cross-listing hvac_cooling + datacenter_buildout captures both narrative drivers. hvac_cooling roster (AAON/MOD/WSO) is the small but direct HVAC + cooling-equipment peer set — ...
        "PLXS",   # rationale: datacenter_buildout is the right fit — peer roster includes VRT (Vertiv), DELL (Dell), HPE (HPE), SMCI (Super Micro), ANET (Arista), HPQ (HP Inc), CLS (Celestica — DIRECT EMS pe...
        "WYFI",   # rationale: Cross-listing captures the integrated vertical model. ai_compute_clouds is the core GPU-cloud + hosting business -- peer roster includes CRWV, CIFR, IREN, NBIS, BTBT (parent), A...
        "APLD",   # rationale: Cross-listing added: APLD's Polaris Forge 1 Ellendale ND campus (400MW + 15-year $11B CoreWeave lease + scheduled 2026-27 expansion) + crypto-to-AI-data-center transformation ma...
        "DGXX",   # rationale: BUCKET C -- proposed REMOVAL from crypto_platforms (incorrect placement; DGXX is a Bitcoin miner + energy infrastructure + colocation operator, NOT a crypto exchange/wallet/trea...
    ],
    "ai_compute_clouds":        [
        "CRWV", "NBIS", "APLD",
        "IREN",   # rationale: Bitcoin miner pivot → $9.7B Microsoft deal + Nvidia partnership for 5GW AI
        "RXT",    # rationale: Rackspace hybrid-cloud + AI operations services
        "DOCN",   # rationale: DigitalOcean agentic-inference cloud platform (SMB-oriented)
        "CIFR",   # rationale: Cipher Mining — Bitcoin miner with HPC hosting expansion
        "WULF",   # rationale: TeraWulf — Bitcoin miner pivoting to AI data-center hosting
        "CORZ",   # rationale: Core Scientific — Bitcoin mining infra + colocation hosting (CoreWeave customer)
        "BTDR",   # rationale: Bitdeer — blockchain + HPC infrastructure
        "HIVE",   # rationale: HIVE Digital — green-powered data centers (Bitcoin + HPC)
        "HUT",    # rationale: Hut 8 — power + digital infrastructure with HPC pivot
        "RIOT",   # rationale: Riot Platforms — Bitcoin miner with HPC site optionality
        "MARA",   # rationale: MARA Holdings — Bitcoin + energy infrastructure with HPC ambition
        "CLSK",   # rationale: CleanSpark — Bitcoin miner with energy infrastructure assets
        "TLN",    # rationale: Talen Energy — independent power producer with Amazon nuclear-PPA for AI DC
        "KEEL",   # rationale: Keel Infrastructure — purpose-built HPC/AI data-center operator,
        "BTBT",   # rationale: ai_compute_clouds is the right primary placement — the theme is explicitly 'AI Compute Clouds — GPU-as-a-Service + miner-pivot DCs', which describes exactly BTBT's transition (B...
        "SLNH",   # rationale: Cross-listing crypto_miners + ai_compute_clouds captures both narrative drivers — Soluna is essentially the pure-play example of the miner-pivot-to-AI-DC transition that defines...
        "WYFI",   # rationale: Cross-listing captures the integrated vertical model. ai_compute_clouds is the core GPU-cloud + hosting business -- peer roster includes CRWV, CIFR, IREN, NBIS, BTBT (parent), A...
    ],
    "semiconductor_testing":    [
        "TER", "FORM",
        "KLAC",   # rationale: KLA process-control / metrology (cross-listed in semi_equipment)
        "ONTO",   # rationale: Onto Innovation — wafer inspection + metrology (cross-listed)
        "NVMI",   # rationale: Nova — process-control / yield metrology (cross-listed)
        "AEHR",   # rationale: Aehr Test — wafer-level burn-in + silicon-photonics test (AI DC ramp)
        "COHU",   # rationale: Cohu — handlers + contactors for semiconductor test
        "CAMT",   # rationale: Camtek — inspection + metrology for advanced packaging (HBM yield)
    ],
    "ai_apps_platforms":        [
        "PLTR", "APP",
        "TEM",    # rationale: Tempus AI — clinical-data layer for oncology + precision medicine (cross-listed in biotech)
        "BBAI",   # rationale: BigBear.ai — AI decision-intelligence for government/defense applications
        "SOUN",   # rationale: SoundHound — independent voice AI platform
        "PATH",   # rationale: UiPath — RPA + agentic-AI automation platform (NOT autonomous driving)
        "IOT",    # rationale: Samsara — physical-operations data platform (fleet IoT + AI; cross-listed in autonomous_mobility)
        "DUOL",   # rationale: Duolingo — AI-powered language learning (consumer AI app)
        "INOD",   # rationale: Innodata — data-engineering services for AI training datasets
        "U",      # rationale: Unity Software — real-time 3D + AI development platform
        "TTD",    # rationale: Trade Desk — AI-driven programmatic ad-tech (cross-listed in ad_tech_marketing)
        "ZETA",   # rationale: Zeta Global — AI-driven marketing cloud platform,
        "AI", # rationale: auto-placed (Software - Infrastructure; generative ai+ai platform),
        "BAND",   # rationale: ai_apps_platforms is the right placement — the dominant 2025-26 narrative driver is AI voice agent infrastructure (OpenAI Realtime API + multi-model BYO-AI + 'Agentic Operations...
        "CRM",   # rationale: Cross-listing productivity_saas + ai_apps_platforms captures both narrative drivers — exactly the MRVL-style multi-theme pattern Dan endorses. productivity_saas (NOW/WDAY/MNDY/H...
        "PEGA",   # rationale: Cross-listing productivity_saas + ai_apps_platforms captures both narrative drivers, parallel to CRM (Salesforce) just-placed in batch 8 with the same dual-identity logic. produ...
        "XMTR",   # rationale: Cross-listing matches the dual identity. ai_apps_platforms captures the AI-native marketplace differentiator -- AI instant-quoting + lead-time prediction + personalized pricing ...
    ],
    "quantum_computing":        [
        "IONQ", "QBTS", "RGTI", "QUBT",
        "INFQ",   # rationale: Infleqtion — neutral-atom quantum computing systems
        "XNDU",   # rationale: Xanadu — photonic quantum computing hardware + software,
        "SKYT",   # rationale: Cross-listing semi_equipment + quantum_computing captures both narrative drivers. semi_equipment roster (LRCX/AMAT/KLAC/ONTO/ENTG/FORM/TER/NVMI/AEHR/ACLS + recent placement KLIC...
    ],

    # ═══════════════════════════════════════════════════
    #  POWER & ENERGY — AI's electricity bill
    # ═══════════════════════════════════════════════════
    "power_demand_for_ai":      [
        "CEG", "VST", "NRG", "TLN",
        "GEV",    # rationale: GE Vernova — gas/wind turbines + grid equipment supplying utilities expanding for AI load
        "ETN",    # rationale: Eaton — power-management gear for DC + grid
        "VRT",    # rationale: Vertiv power/cooling at the rack edge (cross-listed in datacenter_buildout)
        "GNRC",   # rationale: Generac backup generators + grid-stability products for sites near AI DCs
        "AEIS",   # rationale: Advanced Energy precision power conversion (cross-listed in datacenter_buildout)
        "NVT",    # rationale: nVent electrical connection/protection (cross-listed in datacenter_buildout)
        "POWL",   # rationale: Powell Industries — custom switchgear for utilities and DCs
        "MPWR",   # rationale: power-management ICs (cross-listed in ai_compute/connectivity/datacenter_buildout)
        "MOD",    # rationale: Modine thermal/cooling (cross-listed in datacenter_buildout)
        "AAON",   # rationale: precision cooling (cross-listed in datacenter_buildout)
        "BE",     # rationale: Bloom Energy on-site fuel cells (cross-listed in datacenter_buildout)
        "SOLS",   # rationale: Solstice Advanced Materials — specialty chemicals (refrigerants/electronic materials) into power/cooling,
        "BEPC",   # rationale: power_demand_for_ai is the correct primary placement — the theme captures utilities + power-equipment names selling firm 24/7 baseload to hyperscalers. Roster includes CEG (Cons...
        "BIPC",   # rationale: Cross-listing power_demand_for_ai + datacenter_buildout captures both narrative drivers. power_demand_for_ai contains CEG/VST/TLN (nuclear + gas IPPs with hyperscaler PPAs) + GE...
        "BW",   # rationale: Cross-listing power_demand_for_ai + electrical_grid captures both narrative drivers exactly the way many peers in those themes are dual-placed. power_demand_for_ai roster includ...
        "FRMI",   # rationale: Cross-listing datacenter_buildout + power_demand_for_ai captures both narrative drivers — Fermi sits exactly at the intersection. datacenter_buildout roster (VRT/DELL/HPE/SMCI/A...
        "PSIX",   # rationale: Cross-listing electrical_grid + power_demand_for_ai captures both narrative drivers. electrical_grid roster (ETN/VRT/GEV/GNRC/NRG/POWL/AEIS/NVT/RRX/SPXC + recent placements ENS/...
        "PUMP",   # rationale: Cross-listing oil_gas_services + power_demand_for_ai captures both narrative drivers — exactly the dual-identity case as ProPetro transforms from pure-play Permian frac to data ...
        "WLDN",   # rationale: Cross-listing matches the dual identity. power_demand_for_ai is the stock-driver rerating narrative -- data-center electrical engineering + AI grid software is the 2025-26 catal...
        "ACM",   # rationale: Cross-listing added: AECOM's data-center practice +50% growth in FY25 + AI-driven data-center electrical-infrastructure positioning makes power_demand_for_ai a load-bearing seco...
        "ADI",   # rationale: Cross-listing added: ADI's data-center business grew +50% in FY25 + Empower Semiconductor $1.5B acquisition (May 2026) directly targets AI data-center vertical-power-delivery --...
        "AESI",   # rationale: Cross-listing added: AESI's Power segment has transformed via Moser acquisition + 1.4GW Caterpillar framework + 120MW data-center PPA + 2GW 2030 target into a material AI-data-c...
        "AGX",   # rationale: Cross-listing added: AGX's $3B backlog is 61-79% gas-fired power projects with major data-center-tied capacity (1.2GW Sandow Lakes 'powering data hubs + AI operations' + 1.35GW ...
        "ALGM",   # rationale: BUCKET C -- proposed REMOVAL from ai_compute (incorrect placement; ALGM is a fabless sensor/power-IC company for automotive + industrial markets, NOT an AI compute/processor chi...
        "ECG",   # rationale: Cross-listing added: ECG's data center electrical + mechanical construction work has scaled to material revenue contributor (+25%+ YoY growth in data-center segment) + AI hypers...
        "FIX",   # rationale: Cross-listing added: FIX's data center + technology end-market exposure scaled to ~35% of FY25 revenue + growing aggressively + AI hyperscaler M&E contracting positioning makes ...
        "FLR",   # rationale: Cross-listing added: FLR's data center + advanced manufacturing + mission critical EPC backlog (+30% YoY Urban Solutions) + AI hyperscaler campus EPC + semiconductor fab (TSMC A...
        "FPS",   # rationale: Cross-listing added: FPS's AI data center electrical-distribution-equipment positioning + ATS + PDU + Gear eHouses for hyperscaler AI campus electrical infrastructure makes powe...
        "IESC",   # rationale: Cross-listing added: IESC's Communications segment data center electrical + structured cabling + fiber + DAS work has scaled to dominant growth contributor (+40%+ YoY in data ce...
        "J",   # rationale: Cross-listing added: Jacobs' Infrastructure & Advanced Facilities + advanced manufacturing + data center + microelectronics + semiconductor fab EPC + program management work pos...
        "LGN",   # rationale: Cross-listing added: Legence's mission-critical M&E + Engineering & Consulting + Installation & Maintenance for AI hyperscaler data center + semiconductor fab + healthcare + lif...
        "MTZ",   # rationale: Cross-listing added: MTZ's Power Delivery + utility T&D + AI hyperscaler campus electrical infrastructure construction + transmission expansion + grid hardening materializes pow...
        "MYRG",   # rationale: Cross-listing added: MYRG's Transmission & Distribution + utility T&D + AI hyperscaler campus electrical infrastructure construction + transmission expansion + grid-hardening + ...
    ],
    "nuclear_renaissance":      [
        "CCJ", "OKLO", "SMR", "LEU", "BWXT", "UEC",
        "UUUU",   # rationale: Energy Fuels — uranium + REE recovery (cross-listed in critical_minerals/uranium_pure_plays)
        "CEG", "VST", "NRG", "TLN",   # nuclear-utility cross-listings (also in power_demand_for_ai)
        "NLR",    # rationale: VanEck Uranium & Nuclear ETF (basket exposure),
        "MIR",   # rationale: nuclear_renaissance is the right primary placement — peer roster includes CCJ (Cameco uranium), OKLO (Oklo SMR developer), SMR (NuScale SMR), LEU (Centrus Energy), BWXT (BWX nuc...
        "NNE",   # rationale: nuclear_renaissance is the exact fit — peer roster includes CCJ (Cameco uranium), OKLO (Oklo Inc — DIRECT peer; advanced fission microreactor developer + Aurora powerhouse desig...
    ],
    "uranium_pure_plays":       [
        "CCJ", "LEU", "UEC", "UUUU",
        "DNN",    # rationale: Denison Mines — Athabasca-basin uranium exploration/development (Canadian)
        "URA",    # rationale: Global X Uranium ETF (basket exposure across miners + nuclear-fuel cycle)
        "URNM",   # rationale: Sprott Uranium Miners ETF (basket; concentrated in miners),
        "NXE",   # rationale: uranium_pure_plays is the exact fit — peer roster includes CCJ (Cameco — global uranium leader), LEU (Centrus Energy uranium enrichment + HALEU fuel), UEC (Uranium Energy — DIRE...
    ],
    "solar":                    [
        "FSLR", "ENPH",
        "SEDG",   # rationale: SolarEdge — residential + C&I solar inverters and optimizers
        "RUN",    # rationale: Sunrun — residential solar installer + storage (post-25D credit expiry headwind)
        "NXT",    # rationale: Nextpower — utility-scale solar tracker systems
        "TE",     # rationale: T1 Energy — US + Norway solar module and cell manufacturing,
        "ARRY", # rationale: auto-placed (Solar; photovoltaic)
        "CSIQ", # rationale: auto-placed (Solar; photovoltaic),
        "MWH",   # rationale: Cross-listing reshoring_construction + solar captures both narrative drivers. reshoring_construction roster (FIX/PWR/MTZ/IESC/PRIM/MYRG/AGX/STRL/ACM/ECG + CTRI just-placed) is t...
        "SHLS",   # rationale: solar is the exact fit — peer roster includes FSLR (First Solar — DIRECT peer; utility-scale solar module manufacturer), ENPH (Enphase microinverters), SEDG (SolarEdge inverters...
    ],
    "energy_storage":           [
        "FLNC",
        "EOSE",   # rationale: Eos — zinc-bromide long-duration energy storage (US-made; Cerberus IPP JV)
        "AMPX",   # rationale: Amprius silicon-anode lithium-ion batteries (cross-listed in ev_supply_chain)
        "QS",     # rationale: QuantumScape — solid-state lithium-metal battery technology (pre-commercial),
        "ENVX",   # rationale: energy_storage is the right fit — peer roster includes FLNC (Fluence Energy grid-storage systems integrator), EOSE (Eos Energy zinc-iron battery for grid storage), AMPX (Amprius...
        "CSIQ",   # rationale: Cross-listing added: CSIQ's battery energy storage business (e-STORAGE + SolBank + EP Cube + storage projects) has scaled to material revenue contributor with +50%+ growth + GWh...
        "ENPH",   # rationale: Cross-listing added: ENPH's IQ Battery residential energy storage business has scaled to material revenue contributor (~25-30% of revenue + 2x+ shipment growth YoY) -- energy_st...
    ],
    "fuel_cells_hydrogen":      [
        "PLUG",
        "FCEL",   # rationale: FuelCell Energy — stationary fuel cells (utility + industrial)
        "BE",     # rationale: Bloom Energy — solid-oxide fuel cells (cross-listed in power_demand_for_ai / datacenter_buildout),
        "BLDP",   # rationale: fuel_cells_hydrogen is the exact fit — peer roster is PLUG (Plug Power), FCEL (FuelCell Energy), BE (Bloom Energy). All four are pure-play hydrogen / fuel cell pure-plays at sub...
    ],

    # ═══════════════════════════════════════════════════
    #  EV & AUTONOMY
    # ═══════════════════════════════════════════════════
    "ev_makers":                [
        "TSLA", "RIVN", "LCID",
        "F",      # rationale: Ford — Lightning + Mach-E + Maverick Hybrid (legacy OEM EV exposure)
        "EZGO",   # rationale: EZGO Technologies — Chinese e-bikes + light EVs (smaller-cap)
    ],
    "ev_supply_chain":          [
        "BWA",
        "NVTS",   # rationale: Navitas GaN power conversion for EVs (cross-listed in ai_compute)
        "MOD",    # rationale: Modine thermal-management content for EV batteries (cross-listed)
        "APTV",   # rationale: Aptiv high-voltage architecture + ADAS / autonomous platforms
        "ALB",    # rationale: Albemarle lithium for EV batteries (cross-listed in critical_minerals)
        "AMPX",   # rationale: Amprius silicon-anode lithium-ion batteries for high-energy applications
        "ST",     # rationale: Sensata — sensors + protection for EV powertrain / battery monitoring
        "ALSN",   # rationale: Allison Transmission — commercial-vehicle EV transmissions / propulsion,
        "GTX",   # rationale: ev_supply_chain is the right fit — peer roster includes BWA (BorgWarner — DIRECT peer; same dual identity of legacy ICE + EV/E-powertrain transition supplier), NVTS (Navitas — G...
        "LAC",   # rationale: Cross-listing critical_minerals + ev_supply_chain captures both narrative drivers. critical_minerals roster (MP/USAR/UAMY/CRML/UUUU/LEU/ALB/HBM/UEC) includes ALB (Albemarle — DI...
        "VGNT",   # rationale: auto_parts_tech is the parent fit -- APTV (Versigent's former parent) is in this theme along with BWA, LKQ, MOD, ALSN, ST + recent placements VC/MBLY/GT/GTX/AAP. Versigent IS th...
        "ALGM",   # rationale: BUCKET C -- proposed REMOVAL from ai_compute (incorrect placement; ALGM is a fabless sensor/power-IC company for automotive + industrial markets, NOT an AI compute/processor chi...
    ],
    "autonomous_mobility":      [
        "TSLA",   # rationale: FSD + Robotaxi initiative (cross-listed in ev_makers / mag7)
        "AUR",    # rationale: Aurora Innovation — driverless trucks (Dallas↔Houston commercial)
        "IOT",    # rationale: Samsara — fleet IoT + AI for connected operations (cross-listed)
        "LYFT",   # rationale: Lyft — ride-hailing platform positioned for robotaxi distribution
        "JOBY",   # rationale: Joby Aviation — eVTOL air-mobility (urban air taxi)
        "ACHR",   # rationale: Archer Aviation — eVTOL air-mobility platform + military pivot,
        "AIIO",   # rationale: autonomous_mobility is the strongest peer-theme fit — eVTOL development (parallels JOBY + ACHR) + autonomous driving systems via Robus subsidiary (parallels TSLA's claim + AUR)....
        "AMBA",   # rationale: ai_compute is the primary placement — AMBA is an AI-inference SoC designer alongside MRVL/AVGO/NVDA in the existing roster. Direct theme fit on the 80% edge-AI revenue mix. Cros...
        "MBLY",   # rationale: Cross-listing autonomous_mobility + auto_parts_tech captures both narrative drivers exactly the dual-identity case. autonomous_mobility roster (TSLA/AUR/IOT/LYFT/JOBY/ACHR) is t...
    ],

    # ═══════════════════════════════════════════════════
    #  CRYPTO REVIVAL
    # ═══════════════════════════════════════════════════
    "crypto_platforms":         [
        "COIN", "HOOD",
        "CRCL",   # rationale: Circle Internet Group — USDC stablecoin issuer; OCC conditional trust-bank charter
        "GLXY",   # rationale: Galaxy Digital — digital-asset trading/asset-management + crypto-DC infrastructure
        "DXYZ",   # rationale: Destiny Tech100 — pre-IPO tech basket including OpenAI/SpaceX/crypto-adjacent privates,
        "BLSH",   # rationale: crypto_platforms is the exact fit — peer roster includes COIN (Coinbase, the dominant US-listed crypto exchange + custody platform), HOOD (Robinhood crypto trading), CRCL (Circl...
        "ETOR",   # rationale: Cross-listing fintech_disruptors + crypto_platforms captures both narrative drivers — exactly the dual-identity case. fintech_disruptors (SOFI/HOOD/AFRM/UPST/DAVE/LMND/CHYM/FIGR...
        "WT",   # rationale: Cross-listing captures the dual identity. alt_asset_managers is the core ETF + farmland alts business -- peer roster includes AB, APAM, BSIG, COHN, FHI, IVZ, JHG, LAZ, VRTS + re...
        "BMNR",   # rationale: BUCKET C -- proposed REMOVAL from bitcoin_treasury + crypto_miners (BMNR has fundamentally pivoted from BTC mining to ETH treasury -- 5.28M ETH is the dominant identity now, not...
    ],
    "bitcoin_treasury":         [
        "MSTR",
        "ASST",   # rationale: bitcoin_treasury contains MSTR (Strategy / MicroStrategy — the canonical Bitcoin treasury company) + BMNR (Bitmine). ASST is explicitly built as the public-market Bitcoin treasu...
    ],
    "crypto_miners":            [
        "MARA", "RIOT", "HUT", "CLSK",
        "CIFR",   # rationale: Cipher Mining — Bitcoin mining + HPC hosting pivot (cross-listed in ai_compute_clouds)
        "CORZ",   # rationale: Core Scientific — colocation + Bitcoin (CoreWeave customer; cross-listed in ai_compute_clouds)
        "WULF",   # rationale: TeraWulf — Bitcoin + AI data-center hosting (cross-listed in ai_compute_clouds)
        "IREN",   # rationale: IREN — Bitcoin pivot to AI ($9.7B MSFT deal; cross-listed in ai_compute_clouds),
        "SLNH",   # rationale: Cross-listing crypto_miners + ai_compute_clouds captures both narrative drivers — Soluna is essentially the pure-play example of the miner-pivot-to-AI-DC transition that defines...
        "BTDR",   # rationale: Cross-listing added: BTDR's Bitcoin self-mining + SEALMINER ASIC manufacturing remain the dominant revenue base (~80%+ of revenue) -- crypto_miners is a load-bearing existing-id...
        "DGXX",   # rationale: BUCKET C -- proposed REMOVAL from crypto_platforms (incorrect placement; DGXX is a Bitcoin miner + energy infrastructure + colocation operator, NOT a crypto exchange/wallet/trea...
        "HIVE",   # rationale: Cross-listing added: HIVE's Bitcoin mining + sale of digital currencies remains the dominant existing revenue stream while HPC + AI hosting pivot is emerging -- crypto_miners is...
        "KEEL",   # rationale: Cross-listing added: KEEL operates both Bitcoin mining + HPC/AI cloud hosting at owned data centers -- crypto_miners is a load-bearing existing-identity narrative alongside the ...
    ],

    # ═══════════════════════════════════════════════════
    #  FRONTIER AEROSPACE & ROBOTICS
    # ═══════════════════════════════════════════════════
    "drones":                   [
        "AVAV", "RCAT",
        "ONDS",   # rationale: Ondas Holdings — private wireless + counter-drone systems for autonomous ops
        "KTOS",   # rationale: Kratos Valkyrie CCA (Collaborative Combat Aircraft) program of record; cross-listed in defense_tech
        "RKLB",   # rationale: Rocket Lab — space launch + spacecraft (drone-adjacent for ISR; cross-listed in defense_tech/space_economy),
        "AVEX",   # rationale: Cross-listing defense_tech + drones is the right call. defense_tech contains the broader defense services + electronics peer set (LDOS/BAH/CACI services, KTOS/BWXT/AVAV hardware...
        "UMAC",   # rationale: drones is the exact fit -- peer roster includes AVAV (AeroVironment), RCAT (Red Cat), ONDS (Ondas), KTOS (Kratos), RKLB (Rocket Lab). UMAC sits with RCAT + ONDS as small-cap US ...
        "AMPX",   # rationale: Cross-listing added: AMPX revenue is primarily flowing to UAS + aviation drone applications -- DOD prioritizing US-made drones via Replicator + Executive Orders + Amprius's AV p...
    ],
    "space_economy":            [
        "RKLB", "ASTS",
        "LUNR",   # rationale: Intuitive Machines — lunar landers + space infrastructure / services
        "RDW",    # rationale: Redwire — critical space components / infrastructure for gov + commercial
        "DXYZ",   # rationale: Destiny Tech100 — pre-IPO basket with SpaceX exposure (cross-listed in crypto_platforms)
        "SATS",   # rationale: EchoStar — GEO satellite operator + spectrum holdings
        "VSAT",   # rationale: Viasat — broadband + government satellite communications
        "PL",     # rationale: Planet Labs — Earth-observation satellite constellation (PBC)
        "GSAT",   # rationale: Globalstar — mobile satellite services + Apple iPhone partnership
        "SPIR",   # rationale: Spire Global — subscription space-data / analytics (LEO constellation)
        "BKSY",   # rationale: BlackSky — high-revisit Earth-observation imagery
        "IRDM",   # rationale: Iridium — global LEO voice + data communications
        "FLY",    # rationale: Firefly Aerospace — small-launch + lunar lander programs
        "NASA",   # rationale: ETF — space-economy basket exposure
        "UFO",    # rationale: Procure Space ETF — space-economy basket exposure,
        "SIDU",   # rationale: space_economy is the exact fit — peer roster includes RKLB (Rocket Lab — DIRECT peer; smallsat launch + spacecraft manufacturing), ASTS (AST SpaceMobile), LUNR (Intuitive Machin...
        "VOYG",   # rationale: Cross-listing is exact -- defense_tech roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI, LDOS, BAH, CACI, SARO, HII + recent placements HXL/ESE/KOPN/LOA...
        "YSS",   # rationale: Cross-listing exact -- defense_tech roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI, LDOS, BAH, CACI, SARO, HII + recent placements HXL/ESE/KOPN/LOAR/L...
    ],
    "robotics_automation":      [
        "SYM",
        "CGNX",   # rationale: Cognex — machine-vision systems for factory + warehouse automation
        "TER",    # rationale: Teradyne — owns Universal Robots collaborative-robot business (cross-listed in testing/equipment)
        "AVAV",   # rationale: AeroVironment — military robotic systems (cross-listed in drones/defense_tech)
        "OUST",   # rationale: Ouster — lidar sensors for industrial + automotive autonomy
        "ZBRA",   # rationale: Zebra Technologies — automatic-identification + warehouse robotics
    ],

    # ═══════════════════════════════════════════════════
    #  DEFENSE & GEOPOLITICAL
    # ═══════════════════════════════════════════════════
    "defense_tech":             [
        "KTOS", "AVAV", "BWXT", "AXON", "RCAT", "ONDS",
        "OSK",    # rationale: Oshkosh — purpose-built military vehicles (tactical trucks, JLTV)
        "KRMN",   # rationale: Karman Holdings — mission-critical aerospace structures for defense/space
        "RKLB",   # rationale: Rocket Lab — launch + spacecraft for defense space architectures (cross-listed in space_economy)
        "TTMI",   # rationale: TTM Technologies — RF / microwave PCBs for defense radar + EW systems
        "LDOS",   # rationale: Leidos — defense IT and mission solutions (legacy services)
        "BAH",    # rationale: Booz Allen Hamilton — AI/digital consulting for DoD + intelligence
        "CACI",   # rationale: CACI International — gov IT and intel-mission services
        "SARO",   # rationale: StandardAero — aerospace engine MRO for military + commercial fleets,
        "HII", # rationale: auto-placed (Aerospace & Defense; military+electronic warfare),
        "AIR",   # rationale: defense_tech is the right primary placement — roster includes LDOS + BAH + CACI (defense services co's, direct peers to AAR Government Solutions), TTMI (defense electronics), KT...
        "AMSC",   # rationale: electrical_grid is the primary placement — Grid Solutions is 85% of revenue and roster includes ETN/VRT/GEV/GNRC/NRG (the grid infrastructure + power-electronics peer set). Cros...
        "AMTM",   # rationale: defense_tech is the exact right bucket — direct peers in the roster (LDOS/BAH/CACI/SARO) are also government services + engineering contractors. AMTM Global Engineering Solution...
        "ATRO",   # rationale: defense_tech is the better placement than airlines because ~40% of revenue is defense (Test Systems + military electronic systems + MOSA programs + drone power systems developme...
        "AVEX",   # rationale: Cross-listing defense_tech + drones is the right call. defense_tech contains the broader defense services + electronics peer set (LDOS/BAH/CACI services, KTOS/BWXT/AVAV hardware...
        "BELFB",   # rationale: Cross-listing defense_tech + datacenter_buildout captures both narrative drivers. defense_tech is the largest segment (55% of revenue) and peer to TTMI (defense electronics PCBs...
        "ESE",   # rationale: defense_tech is the right primary placement — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT, ONDS (Ondas drones), OSK (Oshkosh ...
        "HXL",   # rationale: defense_tech is the right primary placement — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN ...
        "KOPN",   # rationale: defense_tech is the right fit — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN (Karman Holdin...
        "LASR",   # rationale: Cross-listing defense_tech + ai_optics captures both narrative drivers exactly the dual-identity case. defense_tech (KTOS/AVAV/BWXT/AXON/RCAT/ONDS/OSK/KRMN/RKLB/TTMI) is the def...
        "LOAR",   # rationale: defense_tech is the exact fit — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN (Karman Holdin...
        "LPTH",   # rationale: defense_tech is the right fit — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN (Karman Holdin...
        "MRCY",   # rationale: defense_tech is the exact fit — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN (Karman Holdin...
        "NN",   # rationale: defense_tech is the closest fit — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon), RCAT/ONDS (drones), OSK (Oshkosh), KRMN (Karman Hold...
        "OSIS",   # rationale: defense_tech is the right primary placement — peer roster includes KTOS (Kratos), AVAV (AeroVironment), BWXT (BWX nuclear), AXON (Axon — DIRECT peer; security + public-safety te...
        "RAL",   # rationale: defense_tech is the closest fit given the dominant PacSci EMC + Qualitrol grid-monitoring + Hengstler-Dynapar industrial-safety identity in the Sensors & Safety Systems segment ...
        "VELO",   # rationale: defense_tech is the right fit -- peer roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI + recent placements HXL/ESE/KOPN/LOAR/LPTH/MRCY. VELO sits with K...
        "VOYG",   # rationale: Cross-listing is exact -- defense_tech roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI, LDOS, BAH, CACI, SARO, HII + recent placements HXL/ESE/KOPN/LOA...
        "VSEC",   # rationale: defense_tech is the right fit -- peer roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI, LDOS, BAH, CACI, SARO, HII + recent placements HXL/ESE/KOPN/LOAR...
        "VVX",   # rationale: Cross-listing matches the established LDOS / BAH / CACI pattern -- those three peers are dual-listed in defense_tech AND consulting_govt. V2X has identical dual identity: defens...
        "YSS",   # rationale: Cross-listing exact -- defense_tech roster includes KTOS, AVAV, BWXT, AXON, RCAT, ONDS, OSK, KRMN, RKLB, TTMI, LDOS, BAH, CACI, SARO, HII + recent placements HXL/ESE/KOPN/LOAR/L...
        "ACHR",   # rationale: Cross-listing added: ACHR's defense program with Anduril Industries has become a materially developed second narrative -- two 2025 strategic acquisitions, Karem Aircraft collabo...
        "ALSN",   # rationale: Cross-listing added: ALSN's defense segment FY25 revenue $267M (+26% YoY) + July 2025 NGET Phase 2 contract for next-generation electrified military transmission + the wheeled/t...
        "ATI",   # rationale: Cross-listing added: ATI's Aerospace & Defense segment is 68% of FY25 sales (well above prior 65% target) with defense-specific revenue +9% YoY Q1 2026 + producing 6 of 7 most a...
        "BBAI",   # rationale: Cross-listing added: BBAI's explicit Q1 2026 pivot to 'specialized defense + national security technology company' + Ask Sage federal agentic AI + Fincantieri defense-shipbuildi...
        "BKSY",   # rationale: Cross-listing added: BKSY is fundamentally a defense + national-security space-intelligence company -- US federal government + DOD + IC + international defense customers dominat...
        "CRS",   # rationale: Cross-listing added: CRS's aerospace + defense revenue 60-70% of total + aerospace + defense bookings +23% sequentially + multiple aerospace long-term agreements + jet engine + ...
        "FLY",   # rationale: Cross-listing added: FLY's defense + national security mission solutions + DOD Space Force responsive launch + Defense Innovation Unit awards + Lockheed Martin Alpha medium-lift...
        "JOBY",   # rationale: Cross-listing added: Joby's Logistics Air Mobility (LAM) + Defense Innovation Unit Agility Prime + US Air Force AFWERX + Edwards AFB military testing + Marine Corps + Army futur...
    ],
    "critical_minerals":        [
        "MP", "USAR",
        "UAMY",   # rationale: US Antimony — antimony + zeolite + precious metals (defense + tech industrial inputs)
        "CRML",   # rationale: Critical Metals — Greenland REE + Austrian lithium exploration/development
        "UUUU",   # rationale: Energy Fuels — uranium + REE separation at White Mesa (cross-listed in nuclear/uranium)
        "LEU",    # rationale: Centrus — uranium enrichment in the critical-minerals fuel-cycle bucket (cross-listed)
        "ALB",    # rationale: Albemarle — lithium for energy storage / EV batteries (critical-minerals overlap)
        "UEC",    # rationale: Uranium Energy — uranium pure-play (cross-listed in uranium_pure_plays/nuclear),
        "ALM",   # rationale: critical_minerals theme is built for exactly this — MP (rare earths), USAR (US Antimony), UAMY (antimony), CRML (rare earth+critical metals), UUUU (uranium). Tungsten is a recog...
        "LAC",   # rationale: Cross-listing critical_minerals + ev_supply_chain captures both narrative drivers. critical_minerals roster (MP/USAR/UAMY/CRML/UUUU/LEU/ALB/HBM/UEC) includes ALB (Albemarle — DI...
        "PPTA",   # rationale: Cross-listing critical_minerals + gold_silver_miners captures both narrative drivers. critical_minerals roster (MP/USAR/UAMY/CRML/UUUU/LEU/ALB/HBM/UEC) includes UAMY (United Sta...
    ],

    # ═══════════════════════════════════════════════════
    #  METALS & MINING
    # ═══════════════════════════════════════════════════
    "gold_silver_miners":       [
        "KGC", "EGO", "AG", "BTG", "PAAS", "CDE", "HL", "IAG", "AGI", "EQX",
        "WPM",    # rationale: Wheaton — royalty/streaming model (no operational risk, leverage to commodity price)
        "NUGT",   # rationale: Direxion 2x gold miners ETF (leveraged exposure)
        "JNUG",   # rationale: Direxion 2x junior gold miners ETF (high-beta exploration leverage)
        "GDXU",   # rationale: MicroSectors 3x gold miners ETN
        "SIL",    # rationale: Global X Silver Miners ETF (basket exposure)
        "SILJ",   # rationale: Amplify Junior Silver Miners ETF (junior leverage),
        "AUGO",   # rationale: gold_silver_miners is the primary theme — direct peer to KGC (Kinross — gold producer with LatAm ops), EGO (Eldorado — mid-tier gold), BTG (B2Gold), PAAS (Pan American Silver), ...
        "EXK",   # rationale: gold_silver_miners is the exact fit — peer roster includes KGC (Kinross), EGO (Eldorado), AG (First Majestic — DIRECT silver peer), BTG (B2Gold), PAAS (Pan American Silver — DIR...
        "FSM",   # rationale: gold_silver_miners is the exact fit — peer roster includes KGC (Kinross), EGO (Eldorado), AG (First Majestic silver), BTG (B2Gold), PAAS (Pan American Silver), CDE (Coeur Mining...
        "HYMC",   # rationale: gold_silver_miners is the right fit — peer roster includes KGC (Kinross), EGO (Eldorado), AG (First Majestic), BTG (B2Gold), PAAS (Pan American Silver), CDE (Coeur Mining), HL (...
        "PPTA",   # rationale: Cross-listing critical_minerals + gold_silver_miners captures both narrative drivers. critical_minerals roster (MP/USAR/UAMY/CRML/UUUU/LEU/ALB/HBM/UEC) includes UAMY (United Sta...
        "SSRM",   # rationale: gold_silver_miners is the exact fit — peer roster includes KGC (Kinross), EGO (Eldorado), AG (First Majestic silver), BTG (B2Gold), PAAS (Pan American Silver), CDE (Coeur Mining...
        "SVM",   # rationale: gold_silver_miners is the exact fit — peer roster includes KGC (Kinross), EGO (Eldorado), AG (First Majestic — DIRECT silver-focused peer), BTG (B2Gold), PAAS (Pan American Silv...
    ],
    "industrial_metals":        [
        "FCX",    # rationale: Freeport-McMoRan — copper + gold mining (electrification + AI grid build copper demand)
        "SCCO",   # rationale: Southern Copper — pure-play copper producer with low-cost Peruvian + Mexican mines
        "CLF",    # rationale: Cleveland-Cliffs — US steel + iron ore (Section 232 tariff beneficiary)
        "AA",     # rationale: Alcoa — bauxite/alumina/aluminum (Section 232 50% aluminum tariff)
        "ATI",    # rationale: ATI — specialty alloys + complex components (aerospace + defense materials)
        "CRS",    # rationale: Carpenter Technology — specialty premium alloys for aerospace + defense,
        "CENX",   # rationale: industrial_metals is the exact fit — peer roster includes FCX (Freeport copper), SCCO (Southern Copper), CLF (Cleveland-Cliffs steel), AA (Alcoa — direct peer; the other major U...
        "CSTM",   # rationale: industrial_metals is the right fit — peer roster includes FCX (Freeport copper), SCCO (Southern Copper), CLF (Cleveland-Cliffs steel), AA (Alcoa — direct peer; primary + value-a...
        "KALU",   # rationale: industrial_metals is the exact fit — peer roster includes FCX (Freeport copper), SCCO (Southern Copper), CLF (Cleveland-Cliffs steel), AA (Alcoa primary + value-add aluminum — D...
        "MTRN",   # rationale: industrial_metals is the right fit — peer roster includes FCX (Freeport copper), SCCO (Southern Copper), CLF (Cleveland-Cliffs steel), AA (Alcoa aluminum), ATI (Allegheny Techno...
        "HBM",   # rationale: BUCKET C -- proposed primary-theme CHANGE from critical_minerals to industrial_metals. HBM is fundamentally a base-metals mining company (copper + gold + zinc + molybdenum + sil...
    ],

    # ═══════════════════════════════════════════════════
    #  CYBERSECURITY & ENTERPRISE SOFTWARE
    # ═══════════════════════════════════════════════════
    "cybersecurity":            [
        "PANW", "CRWD", "FTNT", "ZS", "OKTA",
        "NET",    # rationale: Cloudflare zero-trust + edge security (cross-listed in ai_networking/cloud_infra)
        "S",      # rationale: SentinelOne endpoint security platform
        "RBRK",   # rationale: Rubrik data-security / cyber-resilience platform
        "CHKP",   # rationale: Check Point Software — perimeter + cloud security
        "GEN",    # rationale: Gen Digital — Norton + LifeLock consumer cyber-safety
        "AKAM",   # rationale: Akamai web security + DDoS protection (cross-listed in ai_networking/cloud_infra)
        "BB",     # rationale: BlackBerry — cybersecurity + IoT platforms (Cylance/QNX)
        "FSLY",   # rationale: Fastly edge cloud / security (cross-listed in cloud_infra),
        "QLYS", # rationale: auto-placed (Software - Infrastructure; cyber+cybersecurity)
        "TENB", # rationale: auto-placed (Software - Infrastructure; cyber+cybersecurity),
        "CVLT",   # rationale: cybersecurity is the right fit — peer roster includes PANW (Palo Alto), CRWD (CrowdStrike), FTNT (Fortinet), ZS (Zscaler), OKTA, NET (Cloudflare security), S (SentinelOne), RBRK...
        "VRNS",   # rationale: cybersecurity is the exact fit -- peer roster includes PANW (Palo Alto), CRWD (CrowdStrike), FTNT (Fortinet), ZS (Zscaler), OKTA, NET (Cloudflare), S (SentinelOne), RBRK (Rubrik...
        "YOU",   # rationale: Cross-listing captures the dual identity. cybersecurity captures the identity-verification + KYC + workforce-identity B2B platform (CLEAR1 + Sora ID + Mount Sinai/CMS/Ochsner co...
    ],
    "data_devops_platforms":    [
        "DDOG", "MDB", "SNOW",
        "DT",     # rationale: Dynatrace — full-stack observability for cloud + AI workloads
        "GTLB",   # rationale: GitLab — end-to-end DevSecOps platform
        "FROG",   # rationale: JFrog — software supply-chain artifact + binary management
        "TEAM",   # rationale: Atlassian — Jira/Confluence collaboration + DevOps tooling
        "DBX",    # rationale: Dropbox — content collaboration + AI-powered productivity layer (cross-listed in cloud_infra)
        "ORCL",   # rationale: Oracle — enterprise database + cloud infrastructure (OCI; second-source vs hyperscalers),
        "OTEX", # rationale: auto-placed (Software - Application; devops+observability),
        "ESTC",   # rationale: data_devops_platforms is the right fit — peer roster includes DDOG (Datadog observability — DIRECT peer in observability + AI ops), MDB (MongoDB document DB — DIRECT peer as dev...
        "TDC",   # rationale: data_devops_platforms is the right fit — peer roster includes DDOG (Datadog), MDB (MongoDB), SNOW (Snowflake — DIRECT competitor + peer; enterprise data warehouse + AI platform)...
    ],
    "cloud_infra":              [
        "NET", "FSLY", "AKAM",
        "NTNX",   # rationale: Nutanix Cloud Platform — hybrid multicloud + GPT-in-a-Box for enterprise AI
        "RXT",    # rationale: Rackspace FAIR multi-cloud AI hosting (cross-listed in ai_compute_clouds)
        "DBX",    # rationale: Dropbox — collaboration cloud (cross-listed in data_devops_platforms)
        "DOCN",   # rationale: DigitalOcean — SMB-focused cloud + agentic inference (cross-listed in ai_compute_clouds)
        "GDDY",   # rationale: GoDaddy — cloud-based domains + SMB website/commerce stack
        "TWLO",   # rationale: Twilio customer-engagement platform (cloud comms APIs),
        "BOX",   # rationale: cloud_infra is the right primary placement — peer roster includes DBX (Dropbox, direct peer in cloud file/content sync), NET (Cloudflare edge), FSLY (Fastly CDN), AKAM (Akamai C...
        "CALX",   # rationale: cloud_infra is the closest fit by current trajectory — peer roster (NET / FSLY / AKAM / NTNX / RXT / DBX / DOCN / GDDY / TWLO / MSFU) is a mix of cloud platforms + network/CDN h...
        "FIVN",   # rationale: cloud_infra is the closest fit — peer roster includes TWLO (Twilio — DIRECT peer; CPaaS / contact-center adjacent), NET (Cloudflare edge), FSLY (Fastly CDN), AKAM (Akamai), NTNX...
        "RNG",   # rationale: cloud_infra is the right fit — peer roster includes NET (Cloudflare), FSLY (Fastly), AKAM (Akamai), NTNX (Nutanix), RXT (Rackspace), DBX (Dropbox), DOCN (DigitalOcean), GDDY (Go...
    ],
    "productivity_saas":        [
        "NOW", "WDAY", "MNDY", "HUBS",
        "TEAM",   # rationale: Atlassian (cross-listed in data_devops_platforms; productivity overlap)
        "INTU",   # rationale: Intuit — QuickBooks + TurboTax + Credit Karma (SMB/consumer financial productivity)
        "DOCU",   # rationale: DocuSign — e-signature + agreement-management workflows
        "ZM",     # rationale: Zoom AI-first work platform (video + AI Companion)
        "FIG",    # rationale: Figma — collaborative design platform (recent IPO)
        "KVYO",   # rationale: Klaviyo — e-commerce marketing automation SaaS (cross-listed in ad_tech_marketing),
        "ASAN",   # rationale: productivity_saas is the textbook fit. Roster includes NOW (ServiceNow workflow) + WDAY (HCM productivity) + MNDY (Monday.com — direct ASAN competitor) + HUBS (HubSpot marketing...
        "CRM",   # rationale: Cross-listing productivity_saas + ai_apps_platforms captures both narrative drivers — exactly the MRVL-style multi-theme pattern Dan endorses. productivity_saas (NOW/WDAY/MNDY/H...
        "FRSH",   # rationale: productivity_saas is the right fit — peer roster includes NOW (ServiceNow — DIRECT peer in ITSM/ESM; Freshservice is the SMB-and-mid-market Freshworks alternative), WDAY (Workda...
        "NAVN",   # rationale: productivity_saas is the right fit — peer roster includes NOW (ServiceNow), WDAY (Workday HR), MNDY (Monday), HUBS (HubSpot), TEAM (Atlassian), INTU (Intuit — DIRECT peer; SMB f...
        "PCTY",   # rationale: productivity_saas is the exact fit — peer roster includes NOW (ServiceNow), WDAY (Workday — DIRECT enterprise-HCM peer), MNDY (Monday), HUBS (HubSpot), TEAM (Atlassian), INTU (I...
        "PEGA",   # rationale: Cross-listing productivity_saas + ai_apps_platforms captures both narrative drivers, parallel to CRM (Salesforce) just-placed in batch 8 with the same dual-identity logic. produ...
        "WK",   # rationale: productivity_saas captures cross-industry horizontal software for the CFO office -- WK serves every industry vertical with the same financial-reporting + GRC + ESG modules. Rost...
    ],
    "vertical_saas":            [
        "TOST", "VEEV", "GWRE", "SHOP",
        "PCOR",   # rationale: Procore — cloud-based construction management software
        "TYL",    # rationale: Tyler Technologies — vertical software for state/local government
        "DOCS",   # rationale: Doximity — digital platform for medical professionals (vertical SaaS for healthcare)
        "PAYC",   # rationale: Paycom — vertical SaaS for HCM / payroll,
        "APPF",   # rationale: vertical_saas is the textbook fit. Roster contains TOST (restaurant vertical SaaS) + VEEV (life sciences) + GWRE (insurance) + SHOP (e-commerce platform) + PCOR (construction) —...
        "BSY",   # rationale: vertical_saas is the right fit — peer roster includes TOST (Toast — restaurant POS), VEEV (Veeva — life-sciences CRM), GWRE (Guidewire — insurance core systems), SHOP (Shopify —...
        "CCC",   # rationale: vertical_saas is the exact fit — peer roster includes GWRE (Guidewire — insurance core systems, the closest direct peer; both are insurance-vertical SaaS network platforms), VEE...
        "MANH",   # rationale: vertical_saas is the right fit — peer roster includes TOST (Toast restaurant POS), VEEV (Veeva life sciences), GWRE (Guidewire insurance), SHOP (Shopify commerce), PCOR (Procore...
        "NCNO",   # rationale: vertical_saas is the exact fit — peer roster includes TOST (Toast restaurant POS), VEEV (Veeva life sciences), GWRE (Guidewire insurance — DIRECT peer; nCino IS the banking equi...
        "QTWO",   # rationale: vertical_saas is the exact fit — peer roster includes TOST (Toast restaurant POS), VEEV (Veeva life sciences), GWRE (Guidewire insurance), SHOP (Shopify commerce), PCOR (Procore...
        "TRMB",   # rationale: vertical_saas is the right fit -- peer roster includes TOST (Toast), VEEV (Veeva), GWRE (Guidewire), SHOP, PCOR (Procore -- DIRECT construction SaaS peer), TYL (Tyler), DOCS, PA...
        "TTAN",   # rationale: vertical_saas is the exact fit -- peer roster includes TOST (Toast restaurant POS -- DIRECT peer in vertical SaaS for skilled-services-adjacent industry), VEEV (Veeva), GWRE (Gu...
        "WAY",   # rationale: vertical_saas is the exact fit -- peer roster includes TOST (restaurants), VEEV (Veeva Systems -- DIRECT peer in healthcare/life-sciences-vertical SaaS), GWRE (insurance), SHOP ...
    ],
    "ad_tech_marketing":        [
        "APP", "TTD",
        "RDDT",   # rationale: Reddit — data + ads (cross-listed in social_media; high-signal LLM training data licensing)
        "PINS",   # rationale: Pinterest visual search + Media Network Connect non-endemic ads (cross-listed in social_media)
        "SNAP",   # rationale: Snap programmatic ad platform (cross-listed in social_media)
        "ZETA",   # rationale: Zeta Global omnichannel marketing cloud (cross-listed in ai_apps_platforms)
        "HUBS",   # rationale: HubSpot CRM + marketing hub (cross-listed in productivity_saas)
        "KVYO",   # rationale: Klaviyo e-commerce email/SMS marketing (cross-listed in productivity_saas),
        "BRZE",   # rationale: ad_tech_marketing is the right fit — peer roster includes APP (AppLovin mobile-ad monetization), TTD (The Trade Desk DSP), RDDT/PINS/SNAP (social ad inventory owners), ZETA (Zet...
        "GTM",   # rationale: ad_tech_marketing is the right fit — peer roster includes APP (AppLovin), TTD (Trade Desk), RDDT/PINS/SNAP (social ad inventory), ZETA (Zeta Global — direct peer in data-driven ...
    ],

    # ═══════════════════════════════════════════════════
    #  HEALTHCARE NARRATIVES
    # ═══════════════════════════════════════════════════
    "medtech_devices_consumer_health":  [
        "PODD",   # rationale: Insulet — Omnipod patch pumps + Type 2 pivotal trial in 2026
        "DXCM",   # rationale: Dexcom — CGM (G7 15-Day rollout + Stelo OTC; 11-13% revenue growth guide)
        "GKOS",   # rationale: Glaukos — minimally invasive glaucoma + corneal devices (highest R&D intensity)
        "TMDX",   # rationale: TransMedics — Organ Care System (OCS) for transplant logistics
        "ALGN",   # rationale: Align Technology — Invisalign clear aligners (consumer-orthodontic SaaS-like)
        "GMED",   # rationale: Globus Medical — spine + orthopedic implants (NuVasive merger integration)
        "BAX",    # rationale: Baxter — broad hospital + renal-care portfolio
        "ZTS",    # rationale: Zoetis — animal health (companion + livestock pharmaceuticals)
        "ELAN",   # rationale: Elanco Animal Health — companion + production animal pharmaceuticals
        "MDLN",   # rationale: Medline — med-surg products distribution (recent IPO; private-equity exit),
        "ATEC", # rationale: auto-placed (Medical Devices; implant)
        "AXGN", # rationale: auto-placed (Medical Devices; orthopedic+implant)
        "HAE", # rationale: auto-placed (Medical Devices; medical device+orthopedic)
        "ITGR", # rationale: auto-placed (Medical Devices; medical device+orthopedic)
        "LIVN", # rationale: auto-placed (Medical Devices; medical device+implant)
        "NVST", # rationale: auto-placed (Medical Instruments & Supplies; implant)
        "PRCT", # rationale: auto-placed (Medical Devices; surgical robot)
        "UFPT", # rationale: auto-placed (Medical Devices; medical device+orthopedic)
        "XRAY", # rationale: auto-placed (Medical Instruments & Supplies; medical device+implant),
        "HNGE",   # rationale: medtech_devices_consumer_health is the closest fit — peer roster includes PODD (Insulet diabetes), DXCM (Dexcom CGM), GKOS (Glaukos eye), TMDX (TransMedics), ALGN (Align Tech), ...
        "INSP",   # rationale: medtech_devices_consumer_health is the exact fit — peer roster includes PODD (Insulet insulin pump), DXCM (Dexcom CGM), GKOS (Glaukos eye implant), TMDX (TransMedics organ-prese...
        "IRTC",   # rationale: medtech_devices_consumer_health is the right fit — peer roster includes PODD (Insulet insulin pump — DIRECT peer in wearable medical device + recurring revenue), DXCM (Dexcom CG...
        "MMSI",   # rationale: medtech_devices_consumer_health is the exact fit — peer roster includes PODD (Insulet), DXCM (Dexcom), GKOS (Glaukos), TMDX (TransMedics), ALGN (Align Tech), GMED (Globus Medica...
        "NOVT",   # rationale: medtech_devices_consumer_health is the closest fit — peer roster includes PODD (Insulet), DXCM (Dexcom), GKOS (Glaukos), TMDX (TransMedics), ALGN (Align Tech), GMED (Globus Medi...
        "PBH",   # rationale: medtech_devices_consumer_health is the closest fit — peer roster includes PODD (Insulet), DXCM (Dexcom), GKOS (Glaukos), TMDX (TransMedics), ALGN (Align Tech), GMED (Globus Medi...
        "TNDM",   # rationale: medtech_devices_consumer_health is the exact fit -- peer roster includes PODD (Insulet -- DIRECT competitor; insulin pump pure-play), DXCM (Dexcom CGM -- adjacent technology int...
        "HIMS",   # rationale: BUCKET C -- proposed primary-theme CHANGE from biotech to medtech_devices_consumer_health. HIMS is fundamentally a DTC telehealth + consumer-health platform + subscription-pharm...
    ],
    "diagnostics_tools":        [
        "GH", "NTRA", "ILMN",
        "RGEN",   # rationale: Repligen — bioprocessing consumables / filtration for biologics manufacturing
        "TECH",   # rationale: Bio-Techne — reagents / proteins / antibody tools for life-science research
        "IQV",    # rationale: IQVIA — clinical research organization + healthcare data analytics
        "ICLR",   # rationale: ICON — global clinical research organization (CRO)
        "CRL",    # rationale: Charles River Labs — preclinical CRO + research models / animal services
        "BIO",    # rationale: Bio-Rad Laboratories — life-science research instruments + clinical diagnostics
        "WAT",    # rationale: Waters — analytical instruments (mass spec, chromatography) for pharma/QC
        "MTD",    # rationale: Mettler-Toledo — precision instruments + balances / lab automation
        "RVTY",   # rationale: Revvity (ex-PerkinElmer) — life-science tools + diagnostics
        "ARE",    # rationale: Alexandria Real Estate — life-science REIT (lab + biotech property),
        "BLLN", # rationale: auto-placed (Diagnostics & Research; molecular diagnostic+liquid biopsy),
        "AVTR",   # rationale: diagnostics_tools captures life-sciences tools + lab equipment + diagnostics infrastructure. Roster includes ILMN (Illumina sequencing instruments) + RGEN (Repligen — bioprocess...
        "BRKR",   # rationale: diagnostics_tools is the exact fit — peer roster includes BIO (Bio-Rad — direct peer; clinical diagnostics + life science research; same scientific-instruments business model), ...
        "IDXX",   # rationale: diagnostics_tools is the exact fit — peer roster includes GH (Guardant Health oncology), NTRA (Natera prenatal/oncology), ILMN (Illumina sequencing), RGEN (Repligen bioprocessin...
        "QUCY",   # rationale: diagnostics_tools is the closest fit — peer roster includes GH (Guardant Health oncology liquid biopsy — most-comparable peer; molecular cancer diagnostics platform), NTRA (Nate...
        "TWST",   # rationale: diagnostics_tools is the exact fit -- peer roster includes ILMN (Illumina -- DIRECT peer in NGS), RGEN (Repligen bioprocessing -- adjacent peer), TECH (Bio-Techne), IQV, ICLR, C...
        "TXG",   # rationale: diagnostics_tools is the exact fit -- peer roster includes ILMN (Illumina), RGEN (Repligen), TECH (Bio-Techne), IQV, ICLR, CRL, BIO, WAT + recent placements BRKR/IDXX/TWST. TXG ...
        "WGS",   # rationale: diagnostics_tools is the exact fit -- WGS is pure-play genomic diagnostics. Roster includes ILMN (the sequencer-platform parent), NTRA, EXAS, GH, CDNA, NVTA, FLGT, IDXX + recent...
        "CAI",   # rationale: BUCKET C -- proposed primary-theme CHANGE from biotech to diagnostics_tools. CAI is a precision-oncology molecular DIAGNOSTICS + AI platform company -- they do NOT develop drugs...
    ],
    "managed_care":             [
        "HUM", "CNC", "OSCR", "MOH",
        "UHS",    # rationale: Universal Health Services — acute-care + behavioral health hospitals (provider, not payor)
        "ALHC",   # rationale: Alignment Healthcare — Medicare Advantage tech-driven payor
        "THC",    # rationale: Tenet Healthcare — diversified hospital operator (provider side)
        "DVA",    # rationale: DaVita — kidney dialysis services (provider; Berkshire Hathaway holding)
        "BTSG",   # rationale: BrightSpring Health Services — home + community health (LTC + specialty pharmacy)
        "OPCH",   # rationale: Option Care Health — home + alternate-site infusion services,
        "ACHC",   # rationale: managed_care label is 'payors & providers' and the roster already includes hospital operators: UHS (Universal Health Services — direct peer, also operates behavioral health hosp...
        "LFST",   # rationale: managed_care is the right fit — peer roster includes HUM (Humana payor), CNC (Centene payor), OSCR (Oscar Health payor), MOH (Molina Healthcare payor), UHS (Universal Health Ser...
        "RDNT",   # rationale: managed_care is the right fit — peer roster includes HUM (Humana payor), CNC (Centene payor), OSCR (Oscar Health payor), MOH (Molina payor), UHS (Universal Health Services — DIR...
    ],

    # ═══════════════════════════════════════════════════
    #  FINTECH & FINANCIALS
    # ═══════════════════════════════════════════════════
    "fintech_disruptors":       [
        "SOFI", "HOOD", "AFRM", "UPST",
        "DAVE",   # rationale: Dave — neobank consumer financial services (cash advance + checking)
        "LMND",   # rationale: Lemonade — AI-powered consumer insurance
        "CHYM",   # rationale: Chime — digital consumer banking (~10.2M active members)
        "FIGR",   # rationale: Figure — blockchain-based home equity + lending (HELOC marketplace)
        "RKT",    # rationale: Rocket Companies — digital mortgage / real estate / personal finance
        "GRAB",   # rationale: Grab — Southeast Asia super-app (mobility + delivery + fintech)
        "BFH", # rationale: auto-placed (Credit Services; digital payments),
        "BILL",   # rationale: fintech_disruptors captures digital-first SMB/consumer financial services. Roster includes SOFI (consumer banking) + HOOD (retail brokerage) + AFRM (BNPL) + UPST (consumer credi...
        "BULL",   # rationale: fintech_disruptors is the exact fit — peer roster includes HOOD (Robinhood, direct peer; US retail brokerage that also offers crypto trading), SOFI (consumer banking + brokerage...
        "ETOR",   # rationale: Cross-listing fintech_disruptors + crypto_platforms captures both narrative drivers — exactly the dual-identity case. fintech_disruptors (SOFI/HOOD/AFRM/UPST/DAVE/LMND/CHYM/FIGR...
        "KLAR",   # rationale: fintech_disruptors is the exact fit — peer roster includes SOFI (consumer banking), HOOD (retail brokerage), AFRM (Affirm — DIRECT peer in BNPL; same business model, similar siz...
        "LC",   # rationale: fintech_disruptors is the exact fit — peer roster includes SOFI (consumer banking — DIRECT peer; both have bank holding-company structure + digital channel), HOOD (retail broker...
        "PGY",   # rationale: fintech_disruptors is the exact fit — peer roster includes SOFI (consumer banking — a Pagaya partner customer too), HOOD (retail brokerage), AFRM (BNPL), UPST (Upstart — DIRECT ...
        "SEZL",   # rationale: fintech_disruptors is the exact fit — peer roster includes SOFI (consumer banking), HOOD (retail brokerage), AFRM (Affirm — DIRECT major BNPL peer; same business model + similar...
        "SLM",   # rationale: fintech_disruptors is the closest fit — peer roster includes SOFI (consumer banking — DIRECT student loans peer + similar bank-holding-company model), HOOD, AFRM, UPST (Upstart ...
        "UWMC",   # rationale: fintech_disruptors is the right fit -- peer roster includes SOFI (consumer banking), HOOD, AFRM, UPST, DAVE, LMND, CHYM, FIGR, RKT (Rocket Companies -- DIRECT mortgage peer; sam...
        "WU",   # rationale: fintech_disruptors captures the digital-pivot + stablecoin-rails narrative driving the stock today -- USDPT on Solana + Digital Asset Network + Branded Digital growth + Intermex...
        "INTR",   # rationale: Cross-listing added: INTR is fundamentally a digital banking + super-app + fintech-disruptor business model + Brazilian + cross-border customer base (similar to NU but smaller);...
        "WOK",   # rationale: Cohen Circle is a fintech-focused SPAC (Paya/Perella/INX track record), auto-placed medtech label was incorrect
    ],
    "financial_services_specialty": [
        "RYAN",   # rationale: Ryan Specialty — E&S wholesale broker (placed 93-94% of E&S premiums historically)
        "KNSL",   # rationale: Kinsale Capital — pure-play E&S underwriter for hard-to-place P&C risks
        "CPAY",   # rationale: Corpay — corporate / commercial payments specialty (fuel + lodging + cross-border)
        "GPN",    # rationale: Global Payments — payment-tech + software for merchants
        "RELY",   # rationale: Remitly — digital cross-border consumer remittances,
        "EEFT",   # rationale: financial_services_specialty is the right fit — peer roster includes RYAN (Ryan Specialty insurance), KNSL (Kinsale Capital specialty insurance), CPAY (Corpay payments — DIRECT ...
        "ERIE",   # rationale: financial_services_specialty is the right fit — peer roster includes RYAN (Ryan Specialty insurance brokerage), KNSL (Kinsale Capital specialty insurance — direct peer in P&C sp...
        "FOUR",   # rationale: financial_services_specialty is the right fit — peer roster includes RYAN (Ryan Specialty insurance), KNSL (Kinsale Capital specialty insurance), CPAY (Corpay — B2B payments pro...
        "HQY",   # rationale: financial_services_specialty is the closest fit — peer roster includes RYAN (Ryan Specialty insurance brokerage), KNSL (Kinsale Capital specialty insurance), CPAY (Corpay paymen...
        "JXN",   # rationale: financial_services_specialty is the closest fit — peer roster includes RYAN (Ryan Specialty insurance brokerage), KNSL (Kinsale Capital specialty insurance), CPAY (Corpay), GPN ...
        "MIAX",   # rationale: financial_services_specialty is the closest fit — peer roster includes RYAN (specialty insurance broker), KNSL (specialty insurance), CPAY (payments), GPN (Global Payments), REL...
        "MRX",   # rationale: financial_services_specialty is the right fit — peer roster includes RYAN (specialty insurance broker), KNSL (specialty insurance), CPAY (payments), GPN (Global Payments), RELY ...
        "SNEX",   # rationale: financial_services_specialty is the right fit — peer roster includes RYAN (specialty insurance broker), KNSL (specialty insurance), CPAY (Corpay payments), GPN (Global Payments)...
        "WEX",   # rationale: financial_services_specialty is the right fit -- WEX is a mature specialty-finance/payments scale player with 40+ year operating history + three structured B2B segments + float ...
        "HRB",   # rationale: BUCKET C -- proposed primary-theme CHANGE from fintech_disruptors to financial_services_specialty. H&R Block is fundamentally a tax preparation services + DIY tax software firm ...
        "LPLA",   # rationale: BUCKET C -- proposed primary-theme CHANGE from alt_asset_managers to financial_services_specialty. LPL is fundamentally an independent broker-dealer + RIA custodian + wealth man...
    ],
    "alt_asset_managers":       [
        "TPG",    # rationale: TPG — diversified PE + credit + impact + real estate alternative asset manager
        "OWL",    # rationale: Blue Owl Capital — direct-lending + GP-stakes specialist (53% PC in fee-earning AUM)
        "ARES",   # rationale: Ares Management — private-credit-heavy alts manager (66% PC of fee-earning AUM)
        "CG",     # rationale: Carlyle Group — balanced PE / credit / real assets (least PC exposure among peers)
        "EVR",    # rationale: Evercore — independent investment banking + advisory (M&A / restructuring),
        "HLNE", # rationale: auto-placed (Asset Management; private equity)
        "STEP", # rationale: auto-placed (Asset Management; private equity),
        "AMG",   # rationale: alt_asset_managers theme contains TPG/OWL/ARES/CG/LPLA — all private markets / wealth management firms. AMG sits in the exact same narrative bucket: pivoting from traditional ac...
        "LAZ",   # rationale: alt_asset_managers is the right fit — peer roster includes TPG (TPG Inc), OWL (Blue Owl Capital), ARES (Ares Management), CG (Carlyle Group), LPLA (LPL Financial), EVR (Evercore...
        "MC",   # rationale: alt_asset_managers is the right fit — peer roster includes TPG, OWL, ARES, CG (Carlyle), LPLA (LPL Financial), EVR (Evercore — DIRECT peer; the other large-cap publicly-traded i...
        "WT",   # rationale: Cross-listing captures the dual identity. alt_asset_managers is the core ETF + farmland alts business -- peer roster includes AB, APAM, BSIG, COHN, FHI, IVZ, JHG, LAZ, VRTS + re...
    ],

    # ═══════════════════════════════════════════════════
    #  RESHORING, GRID, INFRASTRUCTURE, TRANSPORTS
    # ═══════════════════════════════════════════════════
    "reshoring_construction":   [
        "FIX",    # rationale: Comfort Systems — mechanical contractor; $12.45B backlog with 56% from advanced-tech / data center
        "PWR",    # rationale: Quanta Services — record $48.5B backlog (transmission + grid mod for AI/electrification)
        "MTZ",    # rationale: MasTec — $19B 18-month backlog (energy / utility / communications infra)
        "IESC",   # rationale: IES Holdings — integrated electrical + tech systems contractor
        "PRIM",   # rationale: Primoris Services — utility + civil infrastructure construction
        "MYRG",   # rationale: MYR Group — electrical construction (T&D + commercial/industrial)
        "AGX",    # rationale: Argan — EPC for natural-gas + renewables power plants
        "STRL",   # rationale: Sterling Infrastructure — e-infra + transportation + building
        "ACM",    # rationale: AECOM — professional infrastructure consulting / EPC
        "ECG",    # rationale: Everus Construction — electrical + mechanical contractor (MDU Resources spinoff)
        "J",      # rationale: Jacobs Solutions — infrastructure + advanced facilities engineering
        "TTEK",   # rationale: Tetra Tech — water + environmental + sustainable-infrastructure consulting
        "DY",     # rationale: Dycom — telecom + fiber specialty contracting (BEAD + AI-DC interconnect)
        "FLR",    # rationale: Fluor — global EPC for energy / chemicals / mining / govt
        "SUNB",   # rationale: Sunbelt Rentals — equipment rental into construction (Ashtead spinoff)
        "LGN",    # rationale: Legence — engineering / install / maintenance for mission-critical facilities,
        "CTRI",   # rationale: reshoring_construction is the exact fit — peer roster includes FIX (Comfort Systems mechanical contractor), PWR (Quanta Services — DIRECT peer, the largest US utility-infrastruc...
        "EME",   # rationale: reshoring_construction is the exact fit — peer roster includes FIX (Comfort Systems — DIRECT peer in mechanical contracting), PWR (Quanta Services — DIRECT peer in electrical T&...
        "MWH",   # rationale: Cross-listing reshoring_construction + solar captures both narrative drivers. reshoring_construction roster (FIX/PWR/MTZ/IESC/PRIM/MYRG/AGX/STRL/ACM/ECG + CTRI just-placed) is t...
        "ROAD",   # rationale: reshoring_construction is the exact fit — peer roster includes FIX (Comfort Systems mechanical contractor), PWR (Quanta Services electrical T&D), MTZ (MasTec — DIRECT peer; larg...
        "TPC",   # rationale: reshoring_construction is the exact fit -- peer roster includes FIX, PWR, MTZ (DIRECT large-cap infrastructure construction peer), IESC, PRIM (DIRECT infrastructure peer), MYRG,...
        "WSC",   # rationale: reshoring_construction captures construction + infrastructure end-market exposure -- WSC's largest customer segment is direct construction + infrastructure with secular tailwind...
    ],
    "electrical_grid":          [
        "ETN",    # rationale: Eaton power-management; expanding $1.2B capacity into transformer / switchgear shortage
        "VRT",    # rationale: Vertiv power/cooling 2026 sales +27-29% organic (cross-listed in datacenter_buildout)
        "GEV",    # rationale: GE Vernova gas turbines + transmission gear (cross-listed in power_demand_for_ai)
        "GNRC",   # rationale: Generac backup generators + grid-stability (cross-listed in power_demand_for_ai)
        "NRG",    # rationale: NRG Energy — retail + power generation (cross-listed in power_demand_for_ai)
        "POWL",   # rationale: Powell Industries custom switchgear sold out on grid + DC demand
        "AEIS",   # rationale: Advanced Energy precision power conversion (cross-listed in datacenter_buildout)
        "NVT",    # rationale: nVent electrical connection / thermal management (cross-listed)
        "RRX",    # rationale: Regal Rexnord — power transmission + motion control + climate solutions
        "SPXC",   # rationale: SPX Technologies — engineered HVAC / power-quality / detection solutions
        "FLS",    # rationale: Flowserve — pumps / seals / valves for power-gen + process industries
        "SEI",    # rationale: Solaris Energy Infrastructure — modular power generation equipment
        "FPS",    # rationale: Forgent Power Solutions — electrical distribution for DCs + grid (cross-listed),
        "AMSC",   # rationale: electrical_grid is the primary placement — Grid Solutions is 85% of revenue and roster includes ETN/VRT/GEV/GNRC/NRG (the grid infrastructure + power-electronics peer set). Cros...
        "BW",   # rationale: Cross-listing power_demand_for_ai + electrical_grid captures both narrative drivers exactly the way many peers in those themes are dual-placed. power_demand_for_ai roster includ...
        "ENS",   # rationale: electrical_grid is the right fit — peer roster includes ETN (Eaton), VRT (Vertiv — DIRECT peer; UPS + thermal management for data centers), GEV (GE Vernova), GNRC (Generac — DIR...
        "ITRI",   # rationale: electrical_grid is the closest fit — peer roster includes ETN (Eaton), VRT (Vertiv data center power + thermal), GEV (GE Vernova grid), GNRC (Generac backup power), NRG (NRG Ene...
        "PLPC",   # rationale: electrical_grid is the exact fit — peer roster includes ETN (Eaton — DIRECT peer in electrical infrastructure), VRT (Vertiv data center power), GEV (GE Vernova grid), GNRC (Gene...
        "PSIX",   # rationale: Cross-listing electrical_grid + power_demand_for_ai captures both narrative drivers. electrical_grid roster (ETN/VRT/GEV/GNRC/NRG/POWL/AEIS/NVT/RRX/SPXC + recent placements ENS/...
    ],
    "hvac_cooling":             [
        "AAON",   # rationale: AAON — precision cooling + $174.5M AI-DC liquid-cooling order (cross-listed)
        "MOD",    # rationale: Modine — $180M AI-DC cooling order, new WI plant scaling Airedale (cross-listed)
        "WSO",    # rationale: Watsco — largest North American HVAC/R distributor (residential + commercial),
        "MAIR",   # rationale: Cross-listing hvac_cooling + datacenter_buildout captures both narrative drivers. hvac_cooling roster (AAON/MOD/WSO) is the small but direct HVAC + cooling-equipment peer set — ...
    ],
    "building_products":        [
        "FBIN",   # rationale: Fortune Brands Innovations — home + security + digital products (Moen, Master Lock)
        "POOL",   # rationale: Pool Corporation — swimming-pool supplies + equipment distribution
        "FND",    # rationale: Floor & Decor — multi-channel flooring specialty retailer
        "WHR",    # rationale: Whirlpool — home appliances (R&R + new-build exposure)
        "IBP",    # rationale: Installed Building Products — insulation + finishing installations for builders
        "SWK",    # rationale: Stanley Black & Decker — power tools / hand tools / outdoor products
        "MHK",    # rationale: Mohawk Industries — flooring (carpet, tile, hardwood)
        "RH",     # rationale: RH — luxury home furnishings retailer + lifestyle brand
        "OC",     # rationale: Owens Corning — residential / commercial building products (insulation + roofing)
        "SITE",   # rationale: SiteOne Landscape Supply — wholesale distributor to landscape professionals
        "BLDR",   # rationale: Builders FirstSource — building materials supplier (lumber + manufactured components)
        "TSCO",   # rationale: Tractor Supply — rural-lifestyle retail (chicken-coop adjacency to building products)
        "CSL",    # rationale: Carlisle Companies — building-envelope products (roofing systems + insulation)
        "NAIL",   # rationale: Direxion 3x homebuilders + supplies ETF (leveraged sector exposure),
        "CVCO",   # rationale: building_products is the exact fit — peer roster includes FBIN (Fortune Brands cabinets + plumbing), POOL (Pool Corp), FND (Floor & Decor), WHR (Whirlpool appliances), IBP (Inst...
        "LPX",   # rationale: building_products is the exact fit — peer roster includes FBIN (Fortune Brands), POOL (Pool Corp), FND (Floor & Decor), WHR (Whirlpool), IBP (Installed Building Products), SWK (...
        "SGI",   # rationale: building_products is the right fit — peer roster includes FBIN (Fortune Brands cabinets + plumbing), POOL (Pool Corp), FND (Floor & Decor), WHR (Whirlpool appliances — DIRECT pe...
        "SKY",   # rationale: building_products is the exact fit — peer roster includes FBIN (Fortune Brands), POOL (Pool Corp), FND (Floor & Decor), WHR (Whirlpool), IBP (Installed Building Products), SWK (...
        "TREX",   # rationale: building_products is the exact fit -- peer roster includes FBIN, POOL, FND, WHR, IBP, SWK, MHK, RH, OC, SITE (DIRECT outdoor-living peer) + recent placements CVCO/LPX/SGI/SKY. T...
    ],
    "industrial_distribution":  [
        "WCC",    # rationale: Wesco — B2B distribution + logistics; DC sales +70% YoY now ~25% of total
        "ARW",    # rationale: Arrow Electronics — global electronic component + IT solutions distribution
        "CDW",    # rationale: CDW — IT solutions distribution to commercial / government / education
        "QXO",    # rationale: QXO — roofing + waterproofing + building products distribution (Brad Jacobs roll-up),
        "NSIT",   # rationale: industrial_distribution is the right fit — peer roster includes WCC (WESCO International electrical distribution), ARW (Arrow Electronics — DIRECT peer; IT/electronics distribut...
        "REZI",   # rationale: industrial_distribution is the right fit pre-separation given ADI Global Distribution is the much-larger ($4.8B) segment within Resideo — peer roster includes WCC (WESCO electri...
        "XMTR",   # rationale: Cross-listing matches the dual identity. ai_apps_platforms captures the AI-native marketplace differentiator -- AI instant-quoting + lead-time prediction + personalized pricing ...
    ],
    "transports":               [
        "KNX",    # rationale: Knight-Swift — diversified TL + LTL carrier (truckload-pressured, LTL buffer)
        "ODFL",   # rationale: Old Dominion — best-in-class LTL carrier; Feb '26 revenue/day -3.3% (early cyclical bottom)
        "SAIA",   # rationale: Saia — LTL carrier in expansion mode (new terminals)
        "XPO",    # rationale: XPO — LTL with AI-driven route optimization; first YoY tonnage gain in 18+ months Feb '26
        "CHRW",   # rationale: C.H. Robinson — asset-light freight brokerage + logistics
        "FTAI",   # rationale: FTAI Aviation — engine leasing + MRO (CFM56 cycle play)
        "CAR",    # rationale: Avis Budget — car + truck rental (cyclical transport-services exposure),
        "ARCB", # rationale: auto-placed (Trucking; trucking+freight transportation)
        "MATX", # rationale: auto-placed (trucking+freight transportation)
        "WERN", # rationale: auto-placed (Trucking; logistics services),
        "GXO",   # rationale: transports is the right fit — peer roster includes KNX (Knight-Swift trucking), ODFL (Old Dominion LTL), SAIA (Saia LTL), XPO (XPO — DIRECT spin-off parent; both share the post-...
        "HTZ",   # rationale: transports is the right fit — peer roster includes KNX (Knight-Swift), ODFL (Old Dominion), SAIA (Saia LTL), XPO (XPO), CHRW (CH Robinson), FTAI (FTAI Aviation leasing), CAR (Av...
        "INSW",   # rationale: transports is the closest fit — peer roster includes KNX (Knight-Swift trucking), ODFL (Old Dominion), SAIA (Saia LTL), XPO (XPO), CHRW (CH Robinson), FTAI (FTAI Aviation leasin...
        "RXO",   # rationale: transports is the exact fit — peer roster includes KNX (Knight-Swift trucking), ODFL (Old Dominion LTL), SAIA (Saia LTL), XPO (XPO — DIRECT spin-off parent; same XPO/GXO/RXO thr...
    ],

    # ═══════════════════════════════════════════════════
    #  ENERGY (TRADITIONAL) & MATERIALS
    # ═══════════════════════════════════════════════════
    "oil_gas_ep":               [
        "APA",    # rationale: APA Corporation — independent E&P (Permian + Egypt + North Sea); strategic-options review
        "SM",     # rationale: SM Energy — Midland Basin + Eagle Ford pure-play E&P
        "PBF",    # rationale: PBF Energy — refining + supplying petroleum products (downstream margin proxy)
        "TPL",    # rationale: Texas Pacific Land — Permian royalties + surface + water (+75% YTD on AI-DC land thesis)
        "VG",     # rationale: Venture Global LNG — US Gulf Coast LNG export terminals (Calcasieu Pass + Plaquemines),
        "MUR", # rationale: auto-placed (Oil & Gas E&P; oil and gas exploration),
        "CRK",   # rationale: oil_gas_ep is the exact fit — peer roster includes APA (Apache), SM (SM Energy), PBF (PBF Energy refiner — slightly different identity), TPL (Texas Pacific Land), VG (Venture Gl...
        "DK",   # rationale: oil_gas_ep is the closest fit — peer roster includes APA (Apache E&P), SM (SM Energy E&P), PBF (PBF Energy — DIRECT peer, also a downstream refiner), TPL (Texas Pacific Land roy...
        "KOS",   # rationale: oil_gas_ep is the exact fit — peer roster includes APA (Apache), SM (SM Energy), PBF (PBF refiner), TPL (Texas Pacific Land royalty), VG (Venture Global LNG — DIRECT downstream ...
        "PARR",   # rationale: oil_gas_ep is the right fit — peer roster includes APA (Apache), SM (SM Energy), PBF (PBF Energy — DIRECT peer; downstream refiner with similar profile), TPL (Texas Pacific Land...
    ],
    "oil_gas_services":         [
        "DINO",   # rationale: HF Sinclair — independent refiner + retail / lubricants (midstream-adjacent services)
        "WFRD",   # rationale: Weatherford — global oilfield equipment + services
        "RIG",    # rationale: Transocean — offshore deepwater drilling rigs
        "LBRT",   # rationale: Liberty Energy — premier hydraulic-fracturing fleet + power-infra pivot
        "KGS",    # rationale: Kodiak Gas Services — natural gas contract compression infrastructure,
        "AESI", # rationale: auto-placed (Oil & Gas Equipment & Services; oilfield services)
        "NESR", # rationale: auto-placed (Oil & Gas Equipment & Services; oilfield services)
        "VAL", # rationale: auto-placed (Oil & Gas Equipment & Services; drilling services),
        "AROC",   # rationale: oil_gas_services theme captures oilfield services + midstream-adjacent services. Roster includes WFRD (Weatherford — completion + intervention services), LBRT (Liberty — frac), ...
        "BORR",   # rationale: oil_gas_services is the exact fit — peer roster includes RIG (Transocean — direct peer; offshore deepwater driller, same dayrate-cyclical business model), VAL (Valaris — global ...
        "HP",   # rationale: oil_gas_services is the exact fit — peer roster includes DINO (HF Sinclair refining services), WFRD (Weatherford OFS), RIG (Transocean offshore drilling), LBRT (Liberty Energy f...
        "NE",   # rationale: oil_gas_services is the exact fit — peer roster includes DINO (HF Sinclair refining), WFRD (Weatherford OFS), RIG (Transocean — DIRECT deepwater driller peer), LBRT (Liberty Ene...
        "PTEN",   # rationale: oil_gas_services is the exact fit — peer roster includes DINO (HF Sinclair refining services), WFRD (Weatherford OFS), RIG (Transocean offshore), LBRT (Liberty Energy frac — DIR...
        "PUMP",   # rationale: Cross-listing oil_gas_services + power_demand_for_ai captures both narrative drivers — exactly the dual-identity case as ProPetro transforms from pure-play Permian frac to data ...
        "TDW",   # rationale: oil_gas_services is the exact fit -- peer roster includes DINO, WFRD, RIG (DIRECT offshore peer), LBRT, KGS, AESI, NESR, VAL (DIRECT offshore peer) + recent placements BORR/HP/N...
    ],
    "industrial_materials":     [
        "DOW",    # rationale: Dow — diversified materials science (commodity chemicals; cutting 4,500 jobs / $2B savings)
        "CE",     # rationale: Celanese — engineered polymers (auto + construction headwinds, $1B divestiture target)
        "WLK",    # rationale: Westlake — PVC + chlor-alkali (housing + construction exposure)
        "ESI",    # rationale: Element Solutions — specialty chemicals for electronics + industrial
        "IP",     # rationale: International Paper — renewable fiber-based packaging (containerboard)
        "SW",     # rationale: Smurfit Westrock — global paper-based packaging (post-merger)
        "CF",     # rationale: CF Industries — nitrogen fertilizers (ammonia / urea / UAN)
        "MOS",    # rationale: Mosaic — concentrated phosphates + potash for fertilizer,
        "ASH",   # rationale: industrial_materials theme contains DOW (commodity + specialty chemicals) + CE (Celanese — acetyl/engineered materials) + WLK (Westlake — chlor-alkali/petchem) + ESI (Element So...
        "CC",   # rationale: Cross-listing industrial_materials + datacenter_buildout captures both narrative drivers. industrial_materials roster (DOW/CE/WLK/ESI/IP/SW/CF/MOS) is the chemicals/packaging/fe...
        "FMC",   # rationale: industrial_materials is the right fit — peer roster includes DOW (Dow Chemicals), CE (Celanese), WLK (Westlake), ESI (Element Solutions), IP (International Paper), SW (Smurfit W...
        "GPK",   # rationale: industrial_materials is the exact fit — peer roster includes DOW (Dow chemicals), CE (Celanese), WLK (Westlake), ESI (Element Solutions), IP (International Paper — DIRECT peer i...
        "HUN",   # rationale: industrial_materials is the exact fit — peer roster includes DOW (Dow Chemicals — DIRECT peer in commodity chemicals + polyurethanes intermediates), CE (Celanese — DIRECT peer i...
        "MEOH",   # rationale: industrial_materials is the exact fit — peer roster includes DOW (Dow Chemicals — DIRECT peer; methanol is a key Dow downstream input), CE (Celanese — DIRECT peer; acetic acid +...
        "NEU",   # rationale: industrial_materials is the exact fit — peer roster includes DOW (Dow chemicals), CE (Celanese specialty chemicals — DIRECT specialty chemicals peer), WLK (Westlake), ESI (Eleme...
        "OLN",   # rationale: industrial_materials is the right fit — peer roster includes DOW (Dow Chemicals — DIRECT peer; chlor alkali + epoxy + diverse chemicals), CE (Celanese), WLK (Westlake — DIRECT p...
        "PCT",   # rationale: industrial_materials is the closest fit — peer roster includes DOW (Dow chemicals), CE (Celanese), WLK (Westlake), ESI (Element Solutions), IP (International Paper), SW (Smurfit...
        "PRM",   # rationale: industrial_materials is the closest fit — peer roster includes DOW (Dow chemicals), CE (Celanese), WLK (Westlake), ESI (Element Solutions — DIRECT peer in specialty chemicals), ...
        "SMG",   # rationale: industrial_materials is the closest fit — peer roster includes DOW (Dow chemicals), CE (Celanese), WLK (Westlake), ESI (Element Solutions), IP (International Paper), SW (Smurfit...
        "SXT",   # rationale: industrial_materials is the right fit — peer roster includes DOW (Dow chemicals), CE (Celanese — DIRECT specialty chemicals peer), WLK (Westlake), ESI (Element Solutions — DIREC...
    ],

    # ═══════════════════════════════════════════════════
    #  CONSUMER & TRAVEL
    # ═══════════════════════════════════════════════════
    "energy_drinks_wellness":   [
        "CELH",   # rationale: Celsius Holdings — fitness energy-drink brand (PepsiCo distribution agreement)
        "BROS",   # rationale: Dutch Bros — drive-thru coffee + energy drink growth (cross-listed in restaurants_fast_casual)
        "COCO",   # rationale: Vita Coco — coconut water + PWR LIFT protein water (+37% Q1 2026 net sales)
        "COKE",   # rationale: Coca-Cola Consolidated — largest US Coca-Cola bottler (functional + energy SKU exposure),
        "BRBR",   # rationale: energy_drinks_wellness is the closest fit — peer roster is CELH (Celsius energy-drink challenger brand), BROS (Dutch Bros coffee), COCO (Vita Coco coconut water — wellness bever...
        "PRMB",   # rationale: energy_drinks_wellness is the closest fit — peer roster includes CELH (Celsius — energy drink challenger brand), BROS (Dutch Bros coffee), COCO (Vita Coco — DIRECT peer; premium...
    ],
    "restaurants_fast_casual":  [
        "CAVA",   # rationale: CAVA Group — Mediterranean fast-casual; comps pacing above 3-5% guide, 415 restaurants
        "WING",   # rationale: Wingstop — wing-focused fast-casual franchise model with strong unit growth
        "SHAK",   # rationale: Shake Shack — upscale fast-casual burgers
        "EAT",    # rationale: Brinker International — Chili's + Maggiano's (casual-dining; positioned for trade-down)
        "BROS",   # rationale: Dutch Bros — drive-thru coffee (cross-listed in energy_drinks_wellness),
        "CAKE",   # rationale: restaurants_fast_casual is the closest fit but with a label-stretch caveat. Peer roster: CAVA (Mediterranean fast-casual), WING (Wingstop wings), SHAK (Shake Shack burgers), EAT...
        "PZZA",   # rationale: restaurants_fast_casual is the right fit — peer roster includes CAVA (Mediterranean fast-casual), WING (Wingstop wings — DIRECT QSR pizza-adjacent peer; same delivery + carryout...
        "WEN",   # rationale: restaurants_fast_casual is the exact theme. WEN sits with MCD, YUM, QSR, CMG, SBUX, DPZ, WING, TXRH, CAVA, BROS, JACK + recent placements as the publicly-listed QSR + fast-casua...
    ],
    "footwear_apparel":         [
        "CROX",   # rationale: Crocs — DTC + brand momentum (Crocs +12.9%, HEYDUDE +8.6% DTC growth Q1)
        "ONON",   # rationale: On Holding — premium running/performance brand; reiterated 23%+ CC FY net sales growth
        "BOOT",   # rationale: Boot Barn — western + work specialty retail footwear / apparel
        "AS",     # rationale: Amer Sports — Arc'teryx + Salomon + Wilson outdoor/sports brand portfolio
        "GAP",    # rationale: Gap Inc. — Gap + Old Navy + Banana Republic + Athleta turnaround story
        "VFC",    # rationale: V.F. Corporation — Vans + The North Face + Timberland (restructuring portfolio)
        "AEO",    # rationale: American Eagle Outfitters — denim + Aerie intimates mall apparel
        "ANF",    # rationale: Abercrombie & Fitch — mid-tier apparel turnaround (A&F + Hollister)
        "BIRK",   # rationale: Birkenstock — premium footwear (59% gross margin, post-IPO)
        "CPRI",   # rationale: Capri Holdings — Michael Kors + Versace + Jimmy Choo luxury portfolio
        "CRI",    # rationale: Carter's — kidswear (Carter's + OshKosh)
        "FIGS",   # rationale: FIGS — medical apparel DTC
        "KTB",    # rationale: Kontoor Brands — Wrangler + Lee denim
        "PVH",    # rationale: PVH Corp — Calvin Klein + Tommy Hilfiger
        "REAL",   # rationale: The RealReal — authenticated luxury consignment resale
        "SHOO",   # rationale: Steven Madden — footwear brand
        "UAA",    # rationale: Under Armour — performance apparel + footwear
        "URBN",   # rationale: Urban Outfitters — UO + Anthropologie + Free People specialty apparel
        "VSCO",   # rationale: Victoria's Secret — intimates / lingerie specialty retail
    ],
    "beauty":                   [
        "ELF",    # rationale: e.l.f. Beauty — affordable trend-led cosmetics; 27 consecutive quarters of net sales growth
        "EL",     # rationale: Estée Lauder — prestige skincare + makeup + fragrance; Beauty Reimagined opex reset
        "BBWI",   # rationale: Bath & Body Works — specialty personal care + home fragrance retailer
    ],
    "discount_retail":          [
        "DG",     # rationale: Dollar General — small-format rural-focused discount (low-income core consumer pressured)
        "DLTR",   # rationale: Dollar Tree — Dollar Tree 3.0 multi-price now ~5,300 stores capturing trade-down
        "FIVE",   # rationale: Five Below — youth + family value retailer in transition
        "OLLI",   # rationale: Ollie's Bargain Outlet — closeout retailer (treasure-hunt format)
        "SFM",    # rationale: Sprouts Farmers Market — specialty grocer (fresh + healthy)
        "MUSA",   # rationale: Murphy USA — value gas-station retailing (Walmart-adjacent fuel + convenience)
    ],
    "ecommerce":                [
        "ETSY",   # rationale: Etsy — two-sided online marketplace for handmade / vintage goods
        "RH",     # rationale: RH (Restoration Hardware) — luxury home furnishings (cross-listed in building_products)
        "W",      # rationale: Wayfair — online home goods retailer
        "EBAY",   # rationale: eBay — global marketplace for new + used goods + collectibles
        "CHWY",   # rationale: Chewy — pet products e-commerce (secular pet-spend tailwind)
        "CART",   # rationale: Maplebear (Instacart) — grocery e-commerce technology platform
        "CVNA",   # rationale: Carvana — used-vehicle e-commerce (fastest-growing US retail e-commerce)
        "SHOP",   # rationale: Shopify — commerce platform tools (cross-listed in vertical_saas)
        "CPNG",   # rationale: Coupang — Korean e-commerce + fulfillment (Rocket Delivery)
        "WIX",    # rationale: Wix — website + e-commerce platform for SMBs
        "DASH",   # rationale: DoorDash — local commerce / delivery platform (food + groceries + ads)
        "KMX",    # rationale: CarMax — used-car retailer (omnichannel + digital)
        "GLBE", # rationale: auto-placed (Internet Retail; e-commerce platform),
        "CARG",   # rationale: ecommerce is the right fit — peer roster includes ETSY (handmade marketplace), RH (Restoration Hardware), W (Wayfair home), EBAY (general marketplace), CHWY (Chewy pet), CART (I...
        "STUB",   # rationale: ecommerce is the right fit — peer roster includes ETSY (Etsy marketplace), RH (RH home), W (Wayfair home), EBAY (eBay — DIRECT marketplace peer; StubHub's former parent + simila...
        "GRAB",   # rationale: Cross-listing added: Grab's super-app business model spans Deliveries (food + grocery + parcels) + Mobility + Financial Services -- the Deliveries + ecommerce-adjacent business ...
        "SN",   # rationale: small kitchen appliances + vacuums + beauty (SharkNinja) — consumer brand, not building materials; DTC mix adds ecommerce
    ],
    "travel_services":          [
        "CCL",    # rationale: Carnival — largest global cruise operator (85% of 2026 capacity booked at record prices)
        "NCLH",   # rationale: Norwegian Cruise Line — expanding Caribbean capacity 40% YoY (capacity-absorption risk)
        "RCL",    # rationale: Royal Caribbean — premium cruise operator (~2/3 of 2026 booked at record rates)
        "VIK",    # rationale: Viking Holdings — river + ocean expedition cruises (older affluent demographic)
        "BKNG",   # rationale: Booking Holdings — online travel platforms (Booking.com / Priceline / Agoda / Kayak)
        "EXPE",   # rationale: Expedia Group — online travel (Expedia / Vrbo / Hotels.com),
        "MMYT",   # rationale: travel_services is the right fit — peer roster includes CCL (Carnival cruise), NCLH (Norwegian Cruise), RCL (Royal Caribbean), VIK (Viking Cruises), BKNG (Booking Holdings — DIR...
        "YOU",   # rationale: Cross-listing captures the dual identity. cybersecurity captures the identity-verification + KYC + workforce-identity B2B platform (CLEAR1 + Sora ID + Mount Sinai/CMS/Ochsner co...
    ],
    "airlines":                 [
        "AAL",   # rationale: American Airlines — global network carrier (Latin America + transatlantic strength)
        "UAL",   # rationale: United Airlines — premium-cabin + international (positioned with Delta as leader)
        "JBLU",  # rationale: JetBlue — leisure-focused with TrueBlue / Mint premium cabin restructuring
        "LUV",   # rationale: Southwest — capacity-disciplined (~2% FY 2026 growth at low end of guide)
        "ALK",   # rationale: Alaska Air Group — west-coast carrier with Hawaiian integration,
        "ALGT",   # rationale: airlines theme contains AAL/UAL/JBLU/LUV/ALK — all US passenger airlines. ALGT is the canonical US ULCC + a direct peer to the existing roster (especially LUV/JBLU as discount-l...
        "CPA",   # rationale: Cross-listing added: CPA is a pure-play airline (Panama-based regional + intra-Americas hub carrier) -- airlines theme captures the core operating identity alongside AAL, DAL, U...
    ],
    "streaming_media":          [
        "SPOT",   # rationale: Spotify — audio streaming + podcasts; Premium Lite $8.99 / standard $15.99 ad-free
        "ROKU",   # rationale: Roku — connected-TV operating system + ad platform (85.5M streaming households)
        "PSKY",   # rationale: Paramount Skydance — media + entertainment post-merger
        "SPHR",   # rationale: Sphere Entertainment — Las Vegas immersive venue + MSG Networks
        "CHTR",   # rationale: Charter Communications — broadband connectivity + Spectrum video bundles
        "GDC",    # rationale: GD Culture Group — short-video / live-streaming + e-commerce platform,
        "WMG", # rationale: auto-placed (Entertainment; streaming service),
        "IMAX",   # rationale: streaming_media is the closest fit — peer roster includes SPOT (Spotify audio streaming), ROKU (Roku CTV platform), PSKY (Paramount Skydance media), SPHR (Sphere Entertainment —...
        "LBRDK",   # rationale: streaming_media is the right fit — peer roster includes SPOT (Spotify), ROKU (Roku CTV), PSKY (Paramount Skydance), SPHR (Sphere Entertainment), CHTR (Charter Communications — D...
        "LION",   # rationale: streaming_media is the right fit — peer roster includes SPOT (Spotify), ROKU (Roku CTV), PSKY (Paramount Skydance — DIRECT peer in studio + content business), SPHR (Sphere Enter...
        "NXST",   # rationale: streaming_media is the right fit — peer roster includes SPOT (Spotify), ROKU (Roku CTV), PSKY (Paramount Skydance media — DIRECT peer; broadcast + studio + cable), SPHR (Sphere ...
        "SIRI",   # rationale: streaming_media is the exact fit — peer roster includes SPOT (Spotify — DIRECT peer; audio streaming + podcast), ROKU (Roku CTV), PSKY (Paramount), SPHR (Sphere Entertainment), ...
        "TDS",   # rationale: streaming_media is the closest fit — peer roster includes SPOT (Spotify), ROKU (Roku CTV), PSKY (Paramount), SPHR (Sphere Entertainment), CHTR (Charter Cable — DIRECT peer; cabl...
        "VSNT",   # rationale: streaming_media is the right fit -- peer roster includes SPOT, ROKU, PSKY (Paramount Skydance -- DIRECT peer; same legacy-cable-spinoff + streaming-transition narrative; merged ...
    ],
    "gaming_betting":           [
        "RBLX",   # rationale: Roblox — immersive gaming + social platform (heavy young-user engagement)
        "DKNG",   # rationale: DraftKings — online sports betting + prediction markets ($55-80B GGR opportunity by 2030),
        "CHDN",   # rationale: gaming_betting is the right fit — peer roster includes DKNG (DraftKings — direct online-wagering peer for TwinSpires segment), RBLX (Roblox gaming platform), GME (GameStop gamin...
        "PENN",   # rationale: gaming_betting is the exact fit — peer roster includes RBLX (Roblox gaming), GME (GameStop gaming retail), DKNG (DraftKings — DIRECT peer; online sports betting + iCasino) + rec...
        "RSI",   # rationale: gaming_betting is the exact fit — peer roster includes RBLX (Roblox), GME (GameStop retail), DKNG (DraftKings — DIRECT major peer; online sports betting + iCasino) + recent plac...
        "SGHC",   # rationale: gaming_betting is the exact fit — peer roster includes RBLX (Roblox), GME (GameStop), DKNG (DraftKings — DIRECT peer; major online sports betting + iCasino, although US-focused)...
        "SRAD",   # rationale: gaming_betting is the right fit — peer roster includes RBLX (Roblox), GME (GameStop), DKNG (DraftKings) + recent placements CHDN/PENN/RSI/SGHC. SRAD is the B2B data + technology...
    ],
    "social_media":             [
        "SNAP",   # rationale: Snap — Snapchat camera + AR (cross-listed in ad_tech_marketing); ARPU +10% Q1 2026
        "PINS",   # rationale: Pinterest — visual search/discovery (cross-listed); 631M MAUs, 10 consec quarters DD growth
        "RDDT",   # rationale: Reddit — community discussion + ads (cross-listed); ARPU +61% in Q1 with $663M Q1 revenue
    ],

    # ═══════════════════════════════════════════════════
    #  REAL ESTATE & PROPTECH
    # ═══════════════════════════════════════════════════
    "real_estate_proptech":     [
        "Z",      # rationale: Zillow — housing super-app strategy (search + financing + touring + agents)
        "COMP",   # rationale: Compass — residential brokerage end-to-end technology platform
        "OPEN",   # rationale: Opendoor — iBuying / "Opendoor 2.0" asset-light home-flipping platform
        "CSGP",   # rationale: CoStar Group — commercial real-estate information + analytics + listings
        "CBRE",   # rationale: CBRE — global commercial real-estate services + investment management
        "JLL",    # rationale: Jones Lang LaSalle — commercial real-estate services + capital markets,
        "ZG",   # rationale: real_estate_proptech captures the residential-real-estate-marketplace + housing-super-app + mortgage + rental + agent-tools narrative -- ZG is the consumer-facing pure-play in t...
    ],

    # ═══════════════════════════════════════════════════
    #  ELECTRONIC COMPONENTS, AUTO PARTS, MISC
    # ═══════════════════════════════════════════════════
    "auto_parts_tech":          [
        "APTV",   # rationale: Aptiv — high-voltage architecture + ADAS / autonomous tech (cross-listed in ev_supply_chain/autonomous)
        "BWA",    # rationale: BorgWarner — powertrain + battery-management (cross-listed in ev_supply_chain)
        "LKQ",    # rationale: LKQ — aftermarket replacement parts distribution (collision + mechanical)
        "MOD",    # rationale: Modine — thermal management (cross-listed in datacenter_buildout / ev_supply_chain / power)
        "ALSN",   # rationale: Allison Transmission — commercial-vehicle EV transmissions (cross-listed in ev_supply_chain)
        "ST",     # rationale: Sensata — sensors / sensor-rich solutions for vehicle systems (cross-listed),
        "AAP",   # rationale: auto_parts_tech captures the aftermarket-parts narrative even though the current theme narrative emphasizes EV/ADAS OEM-supplier content-per-vehicle. LKQ is already in the roste...
        "GT",   # rationale: auto_parts_tech is the closest fit but with theme-name caveat — peer roster (APTV/BWA/LKQ/MOD/ALSN/ST) is dominated by EV-electronic + powertrain + aftermarket-parts suppliers; ...
        "MBLY",   # rationale: Cross-listing autonomous_mobility + auto_parts_tech captures both narrative drivers exactly the dual-identity case. autonomous_mobility roster (TSLA/AUR/IOT/LYFT/JOBY/ACHR) is t...
        "VC",   # rationale: auto_parts_tech is the right fit -- peer roster includes APTV (Aptiv -- DIRECT peer; same cockpit electronics + ADAS + connected car platform business), BWA (BorgWarner), LKQ, M...
        "VGNT",   # rationale: auto_parts_tech is the parent fit -- APTV (Versigent's former parent) is in this theme along with BWA, LKQ, MOD, ALSN, ST + recent placements VC/MBLY/GT/GTX/AAP. Versigent IS th...
        "VNT",   # rationale: auto_parts_tech is the right fit -- peer roster includes APTV, BWA, LKQ, MOD, ALSN, ST + recent placements VC/MBLY/GT/GTX/AAP. VNT sits with LKQ + AAP as the aftermarket-service...
        "VVV",   # rationale: auto_parts_tech is the right fit -- peer roster includes APTV, BWA, LKQ, MOD, ALSN, ST + recent placements VC/MBLY/GT/GTX/AAP. VVV sits with AAP as the publicly-listed aftermark...
        "BB",   # rationale: Cross-listing added: QNX is nearly half of FY26 revenue + embedded in 275M+ vehicles + the growth engine -- auto_parts_tech is now a load-bearing second narrative (QNX is automo...
    ],
    "electronic_components":    [
        "APH",
        "VSH",    # rationale: Vishay — discrete semis + passives; book-to-bill 1.20 on AI-power demand
        "LFUS",   # rationale: Littelfuse — circuit protection + sensing components (auto + industrial)
        "VICR",   # rationale: Vicor — high-density power-modules for AI/HPC and aerospace
        "OLED",   # rationale: Universal Display — OLED IP + emitter materials (Samsung/LG royalty stream),
        "DIOD",   # rationale: electronic_components is the right fit — peer roster includes APH (Amphenol connectors), VSH (Vishay — DIRECT peer, discrete + passive components), LFUS (Littelfuse — protection...
        "PI",   # rationale: electronic_components is the right fit — peer roster includes APH (Amphenol connectors), VSH (Vishay discrete + passive components), LFUS (Littelfuse protection), VICR (Vicor po...
        "ROG",   # rationale: electronic_components is the right fit — peer roster includes APH (Amphenol connectors), VSH (Vishay discrete + passive — DIRECT peer; same industrial/auto/defense electronic-co...
        "CRUS",   # rationale: BUCKET C -- proposed primary-theme CHANGE from ai_compute (incorrect placement; CRUS is a fabless audio + mixed-signal IC company for smartphones + AR/VR + automotive, NOT an AI...
        "MCHP",   # rationale: BUCKET C -- proposed primary-theme CHANGE from ai_compute to electronic_components. MCHP is fundamentally an embedded MCU + analog + memory + FPGA + security + connectivity semi...
    ],
    "data_analytics_terminals": [
        "FDS",    # rationale: FactSet — financial data + market analytics platform
        "TRI",    # rationale: Thomson Reuters — content + technology for legal / tax / news (Westlaw + Refinitiv)
        "TYL",    # rationale: Tyler Technologies — gov data + analytics (cross-listed in vertical_saas)
        "IT",     # rationale: Gartner — business + tech research subscriptions / advisory
        "VRSK",   # rationale: Verisk — insurance / climate / financial analytics (~$3.2B 2026 revenue)
        "TRU",    # rationale: TransUnion — global consumer credit reporting + risk analytics
        "FICO",   # rationale: Fair Isaac — credit-scoring analytics + decision-management software,
        "MORN",   # rationale: data_analytics_terminals is the exact fit — peer roster includes FDS (FactSet — DIRECT peer; investment research + analytics terminal), TRI (Thomson Reuters), TYL (Tyler Technol...
    ],
    "consulting_govt":          [
        "ACN",    # rationale: Accenture — strategy + IT consulting (DOGE federal-contract reviews headwind)
        "EPAM",   # rationale: EPAM Systems — digital engineering + software services (commercial-heavy)
        "CTSH",   # rationale: Cognizant — IT services + digital transformation
        "BAH",    # rationale: Booz Allen Hamilton — federal IT + AI consulting (cutting 2.5K jobs; cross-listed in defense_tech)
        "CACI",   # rationale: CACI — defense + intel IT services (cross-listed in defense_tech)
        "LDOS",   # rationale: Leidos — federal mission IT + defense solutions (cross-listed in defense_tech),
        "CNXC",   # rationale: consulting_govt is the closest fit — peer roster includes ACN (Accenture — direct peer in global services + AI-transition consulting), EPAM (EPAM Systems — global IT services), ...
        "DXC",   # rationale: consulting_govt is the exact fit — peer roster includes ACN (Accenture, direct peer in global IT services + consulting), EPAM (EPAM Systems global engineering services), CTSH (C...
        "EFOR",   # rationale: consulting_govt is the exact fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen — DIRECT federal-government peer), CACI (federal ...
        "EXLS",   # rationale: consulting_govt is the right fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant — DIRECT peer; global IT services + consulting + AI pivot), BAH (Bo...
        "FCN",   # rationale: consulting_govt is the right fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen), CACI/LDOS (federal IT services). FCN is the bus...
        "KBR",   # rationale: consulting_govt is the right fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen — DIRECT government services peer), CACI (DIRECT ...
        "KD",   # rationale: consulting_govt is the exact fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant — DIRECT peer; same global IT services + managed infrastructure mod...
        "MMS",   # rationale: consulting_govt is the exact fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen — DIRECT government services peer), CACI (DIRECT ...
        "PSN",   # rationale: consulting_govt is the right fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen — DIRECT federal services + cyber + defense peer)...
        "RHI",   # rationale: consulting_govt is the right fit — peer roster includes ACN (Accenture), EPAM (EPAM Systems), CTSH (Cognizant), BAH (Booz Allen — DIRECT consulting peer to Protiviti), CACI/LDOS...
        "ULS",   # rationale: consulting_govt is the closest fit -- peer roster includes ACN, EPAM, CTSH, BAH, CACI, LDOS + recent placements DXC/EFOR/KBR/KD/EXLS/MMS/PSN/RHI. UL Solutions is a global TIC se...
        "VVX",   # rationale: Cross-listing matches the established LDOS / BAH / CACI pattern -- those three peers are dual-listed in defense_tech AND consulting_govt. V2X has identical dual identity: defens...
        "WLDN",   # rationale: Cross-listing matches the dual identity. power_demand_for_ai is the stock-driver rerating narrative -- data-center electrical engineering + AI grid software is the 2025-26 catal...
        "G",   # rationale: BUCKET C -- proposed primary-theme CHANGE from data_analytics_terminals to consulting_govt. Genpact is a BPO + IT services + AI/analytics services firm (services revenue model),...
        "GLOB",   # rationale: Cross-listing added: Globant's primary identity is digital + software engineering services + AI/ML transformation firm comparable to ACN/EPAM/CTSH/IBM (consulting + IT services ...
    ],

    # ═══════════════════════════════════════════════════
    #  BIOTECH — placed last, isolated from the rest
    # ═══════════════════════════════════════════════════
    "biotech":                  [
        "RVMD", "AXSM", "CELC", "MIRM", "PTCT", "ARWR", "BBIO", "ALNY", "INSM",
        "MDGL", "PRAX", "TVTX", "MRNA", "NTLA", "CYTK", "HALO", "ABVX", "ERAS", "ROIV",
        "TEM",    # rationale: Tempus AI — clinical-data layer for oncology (cross-listed in ai_apps_platforms)
        "ORKA",   # rationale: Oruka Therapeutics — clinical-stage immunology / psoriasis biologics
        "NTRA",   # rationale: Natera — diagnostics + Signatera MRD (cross-listed in diagnostics_tools)
        "LABU",   # rationale: Direxion 3x S&P Biotech Bull ETF (leveraged sector exposure)
        "ARKG",   # rationale: ARK Genomic Revolution ETF (active gene/biotech basket),
        "ABCL", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "ADMA", # rationale: auto-placed (Biotechnology; antibody+biopharmaceutical)
        "ANAB", # rationale: auto-placed (Biotechnology; antibody)
        "APGE", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "ARQT", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "AVTX", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "BCRX", # rationale: auto-placed (antibody+monoclonal)
        "BEAM", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "CGON", # rationale: auto-placed (Biotechnology; oncology+immunotherapy)
        "CORT", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "CRSP", # rationale: auto-placed (Biotechnology; gene editing+oncology)
        "DNTH", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "EWTX", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "IBRX", # rationale: auto-placed (Biotechnology; immunotherapy+biologic)
        "IMVT", # rationale: auto-placed (Biotechnology; monoclonal)
        "INBX", # rationale: auto-placed (Biotechnology; biopharmaceutical+biologic)
        "IOVA", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "KNSA", # rationale: auto-placed (antibody+biopharmaceutical)
        "KYMR", # rationale: auto-placed (Biotechnology; oncology+biopharmaceutical)
        "LEGN", # rationale: auto-placed (Biotechnology; oncology+biopharmaceutical)
        "LGND", # rationale: auto-placed (Biotechnology; antibody+oncology)
        "MANE", # rationale: auto-placed (Biotechnology; immunotherapy+biopharmaceutical)
        "NAMS", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "NKTR", # rationale: auto-placed (Biotechnology; immunotherapy+biopharmaceutical)
        "NUVL", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "QURE", # rationale: auto-placed (Biotechnology; gene therapy)
        "RARE", # rationale: auto-placed (Biotechnology; gene therapy+antibody)
        "RLAY", # rationale: auto-placed (Biotechnology; oncology+biologic)
        "RXRX", # rationale: auto-placed (Biotechnology; oncology)
        "RYTM", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "SLS", # rationale: auto-placed (Biotechnology; immunotherapy+biopharmaceutical)
        "SRPT", # rationale: auto-placed (Biotechnology; gene therapy+biopharmaceutical)
        "SRRK", # rationale: auto-placed (Biotechnology; antibody+biopharmaceutical)
        "SYRE", # rationale: auto-placed (Biotechnology; antibody+monoclonal)
        "TARS", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "TGTX", # rationale: auto-placed (Biotechnology; antibody+biopharmaceutical)
        "TNGX", # rationale: auto-placed (Biotechnology; oncology)
        "VKTX", # rationale: auto-placed (Biotechnology; biopharmaceutical)
        "VRDN", # rationale: auto-placed (Biotechnology; antibody+monoclonal),
        "ALKS",   # rationale: biotech is the right placement — specialty pharma developing novel therapeutics. Existing roster includes CNS peers (CORT — Corcept psychiatric, AXSM — Axsome neuropsychiatric, ...
        "CMPS",   # rationale: biotech is the exact fit — peer roster (67 names) includes the full clinical-stage + commercial biotech universe (RVMD, AXSM, CELC, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL etc)...
        "COGT",   # rationale: biotech is the exact fit — peer roster (67 names) includes the full late-stage biotech universe with NDA-approaching / commercial-launching names like RVMD, AXSM (Axsome — recen...
        "CRNX",   # rationale: biotech is the exact fit — peer roster includes the full clinical + early-commercial-stage biotech universe (RVMD, AXSM, MIRM, PTCT, MDGL, INSM, BBIO, ALNY, etc). CRNX is now a ...
        "INDV",   # rationale: biotech is the right fit — peer roster (67 names) includes the full commercial + clinical biotech universe (RVMD, AXSM, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL etc) plus addict...
        "KRYS",   # rationale: biotech is the exact fit — peer roster (67 names) includes the full commercial + clinical biotech universe (RVMD, AXSM, CELC, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL etc) inclu...
        "LQDA",   # rationale: biotech is the exact fit — peer roster (67 names) includes the full commercial + clinical biotech universe (RVMD, AXSM, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL, KRYS just-place...
        "NVAX",   # rationale: biotech is the exact fit — peer roster (67 names) includes commercial + clinical biotech universe (RVMD, AXSM, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL etc) including vaccine pe...
        "PCVX",   # rationale: biotech is the exact fit — peer roster (67 names) includes clinical + commercial-stage biotechs including vaccine peers like MRNA + BNTX (mRNA platforms) + NVAX just-placed (pro...
        "PTGX",   # rationale: biotech is the exact fit — peer roster (67 names) includes the full clinical + commercial-stage biotech universe (RVMD, AXSM, MIRM, PTCT, ARWR, BBIO, ALNY, INSM, MDGL etc) + rec...
    ],

    # Mall-anchored department stores + non-apparel specialty retail. Trades
    # on consumer health / mall-traffic data / discretionary credit cycle.
    "consumer_retail": [
        "M",   # rationale: Macy's — flagship US department store
        "KSS", # rationale: Kohl's — mid-tier mall department store
        "DDS", # rationale: Dillard's — Southern US department store
        "ASO", # rationale: Academy Sports — sporting goods big-box
        "WSM", # rationale: Williams-Sonoma — premium home goods retail
        "SIG", # rationale: Signet Jewelers — Kay/Zales jewelry chain
        "EYE", # rationale: National Vision — eyewear specialty retail,
        "WRBY",   # rationale: consumer_retail is the right fit -- WRBY is omnichannel specialty retail (323 stores + ecommerce) with the AI-glasses launch sitting at the consumer-brand + wearable-tech inters...
        "GME",   # rationale: BUCKET C -- proposed primary-theme CHANGE from gaming_betting to consumer_retail. GameStop is fundamentally a specialty retailer of video games + collectibles + trading cards + ...
        "SN",   # rationale: small kitchen appliances + vacuums + beauty (SharkNinja) — consumer brand, not building materials; DTC mix adds ecommerce
    ],

    # Latin America / Emerging Markets — LatAm-domiciled or LatAm-core
    # equity exposure. Rotates with USD strength + commodity cycles.
    "latam_em": [
        "BAP",  # rationale: Credicorp — Peruvian banking holding company
        "TIGO", # rationale: Millicom — LatAm telecom
        "CPA",  # rationale: Copa Holdings — Panama-based LatAm airline
        "INTR", # rationale: Inter & Co — Brazilian neobank
        "XP",   # rationale: XP Inc — Brazilian wealth / fintech
        "STNE", # rationale: StoneCo — Brazilian payments
        "VIST", # rationale: Vista Energy — Mexico / Argentina E&P
        "GLOB", # rationale: Globant — Argentine-founded IT services
        "SATL", # rationale: Satellogic — Argentine satellite imaging
        "SGML", # rationale: Sigma Lithium — Brazilian lithium miner,
        "AUGO",   # rationale: gold_silver_miners is the primary theme — direct peer to KGC (Kinross — gold producer with LatAm ops), EGO (Eldorado — mid-tier gold), BTG (B2Gold), PAAS (Pan American Silver), ...
    ],

    # ═══════════════════════════════════════════════════
    #  CHINA — ADRs + China-exposure (potential bottoming play)
    #  Names that trade on Chinese-market sentiment, regardless of
    #  GICS sector. Cross-listed freely — co-movement is the point.
    # ═══════════════════════════════════════════════════
    "china": [
        # Internet / e-commerce
        "BABA",   # rationale: Alibaba — China e-commerce + cloud bellwether
        "PDD",    # rationale: Pinduoduo / Temu parent
        "JD",     # rationale: JD.com — e-commerce + logistics
        "BIDU",   # rationale: Baidu — search + AI + autonomous
        "VIPS",   # rationale: Vipshop — discount e-commerce
        "DADA",   # rationale: Dada Nexus — local delivery (not in cache yet)
        # EV
        "NIO",    # rationale: NIO — China EV
        "XPEV",   # rationale: XPeng — China EV
        "LI",     # rationale: Li Auto — China EV
        "ZK",     # rationale: ZEEKR — Geely premium EV (not in cache yet)
        # Media / entertainment / social
        "BILI",   # rationale: Bilibili — video / gaming community
        "TME",    # rationale: Tencent Music
        "NTES",   # rationale: NetEase — gaming
        "IQ",     # rationale: iQIYI — streaming video
        "WB",     # rationale: Weibo — social
        "DOYU",   # rationale: DouYu — game streaming
        "HUYA",   # rationale: HUYA — game streaming
        "MOMO",   # rationale: Hello Group — social
        "ZH",     # rationale: Zhihu — Q&A / content platform
        # Fintech / brokerage
        "FUTU",   # rationale: Futu Holdings — online brokerage
        "TIGR",   # rationale: UP Fintech (Tiger Brokers) — online brokerage
        "QFIN",   # rationale: Qifu Technology — credit-tech
        "LX",     # rationale: LexinFintech — consumer finance
        "FINV",   # rationale: FinVolution — consumer finance
        # Travel / hospitality / consumer services
        "TCOM",   # rationale: Trip.com — China travel OTA
        "YUMC",   # rationale: Yum China — KFC/Pizza Hut operator
        "HTHT",   # rationale: H World (Huazhu) — hotels
        "BEKE",   # rationale: KE Holdings (Beike) — housing transactions
        "ATHM",   # rationale: Autohome — auto marketplace
        # Education
        "EDU",    # rationale: New Oriental Education
        "TAL",    # rationale: TAL Education
        "GOTU",   # rationale: Gaotu Techedu
        # Logistics / data centers
        "ZTO",    # rationale: ZTO Express — parcel delivery
        "GDS",    # rationale: GDS Holdings — China data centers
        "VNET",   # rationale: VNET Group — China data centers
        # Solar / clean energy (China manufacturing base)
        "JKS",    # rationale: JinkoSolar — China solar module maker
        "DQ",     # rationale: Daqo New Energy — China polysilicon
        "CSIQ",   # rationale: cross-listed from solar — Canadian Solar, China-heavy manufacturing/ops
        # Biotech (China-based)
        "ZLAB",   # rationale: Zai Lab — China biopharma
        "LEGN",   # rationale: cross-listed from biotech — Legend Biotech, Nanjing-based CAR-T
        # China-exposure ETFs (broad-market correlation anchors)
        "FXI",    # rationale: iShares China Large-Cap ETF
        "KWEB",   # rationale: KraneShares China Internet ETF
        "MCHI",   # rationale: iShares MSCI China ETF
        "ASHR",   # rationale: CSI 300 A-shares ETF
        "CQQQ",   # rationale: Invesco China Technology ETF
        "PGJ",    # rationale: Invesco Golden Dragon China ETF
        "KTEC",   # rationale: KraneShares China tech ETF
        "YINN",   # rationale: 3x leveraged China bull ETF (directional exposure)
        "CWEB",   # rationale: 2x leveraged China internet bull ETF (directional exposure)
    ],
}


# Display labels for each theme key
THEME_LABELS = {
    "mag7":                     "MAG7 · mega-cap tech anchors",
    "ai_compute":               "AI Compute · semis",
    "ai_optics":                "AI Optics · 800G/1.6T",
    "ai_networking":            "AI Networking · DC fabric",
    "ai_connectivity":          "AI Connectivity · analog & mixed signal",
    "semiconductor_ip":         "Semiconductor IP · ARM, EDA",
    "memory_cycle":             "Memory Cycle · HBM/NAND",
    "semi_equipment":           "Semi Equipment · wafer fab cap-ex",
    "datacenter_buildout":      "Datacenter Buildout · servers, cooling, racks, fiber",
    "ai_compute_clouds":        "AI Compute Clouds · GPU-as-a-Service + miner-pivot DCs",
    "semiconductor_testing":    "Semiconductor Testing · ATE & metrology",
    "ai_apps_platforms":        "AI Apps & Platforms",
    "quantum_computing":        "Quantum Computing",
    "power_demand_for_ai":      "Power Demand for AI · utilities, grid, generators",
    "nuclear_renaissance":      "Nuclear Renaissance · SMR + utilities",
    "uranium_pure_plays":       "Uranium · pure plays",
    "solar":                    "Solar",
    "energy_storage":           "Energy Storage",
    "fuel_cells_hydrogen":      "Fuel Cells & Hydrogen",
    "ev_makers":                "EV Makers",
    "ev_supply_chain":          "EV Supply Chain",
    "autonomous_mobility":      "Autonomous Mobility · self-driving + eVTOL",
    "crypto_platforms":         "Crypto Platforms & Stablecoins",
    "bitcoin_treasury":         "Bitcoin Treasury Cos",
    "crypto_miners":            "Crypto Miners",
    "drones":                   "Drones",
    "space_economy":            "Space Economy",
    "robotics_automation":      "Robotics & Automation",
    "defense_tech":             "Defense Tech",
    "critical_minerals":        "Critical Minerals & Rare Earths",
    "gold_silver_miners":       "Gold & Silver Miners",
    "industrial_metals":        "Industrial Metals · copper, steel, alum, specialty",
    "cybersecurity":            "Cybersecurity",
    "data_devops_platforms":    "Data & DevOps Platforms",
    "cloud_infra":              "Cloud Infra · edge, CDN, hosting",
    "productivity_saas":        "Productivity SaaS",
    "vertical_saas":            "Vertical SaaS",
    "ad_tech_marketing":        "Ad Tech & Marketing",
    "medtech_devices_consumer_health":  "Medtech Devices & Consumer Health",
    "diagnostics_tools":        "Diagnostics & Life Science Tools",
    "managed_care":             "Managed Care · payors & providers",
    "fintech_disruptors":       "Fintech Disruptors",
    "financial_services_specialty": "Financial Services · payments + specialty ins",
    "alt_asset_managers":       "Alt Asset Managers · PE/credit",
    "reshoring_construction":   "Reshoring & Construction",
    "electrical_grid":          "Electrical Grid & Power Equipment",
    "hvac_cooling":             "HVAC & Cooling",
    "building_products":        "Building Products & Housing Stack",
    "industrial_distribution":  "Industrial Distribution",
    "transports":               "Transports · trucking, freight, leasing, rental",
    "oil_gas_ep":               "Oil & Gas E&P · upstream + LNG",
    "oil_gas_services":         "Oil & Gas Services",
    "industrial_materials":     "Industrial Materials · chemicals, packaging, fertilizer",
    "energy_drinks_wellness":   "Energy Drinks & Wellness",
    "restaurants_fast_casual":  "Restaurants · fast casual",
    "footwear_apparel":         "Footwear & Apparel",
    "beauty":                   "Beauty",
    "discount_retail":          "Discount Retail",
    "ecommerce":                "E-commerce & DTC",
    "travel_services":          "Travel Services · cruise + OTAs",
    "airlines":                 "Airlines",
    "streaming_media":          "Streaming, Media & Cable",
    "gaming_betting":           "Gaming & Sports Betting",
    "social_media":             "Social Media",
    "real_estate_proptech":     "Real Estate & Proptech",
    "auto_parts_tech":          "Auto Parts & Tech",
    "electronic_components":    "Electronic Components",
    "data_analytics_terminals": "Data Analytics & Terminals",
    "consulting_govt":          "Consulting & Gov Services",
    "biotech":                  "Biotech · therapeutics, gene editing, GLP-1",
    "latam_em":                 "LatAm · Latin America",
    "consumer_retail":          "Consumer Retail · department stores + specialty mall retail",
    "china":                    "China · ADRs + China-exposure",
}


# Trader narratives — what's driving money flow per theme.
# One 1-2 sentence narrative per theme. `# source: <url>` above each entry.
# Zero confabulation: every narrative is grounded in cited reporting.
# Filled in batches as themes are researched; missing keys fall back to
# rendering without a narrative line.
THEME_NARRATIVES = {
    # source: https://finance.yahoo.com/markets/article/magnificent-7-earnings-rush-reveals-ai-spending-surge-with-hyperscaler-capex-set-to-reach-725-billion-in-2026-224901707.html
    "mag7":                     "The seven biggest US tech companies driving the bulk of S&P earnings growth; four of them (MSFT, GOOGL, AMZN, META) are guiding to roughly $725B of 2026 capex with ~75% earmarked for AI infrastructure.",

    # source: https://www.heygotrade.com/en/blog/how-hyperscaler-capex-drives-semiconductor-stock-prices/
    "ai_compute":               "GPUs and custom AI silicon at the center of the hyperscaler capex wave; NVDA data-center revenue $194B fiscal 2026, AMD data-center +57% YoY, plus the broader ecosystem of analog power, advanced packaging, memory controllers, and programmable logic feeding the same buildout.",

    # source: https://tech-insider.org/nvidia-silicon-photonics-lumentum-coherent-ai-data-center-2026/
    "ai_optics":                "800G transceivers shipping in volume and 1.6T ramping in 2H 2026; NVIDIA's $4B silicon-photonics investments in Lumentum and Coherent flagged optics as a hard capacity bottleneck for AI clusters.",

    # source: https://exoswan.com/ai-infrastructure-stocks/
    "ai_networking":            "Ethernet/InfiniBand fabric between GPU racks and across buildings; Arista raised 2026 AI guidance to $3.5B, Credo's AEC revenue ramped +272% YoY, Astera Labs entered the $20B switching market with Scorpio in early 2026.",

    # source: https://markets.financialcontent.com/stocks/article/finterra-2026-1-28-the-connectivity-powerhouse-a-deep-dive-into-astera-labs-alab-and-the-future-of-ai-fabrics
    "ai_connectivity":          "The PCIe 6 / CXL / NVLink layer moving data between GPUs, CPUs, memory, and storage inside an AI rack; Astera Labs Aries retimers, Marvell Alaska P retimers, plus the analog/mixed-signal supply for high-speed serdes.",

    # source: https://finance.yahoo.com/sectors/technology/articles/arm-holdings-doubled-2026-outperforming-192717021.html
    "semiconductor_ip":         "Royalty engines on every shipped AI chip — ARM at $4.92B FY2026 revenue with data-center royalty more than doubling YoY, plus EDA tools from Synopsys and Cadence required to design AI silicon.",

    # source: https://news.ssbcrack.com/memory-and-storage-stocks-surge-as-ai-demand-boosts-micron-sandisk-and-western-digital/
    "memory_cycle":             "HBM sold out through calendar 2026 for Nvidia Blackwell; data centers now consume over 50% of industry DRAM+NAND; Sandisk tripled YTD, Western Digital +77%, Seagate +65%.",

    # source: https://ts2.tech/en/applied-materials-stock-jumps-today-as-tsmc-lifts-2026-capex-and-barclays-upgrades-amat/
    "semi_equipment":           "Wafer-fab capex is the upstream pulse — TSMC lifted 2026 capex to $52-56B (vs $46B consensus), driving same-day 5-9% rallies in LRCX, AMAT, and KLA.",

    # source: https://www.datacenterfrontier.com/machine-learning/article/55353727/from-silicon-to-cooling-delloro-maps-the-ai-data-center-buildout
    "datacenter_buildout":      "Power, cooling, racks, servers, and integration around the GPUs — Vertiv supplies the power/cooling ecosystem, Dell carries $43B AI server backlog, Celestica builds custom hyperscaler racks, and liquid cooling became non-negotiable in 2026.",

    # source: https://sergeycyw.substack.com/p/neo-clouds-ai-infrastructures-fastest
    "ai_compute_clouds":        "GPU-as-a-service operators with multi-year hyperscaler contracts — CoreWeave $11B+ contracted at Polaris Forge 1, IREN $9.7B Microsoft deal + Nvidia partnership, Applied Digital $16B leased capacity; several pivoted from Bitcoin mining to AI compute leasing.",

    # source: https://www.lambdafin.com/articles/vistra-vs-constellation-vs-talen
    "power_demand_for_ai":      "Utilities and power-equipment names selling firm 24/7 baseload to hyperscalers — Constellation closed Calpine (55GW combined), Vistra signed a 20-yr AWS PPA up to 1,200 MW at Comanche Peak nuclear, Talen's expanded Amazon Susquehanna deal supplies up to 1,920 MW for ~$18B in revenue.",

    # source: https://markets.financialcontent.com/wral/article/marketminute-2026-1-5-the-nuclear-renaissance-nuscale-power-shares-gap-up-as-smr-technology-hits-major-milestones
    "nuclear_renaissance":      "AI power demand reviving baseload nuclear — Microsoft/Amazon/Meta signing 20-yr PPAs with existing operators, TVA planning up to 6 GW of NuScale SMRs, Oklo pursuing on-site colocation with AI data centers, BWXT noticing the NRC for a new enrichment facility.",

    # source: https://finance.yahoo.com/news/nuclear-comeback-2026-3-uranium-175000894.html
    "uranium_pure_plays":       "Pure mining/enrichment exposure to the nuclear restart — Cameco the sector blue-chip, UEC running the first new US in-situ recovery (Burke Hollow) in a decade, Centrus on enrichment, Energy Fuels at the only licensed US conventional mill.",

    # source: https://www.blockhead.co/2026/05/05/circle-surges-20-as-clarity-act-breakthrough-lifts-crypto-stocks/
    "crypto_platforms":         "Exchanges + stablecoin infrastructure riding the CLARITY Act and OCC trust-bank approvals — Coinbase $1.35B 2025 stablecoin revenue (USDC ATH market cap), Circle granted conditional OCC trust charter, stablecoin market $305B Q1 2026 projected to $3T by 2030.",

    # source: https://www.mexc.com/news/566675
    "bitcoin_treasury":         "Equity proxies for crypto via balance-sheet hoarding — MSTR holds 818,334 BTC ($54B+ cost basis) and accounted for 97.5% of net new corporate BTC purchases in early 2026; BMNR running the same playbook on Ethereum. Funding via convertible debt + perpetual preferreds.",

    # source: https://bitcoinminingstock.io/hashrate
    "crypto_miners":            "Public hashrate leaders MARA (72 EH/s), Bitdeer (65), CleanSpark (46); post-2024-halving margin pressure pushed Hut 8, CleanSpark, Cipher, TeraWulf, and Riot to add AI/HPC compute hosting alongside Bitcoin mining.",

    # source: https://www.thestreet.com/investing/drone-stocks-to-target-as-military-appetite-surges
    "drones":                   "Defense and counter-drone procurement surge — Pentagon requesting ~$54B for autonomous drones in FY2027, Anduril signed a $20B Army contract; tactical-UAS public names AVAV/KTOS/RCAT/ONDS leading the rally.",

    # source: https://www.investing.com/analysis/pentagon-goes-allin-on-ai-historic-defense-budget-fuels-defensetech-stocks-200678438
    "defense_tech":             "$839B FY2026 base defense budget + $150B Golden Dome appropriation; AI-and-autonomy primes (Kratos, AeroVironment, BWXT, Axon, Anduril) gaining share against legacy primes as procurement shifts to attritable systems.",

    # source: https://www.cnbc.com/2025/10/13/rare-earth-stocks.html
    "critical_minerals":        "China's October 2025 rare-earth export controls (suspended for 1 year until Nov 2026) re-rated Western producers — MP Materials +21%, Energy Fuels +16%, USA Rare Earth +18% on the announcement; DoD set $110/kg neodymium-praseodymium floor for MP, a 70% premium to spot.",

    # source: https://finance.yahoo.com/news/quantum-computing-set-scale-2026-180000840.html
    "quantum_computing":        "Speculative leg on quantum hardware — IonQ 2025 revenue +202% YoY guiding $225-245M for 2026 with $370M backlog; Rigetti's $590M cash funds 108-qubit + 150-qubit deployments through 2026.",

    # source: https://www.gurufocus.com/news/2607271/applovin-app-and-palantir-pltr-surge-as-ai-applications-drive-market-optimism
    "ai_apps_platforms":        "Enterprise + consumer AI software riding on top of the GPU stack — Palantir AIP across gov + enterprise, AppLovin AXON drove Q4 2025 revenue +66% YoY at 84% adj EBITDA margin, Tempus Q4 2025 revenue +83% on clinical-data layer for oncology.",

    # source: https://semiengineering.com/hbm-shifts-testing-left-to-preserve-ai-chip-yield/
    "semiconductor_testing":    "Teradyne + Advantest control ~80% of ATE; HBM4/HBM5 testing costs rising to 10-15% of chip cost (vs ~5% for traditional SoCs); Aehr Test backlog $50.9M driven by silicon-photonics + wafer-level burn-in for AI data centers.",

    # source: https://www.sec.gov/Archives/edgar/data/0001780312/000119312526216946/asts-ex99_1.htm
    "space_economy":            "Direct-to-cell broadband, proliferated LEO + lunar landers, and commercial launch — AST SpaceMobile targeting 45 satellites in orbit by end of 2026 with FCC commercial authorization, Rocket Lab providing launch + spacecraft to defense/commercial.",

    # source: https://www.automate.org/videos/symbotic-system-overview
    "robotics_automation":      "Symbotic Q2 FY2026 revenue $676M +23% YoY for AI-enabled warehouse robots; industrial automation and humanoid-robot platforms commercializing through 2026 on labor scarcity + reshoring.",

    # source: https://finance.yahoo.com/sectors/technology/articles/tesla-just-beat-byd-ev-143027681.html
    "ev_makers":                "BYD leads global EV share at 17.1% in March 2026 vs Tesla at 8.9%, but Tesla retook Q1 with 358K deliveries vs BYD's 310K; Rivian R2 SUV launching 2026, Lucid focused on Midsize + first robotaxi deployments.",

    # source: https://www.borgwarner.com/newsroom/press-releases/2026/02/11/borgwarner-expands-series-production-battery-management-system-program-with-a-global-oem
    "ev_supply_chain":          "Powertrain + battery-management content per vehicle still growing — BorgWarner reaffirmed 2026 outlook with new global-OEM BMS program, Albemarle still digesting the lithium market reset, Aptiv high-voltage architectures into next-gen EV platforms.",

    # source: https://www.fool.com/investing/how-to-invest/stocks/how-to-invest-in-waymo-stock/
    "autonomous_mobility":      "Waymo expanding robotaxi to Atlanta/Miami/DC + Tokyo/London in 2026 ($16B raise at $126B post-money); Mobileye targeting 100K self-driving vehicles by 2033; Aurora running driverless trucks Dallas↔Houston commercially.",

    # source: https://markets.financialcontent.com/wral/article/marketminute-2026-3-2-solar-giants-guidance-cliff-first-solar-shares-crater-18-on-weak-2026-outlook-and-policy-headwinds
    "solar":                    "Mixed 2026 — First Solar backlog runs through 2029 with IRA Section 48E 10-pt domestic-content bonus, but new 15% component tariffs and federal permit freeze cost $135M to FSLR 2026; Section 25D residential credit expired Dec 31 2025.",

    # source: https://www.energy-storage.news/fluence-expects-to-meet-feoc-compliance-by-regulatory-deadlines-offers-2026-guidance/
    "energy_storage":           "Fluence guiding $3.2-3.6B FY2026 (~85% in backlog) on grid-scale BESS + AI-powered bidding software across ~50 markets; Eos scaling zinc long-duration storage with Cerberus joint-venture IPP.",

    # source: https://www.sec.gov/Archives/edgar/data/0001664703/000162828026027913/ex991_q126financialresults.htm
    "fuel_cells_hydrogen":      "On-site solid-oxide fuel cells from Bloom Energy expanding into data centers + semiconductor fabs as firm 24/7 power; pure-play hydrogen names (Plug, FuelCell) remain earlier-stage, tied to 45V production tax credit and large-customer adoption.",

    # source: https://parameter.io/may-2026-cybersecurity-stocks-top-5-picks-including-crowdstrike-crwd-and-datadog-ddog/
    "cybersecurity":            "AI deployment expands attack surface + machine identities + data-in-motion, creating pressure to consolidate security stacks — CRWD Falcon, PANW platform play, FTNT Q1 billings +31% YoY at $2.09B, ZS / OKTA each capturing different parts of the consolidation.",

    # source: https://www.tikr.com/blog/datadog-vs-snowflake-which-cloud-data-stock-is-the-better-growth-play
    "data_devops_platforms":    "Consumption-based platforms benefit from AI workload growth — Snowflake re-accelerated to 29-32% YoY revenue growth, MongoDB Atlas accelerating, Datadog +27.7% in fiscal 2025 on observability for AI/security/hybrid.",

    # source: https://siliconangle.com/2026/01/22/nutanix-enterprise-ai-infrastructure-shift-nextconf/
    "cloud_infra":              "Edge + CDN + private cloud — Cloudflare programmable edge (Workers, edge functions), Nutanix Cloud Platform bridging on-prem to public cloud (GPT-in-a-Box + Agentic AI in 2H 2026), Rackspace FAIR multi-cloud AI hosting.",

    # source: https://www.pymnts.com/artificial-intelligence-2/2026/servicenow-sap-and-workday-make-ai-agents-pay-to-play/
    "productivity_saas":        "Enterprise SaaS pivoting to AI-agent monetization — ServiceNow Action Fabric (Knowledge 2026), Workday Agent System of Record + Sana; Feb 2026 'SaaSpocalypse' wiped ~$285B after Anthropic Cowork plugins (HUBS -51%, MNDY -44%, NOW -36% peak-to-trough).",

    # source: https://tech-insider.org/the-rise-of-vertical-saas-why-industry-specific-software-is-winning/
    "vertical_saas":            "Industry-specific SaaS reports 31% median growth (vs 28% horizontal) with 3x retention — Veeva (life sciences, $35B+ mkt cap), Toast (85K+ restaurants), Guidewire (450+ insurers), Shopify (commerce); embedded finance accelerating.",

    # source: https://digiday.com/marketing/ad-tech-briefing-applovins-ai-fueled-surge-and-the-trade-desks-stumble-show-where-investors-are-placing-their-bets/
    "ad_tech_marketing":        "AppLovin AXON +68% YoY at $1.4B Q4 revenue, Trade Desk's Ventura CTV initiative, Pinterest opening Media Network Connect to non-endemic advertisers — CTV + retail-media + AI signal stacking are the 2026 catalysts.",

    # source: https://247wallst.com/investing/2026/04/15/upstart-soars-14-affirm-climbs-7-two-fintech-disruptors-signal-the-sector-may-be-turning-a-corner/
    "fintech_disruptors":       "Digital-finance challengers turning the corner — SoFi Q1 2026 revenue +41% YoY ($1.10B) with record $12.18B originations +68%; Chime $647M Q1 revenue with 10.2M active members; Upstart Q4 revenue +31% YoY on AI lending models.",

    # source: https://www.insurancebusinessmag.com/us/news/breaking-news/ryan-specialty-swings-to-q1-profit-as-eands-tailwinds-fade-573772.aspx
    "financial_services_specialty": "E&S insurance market crossed $100B in 2025 (+7.8%) — Ryan Specialty Q1 2026 revenue +15.2% YoY at $795M, Kinsale's pure E&S underwriting compounding; payments names (CPAY, GPN) on processor consolidation.",

    # source: https://www.morningstar.com/stocks/why-alts-manager-stocks-are-getting-hit-hard
    "alt_asset_managers":       "Private-credit-heavy alts under pressure in 2026 — Apollo (86% PC of fee-earning AUM), Ares (66%), Blue Owl (53%) all down double-digits YTD; Carlyle's more balanced book down only ~2.4%.",

    # source: https://www.marketscreener.com/news/vrsk-verisk-analytics-expects-2026-revenue-range-3-19b-3-24b-vs-factset-est-of-3-21b-ce7f59d3d081f52c
    "data_analytics_terminals": "Subscription financial-data + analytics franchises — FactSet integrated platform with AI insights, Verisk guiding $3.19-3.24B 2026 revenue on insurance analytics, Gartner research subscriptions, FICO/TransUnion credit analytics.",

    # source: https://www.sec.gov/Archives/edgar/data/0001323404/000119312526212446/d91444dex991.htm
    "gold_silver_miners":       "Royalty/streaming + producer leverage to precious-metals price — Wheaton expanded silver stream on Antamina (BHP) Feb 2026, added Jervois (KGL Resources) Apr 2026; sector beneficiary of debasement / safe-haven flows.",

    # source: https://www.ainvest.com/news/trump-tariff-expansion-impact-steel-aluminum-sectors-strategic-financial-implications-producers-cleveland-cliffs-2508/
    "industrial_metals":        "Section 232 tariffs on steel/aluminum hiked 25%→50% in 2025 + extended to 400+ derivative products; Cleveland-Cliffs Q1 2026 HRC avg $980/ton (+24% YoY), $4.8B steelmaking revenue; Freeport / Southern Copper geared to copper electrification demand.",

    # source: https://money.usnews.com/investing/articles/diabetes-and-weight-loss-drug-stocks-with-big-potential
    "biotech":                  "GLP-1 obesity drugs mainstream and pipelines expanding; gene-editing readouts (Intellia, Verve, CRISPR), MASH NASH (Madrigal), rare disease (Alnylam, BridgeBio), oncology (Revolution Medicines, Arrowhead) — pipeline-driven cohort with binary catalysts.",

    # source: https://www.modernhealthcare.com/medical-devices/mh-medical-devices-2026-dexcom-ge-healthcare-philips/
    "medtech_devices_consumer_health":  "Diabetes care + consumer health — Dexcom guiding 2026 revenue $5.16-5.25B (+11-13%) with G7 15-Day expansion, Insulet pivotal Type 2 trial in 2026, Glaukos highest medtech R&D intensity; consumer health pulls in Hims & Hers / Zoetis.",

    # source: https://www.stocktitan.net/sec-filings/NTRA/8-k-natera-inc-reports-material-event-df549edda1b1.html
    "diagnostics_tools":        "Liquid biopsy + life-science tools rerating — Natera Q1 2026 revenue $696.6M (+38.8%) with clinical oncology volumes +54.4% YoY, MRD adoption (Signatera Medicare coverage) and MCED (Guardant Shield, Exact Cancerguard) hitting clinical inflection.",

    # source: https://www.fiercehealthcare.com/regulatory/2026-ma-star-ratings-aetna-humana-see-score-decline-unitedhealthcare-improves
    "managed_care":             "2026 Medicare Advantage Star Ratings stabilized after multi-year declines — Humana 3.61 avg (declining MA-Star membership share), Centene 3.39 (up from 3.14, 20% of members in 4+ star plans vs 1% prior year); margin recovery story.",

    # source: https://www.fedmanager.com/news/trump-administration-pushes-agencies-to-cut-consulting-contracts
    "consulting_govt":          "Federal consulting crackdown — Hegseth terminated $5.1B DoD consulting contracts, DHA cuts saving $1.8B from Accenture/Deloitte/Booz Allen; Booz Allen cutting 2,500 jobs, fiscal 2026 revenue projected -3%; Accenture federal services losing DOGE-reviewed contracts.",

    # source: https://www.ad-hoc-news.de/boerse/news/ueberblick/amphenol-stock-us0320951017-analysts-targets-diverge-as-connectivity/69339273
    "electronic_components":    "Vishay Q4 book-to-bill 1.20 with orders at 3-yr high on AI-power + industrial demand; Amphenol benefiting from connector demand across AI/auto/aerospace; selective SKU-specific component supply tightness in auto + EV + industrial.",

    # source: https://www.tikr.com/blog/10-auto-supplier-stocks-riding-the-ev-transition
    "auto_parts_tech":          "EV + ADAS content per vehicle still rising — Aptiv in 18 of 20 top-selling US models in 2025 with high-voltage architecture + ADAS, BorgWarner expanding battery-management programs, Modine thermal for EV cooling.",

    # source: https://www.precedenceresearch.com/proptech-market
    "real_estate_proptech":     "Proptech market $54.66B → $209B by 2035 (CAGR 16%); Zillow housing super-app strategy, Opendoor 2.0 asset-light iBuying, CoStar enterprise focus; ROAD Act (Senate Mar 2026) restricts institutional SFR buying for 15 years.",

    # source: https://finance.yahoo.com/markets/stocks/articles/comfort-systems-vs-quanta-infrastructure-142100352.html
    "reshoring_construction":   "Record contractor backlogs on AI-DC + semi-plant + reshoring construction — Quanta $48.5B backlog, MasTec $19B (+13% sequential), Comfort Systems $12.45B (vs $6.89B YoY; advanced tech = 56% of revenue).",

    # source: https://www.powermag.com/transformers-in-2026-shortage-scramble-or-self-inflicted-crisis/
    "electrical_grid":          "Transformer / breaker lead times stretched to 4 years; >50% of planned 2026 US data centers expected delayed — Eaton expanding $1.2B capacity, Vertiv guiding $13.25-13.75B 2026 (+27-29% organic), Powell switchgear sold out.",

    # source: https://www.aaon.com/news/aaon-awarded-liquid-cooling-data-center-orders-totaling-approximately-174.5-million
    "hvac_cooling":             "Datacenter cooling pulled HVAC makers into AI-adjacent backlog — Modine $180M AI-DC cooling orders + new Wisconsin plant, AAON $174.5M liquid-cooling order for one DC customer; Watsco distribution layer for residential + light commercial.",

    # source: https://www.investing.com/news/company-news/builders-firstsource-q1-2026-slides-earnings-miss-amid-housing-slump-93CH-4650421
    "building_products":        "Soft 2026 housing — US single-family starts -6.5% YoY in January, Builders FirstSource lowered 2026 guide to $14.6-15.6B; R&R spending the bright spot (mid-single growth 2026, high-single 2027).",

    # source: https://www.inddist.com/earnings/news/22966161/wesco-reports-record-first-quarter
    "industrial_distribution":  "Data-center-driven distribution boom — Wesco DC sales +70% YoY now ~25% of total with record backlog, Arrow Electronics Q1 revenue $9.5B +39% on early cyclical recovery.",

    # source: https://finance.yahoo.com/markets/stocks/articles/zacks-industry-outlook-highlights-old-094000240.html
    "transports":               "Trucking still in recession (Zacks Industry Rank #173/244) — ODFL Feb revenue/day -3.3% (LTL tons -6.8%), XPO first YoY tonnage gain in 18+ months (+0.2%); demand encouragingly improved through Q1.",

    # source: https://markets.financialcontent.com/stocks/article/marketminute-2026-3-16-energy-giant-texas-pacific-land-corp-skyrockets-75-ytd-as-permian-powerhouse-evolves-into-ai-infrastructure-play
    "oil_gas_ep":               "Range-bound oil price + Permian growth — Texas Pacific Land +75% YTD on royalty + water + AI-DC-land thesis, Exxon guiding +12.5% Permian production for 2026; ~$70B US upstream assets on the market.",

    # source: https://www.sec.gov/Archives/edgar/data/0001694028/000169402826000021/lbrt_3312026.htm
    "oil_gas_services":         "NAM completions demand stabilized after softening — Liberty Energy scaling power-infra platform alongside frac, flat oil production targets but modest gas-directed growth; range-bound oil keeps service spend disciplined.",

    # source: https://247wallst.com/investing/2026/03/09/wall-street-just-sent-a-clear-signal-on-dow-and-lyondellbasell-heres-what-it-means/
    "industrial_materials":     "Chemicals deep restructuring — Dow cutting 4,500 jobs ($2B savings), LyondellBasell trimmed 2026 capex $300M, Celanese earnings -51% YoY ($1B divestiture target); NAM ethane cost advantage on Middle-East supply disruption.",

    # source: https://www.sec.gov/Archives/edgar/data/0001482981/000148298126000118/coco-20260331xexx991pressr.htm
    "energy_drinks_wellness":   "Functional-beverage growth pockets — Vita Coco Q1 2026 net sales $180M (+37%, coconut water +42%) raising 2026 guide to $720-735M; CELH energy-drink momentum + COKE bottling exposure.",

    # source: https://www.ainvest.com/news/dutch-bros-cava-restaurant-stock-offers-stronger-2026-growth-catalyst-2512/
    "restaurants_fast_casual":  "Disciplined unit-growth fast-casual standouts — Dutch Bros Q1 2026 revenue +31% YoY with 8.3% same-shop sales growth and 41 new shops opened; CAVA at 415 restaurants with comps pacing above 3-5% guide; Wingstop / Sweetgreen also on growth list.",

    # source: https://www.stocktitan.net/sec-filings/CROX/8-k-crocs-inc-reports-material-event-f9136f79aed8.html
    "footwear_apparel":         "DTC + brand momentum — On Holding reiterated FY 23%+ constant-currency net sales growth (DTC + APAC + apparel leading), Crocs DTC +12.1% (Crocs brand +12.9%, HEYDUDE +8.6%), Birkenstock 59% gross margin + 3.13 current ratio.",

    # source: https://www.investing.com/news/stock-market-news/rj-favours-beauty-stocks-estee-lauder-elf-and-ulta-in-2026-amid-slow-consumption-4457803
    "beauty":                   "Prestige beauty bifurcation — Estée Lauder navigating ~$100M tariff drag with 'Beauty Reimagined' opex reset (Q1 organic +3%, op margin +300bps to 7.3%); e.l.f. Beauty 27 consecutive quarters of net sales growth on affordable trend-led products.",

    # source: https://247wallst.com/investing/2026/03/16/dollar-tree-vs-dollar-general-which-discount-retailer-delivers-the-smarter-return-for-your-portfolio/
    "discount_retail":          "Trade-down bifurcation — Dollar Tree 3.0 multi-price now ~5,300 stores attracting 3M new households (60% from $100K+ income brackets); Dollar General SSS +4.3% but losing trade-down battle to more diversified players.",

    # source: https://www.fool.com/investing/stock-market/market-sectors/consumer-discretionary/top-ecommerce-companies/
    "ecommerce":                "Global e-commerce reaching $6.88T in 2026 (17.5% of total retail) — Amazon adding 0.4 pts share to ~38%, Carvana fastest-growing US retail e-commerce expecting sequential growth Q1 2026, Chewy benefiting from pet-product secular growth.",

    # source: https://247wallst.com/investing/2026/05/20/carnival-jumps-9-norwegian-cruise-line-soars-11-why-royal-caribbean-isnt-joining-the-cruise-party/
    "travel_services":          "Cruise demand record — Carnival 85% of 2026 capacity booked at record prices, Royal Caribbean ~2/3 booked at record rates, 34 new ships on order across top 5 cruise lines (~150K new berths); WTI spike to $111 a fuel-cost headwind.",

    # source: https://www.fool.com/investing/stock-market/market-sectors/industrials/airline-stocks/
    "airlines":                 "Capacity discipline + premium-cabin strategy — Southwest tightening 2026 capacity growth to ~2% (low end of 2-3% guide), Delta margin expansion on brand differentiation + premium-cabin strategy, fuel + geopolitics adding volatility.",

    # source: https://advertising.roku.com/2026-predictions
    "streaming_media":          "Ad-supported tiers absorbing the price hikes — ad-content now ~74% of all TV viewing, Roku connecting 85.5M streaming households (32B streaming hours Q3 2025); Netflix expanding into live sports (MLB Opening Night, Home Run Derby, WWE Raw).",

    # source: https://www.benzinga.com/analyst-stock-ratings/analyst-color/26/01/50075278/draftkings-flutter-bofa-sees-online-betting-driving-gaming-stocks-into-2026
    "gaming_betting":           "DraftKings Q1 2026 revenue $1.6B (+17% YoY), guiding 2026 revenue $6.5-6.9B + adj EBITDA $700-900M, targeting $55-80B GGR opportunity by 2030; Kalshi / Polymarket prediction-market traffic projected 445B contracts in 2026 (vs 95B in 2025).",

    # source: https://www.citybiz.co/article/846441/reddit-outpaces-social-media-rivals-in-revenue-per-user-growth/
    "social_media":             "Reddit Q1 2026 revenue +69% YoY ($663M) with US ARPU +61% to $9.20 (7 consecutive quarters of 60%+ growth); Pinterest Q1 revenue >$1B (+18%) with 631M MAUs (10th consecutive quarter of double-digit user growth).",

    "latam_em":                 "Latin America-anchored equity exposure — Brazilian / Mexican / Peruvian / Argentine banks, payments, telecoms, and resource plays. Rotates with USD strength, local-currency direction, and commodity cycles.",

    "consumer_retail":          "Mall-anchored department stores + specialty consumer retail — Macy's / Kohl's / Dillard's department-store traffic, Williams-Sonoma premium home goods, Academy Sports big-box, Signet jewelry, National Vision eyewear. Trades on consumer health, holiday-season traffic data, and discretionary credit cycle.",
}


# The full hand-picked ticker universe (470 names, $100M+ vol, 3.5+ ADR).
# Used to compute the "Ungrouped" section: any UNIVERSE ticker that doesn't
# appear in any THEMES list is rendered at the bottom of the dashboard.
UNIVERSE = [
    "FTNT", "VSH", "CRWD", "DDOG", "FLEX", "AMD", "HUM", "MXL", "PANW", "CNC",
    "INTC", "PENG", "FROG", "NXPI", "TXN", "ALAB", "HPE", "INOD", "OSCR", "ARW",
    "DOCN", "DGXX", "SYNA", "COCO", "ON", "RKLB", "MU", "STRL", "MTSI", "SMTC",
    "TWLO", "DVA", "RIOT", "MRVL", "GEN", "FCEL", "BTSG", "SITM", "MYRG", "SNDK",
    "STX", "VSAT", "WOLF", "SANM", "BB", "CLSK", "AAON", "EBAY", "TE", "S",
    "AXSM", "GFS", "ST", "DELL", "RBRK", "HUT", "NVTS", "RVMD", "LSCC", "MRAM",
    "GKOS", "NVT", "TTMI", "AKAM", "KEEL", "KGS", "MDB", "BE", "QRVO", "ZS",
    "MCHP", "SWKS", "HRB", "ENPH", "ACLS", "AUR", "KNX", "QCOM", "SM", "TVTX",
    "TSEM", "GH", "NVDA", "SAIA", "RDW", "WDC", "DXYZ", "ROIV", "MARA", "CORZ",
    "WCC", "LTH", "RXT", "ROKU", "CYTK", "AFRM", "OKTA", "PWR", "LUNR", "FPS",
    "AAL", "CGNX", "IONQ", "GTLB", "NBIS", "DINO", "JBL", "FSLR", "WFRD", "POET",
    "BWA", "MKSI", "LRCX", "LFUS", "MPWR", "Q", "IESC", "SMCI", "DXCM", "CPAY",
    "RELY", "ZM", "DBX", "SATS", "FICO", "CRDO", "SFM", "CIEN", "MOH", "FIX",
    "FLY", "ZBRA", "ORCL", "TEAM", "P", "ARWR", "AMAT", "RMBS", "NTNX", "RIG",
    "AXTI", "CROX", "POWL", "SEDG", "PL", "LGN", "APLD", "GNRC", "MUSA", "OUST",
    "VICR", "ALGM", "CELC", "GLXY", "F", "ARES", "WAT", "PAYC", "COHR", "U",
    "LUMN", "KLAC", "AMKR", "ILMN", "ECG", "FLNC", "VIAV", "CLF", "HPQ", "GDDY",
    "ESI", "PLUG", "GLW", "WULF", "MTZ", "TSLA", "IREN", "MSTR", "VIK", "AEHR",
    "CIFR", "HBM", "MNDY", "CRUS", "BAX", "SOUN", "SNOW", "DKNG", "LBRT", "DT",
    "SUNB", "CHRW", "AGX", "EVR", "SEI", "DOCU", "DY", "SPHR", "ZETA", "UAL",
    "JOBY", "EOSE", "QUBT", "ETSY", "ONON", "GEV", "MOD", "HALO", "DAVE", "SOLS",
    "AAOI", "APP", "RVTY", "ODFL", "BIO", "ASTS", "SNAP", "DUOL", "VRT", "BBAI",
    "ARE", "FN", "USAR", "ONTO", "FIG", "BROS", "RUN", "OWL", "XPO", "QS",
    "PTCT", "ICLR", "COMP", "RH", "SWK", "NOW", "CRML", "MDGL", "CART", "QBTS",
    "GPN", "NXT", "AS", "PRAX", "ORKA", "ALK", "CRCL", "FIGR", "APA", "FDS",
    "SARO", "LITE", "IT", "VG", "FORM", "LUV", "CRS", "COIN", "LWLG", "ABVX",
    "AKAN", "CLS", "BAP", "IQV", "RGTI", "CCL", "NTRA", "TER", "NVMI", "CRWV",
    "TLN", "WDAY", "OKLO", "MIRM", "ACHR", "NET", "UPST", "ATI", "IOT", "BTG",
    "EL", "HOOD", "JBLU", "FCX", "PAAS", "PATH", "RDDT", "PSKY", "OC", "FTAI",
    "RRX", "BAH", "PINS", "MHK", "GWRE", "RYAN", "SCCO", "AA", "SPXC", "XNDU",
    "TPG", "DASH", "ONDS", "MP", "ETN", "OLED", "INFQ", "CF", "HIMS", "THC",
    "MRNA", "EZGO", "WSO", "LYFT", "BF.B", "ENTG", "AXON", "BBIO", "FIVE", "SN",
    "AG", "TRU", "GME", "CVNA", "PBF", "ANET", "VEEV", "SMR", "GDC", "NTLA",
    "CAVA", "ACN", "LMND", "CEG", "UAMY", "GMED", "RGEN", "UEC", "TRI", "CDE",
    "TYL", "PLTR", "CG", "BWXT", "TEM", "CRL", "CSL", "EAT", "INTU", "EGO",
    "VRSK", "HL", "WPM", "SYM", "ALB", "ALNY", "TTD", "LPLA", "RCL", "RKT",
    "FND", "BMNR", "UUUU", "HUBS", "OPEN", "CHKP", "ELAN", "COKE", "SOFI", "VST",
    "CBRE", "DOW", "W", "KNSL", "ALGN", "CCJ", "UHS", "LEU", "KGC", "AMPX",
    "TECH", "FLR", "CAR", "KMX", "JLL", "GRAB", "SW", "G", "OLLI", "BOOT",
    "AEIS", "DLTR", "CACI", "BKNG", "EXPE", "FLS", "ALHC", "CSGP", "RBLX", "SHOP",
    "BBWI", "IAG", "SPOT", "GAP", "J", "TTEK", "APH", "AGI", "CHYM", "ERAS",
    "AVAV", "EQX", "FBIN", "KRMN", "IP", "PCOR", "SGI", "QXO", "CDW", "EPAM",
    "CTSH", "TPL", "VFC", "RIVN", "KVYO", "BLDR", "POOL", "DOCS", "LKQ", "TOST",
    "NCLH", "CE", "NRG", "SITE", "ALSN", "DG", "CELH", "WING", "Z", "MTD",
    "IDCC", "APTV", "OSK", "MOS", "MDLN", "ACM", "KTOS", "PODD", "BFAM", "OPCH",
    "INSM", "FSLY", "ELF", "IBP", "CHTR", "WHR", "CHWY", "PRIM", "RCAT", "PLNT",
    "LCID", "WIX", "CPNG", "TMDX", "UI", "WLK", "LDOS", "TSCO", "SHAK", "VISN",
    "ZTS", "COKE",
    # Added 2026-05-20 from theme screenshots Dan supplied
    "SLAB", "ADI", "ARM", "SNPS", "CDNS", "CEVA",
    "GSAT", "SPIR", "BKSY", "IRDM",
    "DNN",
    "BTDR", "HIVE",
    "COHU", "CAMT", "ICHR", "UCTT",
    # Added 2026-05-21 from TC2000 watchlist (399 names; theme placements to follow)
    "AAP", "ABCL", "ABG", "ACHC", "ACMR", "ADEA", "ADMA", "ADTN", "AEHL", "AEO",
    "AESI", "AI", "AIIO", "AIR", "ALGT", "ALKS", "ALM", "ALMU", "AMBA", "AMG",
    "AMR", "AMSC", "AMTM", "AN", "ANAB", "ANF", "AOSL", "APGE", "APPF", "ARCB",
    "AROC", "ARQT", "ARRY", "ASAN", "ASH", "ASO", "ASST", "ATEC", "ATMU", "ATRO",
    "AUGO", "AVEX", "AVTR", "AVTX", "AXGN", "BAND", "BC", "BCO", "BCRX", "BDC",
    "BEAM", "BELFB", "BEPC", "BFH", "BHE", "BILL", "BIPC", "BIRK", "BLDP", "BLLN",
    "BLSH", "BMI", "BORR", "BOX", "BRBR", "BRKR", "BRZE", "BSY", "BTBT", "BTU",
    "BULL", "BW", "BYND", "BZ", "CAI", "CAKE", "CALX", "CALY", "CARG", "CC",
    "CCC", "CECO", "CENX", "CGON", "CHDN", "CHEF", "CHH", "CMPS", "CNR", "CNXC",
    "COGT", "COLD", "CORT", "COUR", "CPA", "CPRI", "CRI", "CRK", "CRM", "CRNX",
    "CRSP", "CSIQ", "CSTM", "CTRI", "CVCO", "CVLT", "CWST", "DDS", "DIOD", "DK",
    "DLB", "DNTH", "DXC", "EEFT", "EFOR", "EME", "ENS", "ENVX", "EQPT", "ERIE",
    "ESAB", "ESE", "ESTC", "ETOR", "EWTX", "EXK", "EXLS", "EXTR", "EYE", "FCN",
    "FIGS", "FIVN", "FLO", "FMC", "FOUR", "FRMI", "FRPT", "FRSH", "FSM", "GLBE",
    "GLOB", "GPGI", "GPI", "GPK", "GT", "GTM", "GTX", "GXO", "HAE", "HAO",
    "HCC", "HII", "HLNE", "HNGE", "HOG", "HP", "HQY", "HRI", "HTZ", "HUN",
    "HXL", "HYMC", "IBRX", "IDXX", "IMAX", "IMVT", "INBX", "INDV", "INSP", "INSW",
    "INTR", "IOVA", "IPGP", "IRTC", "ITGR", "ITRI", "JBS", "JBTM", "JXN", "KAI",
    "KALU", "KBR", "KD", "KLAR", "KLIC", "KMT", "KNF", "KNSA", "KOPN", "KOS",
    "KRYS", "KSS", "KTB", "KYMR", "LAC", "LASR", "LAZ", "LBRDK", "LC", "LEGN",
    "LFST", "LGND", "LION", "LIVN", "LOAR", "LOPE", "LPTH", "LPX", "LQDA", "LRN",
    "M", "MAIR", "MANE", "MANH", "MATX", "MBLY", "MC", "MEOH", "MIAX", "MIR",
    "MMS", "MMSI", "MMYT", "MORN", "MRCY", "MRX", "MSGS", "MTRN", "MUR", "MWH",
    "MZTI", "NAMS", "NAVN", "NCNO", "NE", "NESR", "NEU", "NKTR", "NN", "NNE",
    "NOVT", "NPO", "NSIT", "NUVL", "NVAX", "NVST", "NXE", "NXST", "OLN", "OSIS",
    "OTEX", "PARR", "PATK", "PBH", "PBI", "PCT", "PCTY", "PCVX", "PDFS", "PEGA",
    "PENN", "PGY", "PI", "PII", "PLPC", "PLXS", "POWI", "PPC", "PPTA", "PRCT",
    "PRKS", "PRM", "PRMB", "PSIX", "PSN", "PTEN", "PTGX", "PTON", "PUMP", "PURR",
    "PVH", "PZZA", "QLYS", "QTWO", "QUCY", "QURE", "RAL", "RARE", "RDNT", "REAL",
    "REZI", "RHI", "RLAY", "RNG", "ROAD", "ROG", "RSI", "RVI", "RXO", "RXRX",
    "RYTM", "SAM", "SATL", "SBET", "SEB", "SEZL", "SGHC", "SGML", "SHLS", "SHOO",
    "SIDU", "SIG", "SIRI", "SKY", "SKYT", "SLG", "SLM", "SLNH", "SLS", "SMG",
    "SNEX", "SRAD", "SRPT", "SRRK", "SSRM", "STEP", "STNE", "STUB", "SVM", "SXI",
    "SXT", "SYRE", "TARS", "TDC", "TDIC", "TDS", "TDW", "TENB", "TEX", "TGTX",
    "THO", "THR", "TIGO", "TNDM", "TNGX", "TPC", "TREX", "TRMB", "TRT", "TTAN",
    "TWST", "TXG", "UAA", "UFPT", "ULS", "UMAC", "UPWK", "URBN", "UWMC", "VAL",
    "VC", "VCX", "VECO", "VELO", "VGNT", "VIST", "VITL", "VKTX", "VNT", "VOYG",
    "VRDN", "VRNS", "VSCO", "VSEC", "VSNT", "VVV", "VVX", "WAY", "WEN", "WERN",
    "WEX", "WGS", "WK", "WLDN", "WMG", "WOK", "WRBY", "WSC", "WSM", "WT",
    "WU", "WYFI", "XMTR", "XP", "XRAY", "YETI", "YOU", "YSS", "ZG",
    # Added 2026-05-21 for MAG7 theme
    "GOOGL", "META", "AMZN", "AAPL", "MSFT",
    # Added 2026-05-21 from TC2000 ETF watchlist (filtered: 23 of 116;
    # inverse + redundant-leveraged + no-existing-theme-fit removed)
    "SOXL", "USD", "XSD", "PSI", "TSMX",       # semis -> ai_compute
    "NUGT", "JNUG", "GDXU", "SIL", "SILJ",     # gold/silver -> gold_silver_miners
    "URA", "URNM",                             # uranium -> uranium_pure_plays
    "NLR",                                     # nuclear -> nuclear_renaissance
    "DRAM",                                    # memory -> memory_cycle
    "NASA", "UFO",                             # space -> space_economy
    "LABU", "ARKG",                            # biotech/genomics -> biotech
    "NAIL",                                    # homebuilders -> building_products
    "AMZU",                                    # AMZN proxy -> ecommerce
    "GGLL",                                    # GOOGL proxy -> ad_tech_marketing
    "METU",                                    # META proxy -> social_media
    "MSFU",                                    # MSFT proxy -> cloud_infra
]


# ═══════════════════════════════════════════════════════════════════════════
#  NARRATIVE MAP  (the "Map" Themes sub-view + the synthetic narrative themes)
#
#  Arranges every theme into its place in the AI-market story so leadership can
#  be read in story-space. Four dicts, hand-edited like THEME_NARRATIVES, purely
#  additive — no existing consumer touches these. See NARRATIVE_MAP.md.
#
#  Benchmark for the Map's strength + money-flow is SPY (the yardstick the
#  2026-06-06 research validated), NOT the equal-weight universe the other
#  sub-views use. That is deliberate — see NARRATIVE_MAP.md.
# ═══════════════════════════════════════════════════════════════════════════

# Top-level story bands (display order). "Crypto" is shown as its own band even
# though it is conceptually under Adjacent — the miners-turned-datacenters left
# it, so what remains (exchanges + treasuries) reads cleanest standalone.
MACROTHEMES = {
    "buildout": {"label": "BUILDOUT — the AI build",        "order": 1},
    "output":   {"label": "OUTPUT — what AI enables",       "order": 2},
    "adjacent": {"label": "ADJACENT — real, off-build",     "order": 3},
    "crypto":   {"label": "CRYPTO — exchanges & treasuries","order": 4},
    "noise":    {"label": "NOISE — off-narrative",          "order": 5},
}

# Story-zones (the regions drawn on the map). Buildout keeps its chain-walk
# (Hub -> Infrastructure -> Power -> Materials, the order money walked the chain
# 2023->2025). order = top-to-bottom paint order across the whole map.
NARRATIVE_ZONES = {
    "hub":            {"label": "Hub — AI compute & datacenters",                 "macro": "buildout", "order": 1},
    "infrastructure": {"label": "AI Infrastructure — chips, optics, memory, net", "macro": "buildout", "order": 2},
    "power":          {"label": "Power — nuclear, grid, storage, cooling",        "macro": "buildout", "order": 3},
    "materials":      {"label": "Raw Materials — critical minerals",              "macro": "buildout", "order": 4},
    "output":         {"label": "What AI Enables",                                "macro": "output",   "order": 5},
    "adjacent":       {"label": "Adjacent — solar, EV supply, reshoring, metals", "macro": "adjacent", "order": 6},
    "crypto":         {"label": "Crypto — exchanges & treasuries",                "macro": "crypto",   "order": 7},
    "noise":          {"label": "Noise — off-narrative quarantine",              "macro": "noise",    "order": 8},
}

# Every theme in THEMES -> its zone. Author-assigned (Dan does not hand-classify).
# Settled 2026-06-06 + co-movement-audited 2026-06-07:
#  - crypto_miners -> hub  (the complex repurposed to AI datacenters; trades as
#    infra, not Bitcoin — confirmed by co-movement while BTC sold off).
#  - the four orphan-SaaS (data_analytics_terminals / productivity_saas /
#    vertical_saas / ad_tech_marketing) -> noise  (near-zero-to-negative
#    co-movement with the AI core + weak RS; the AI-disrupted SaaS the research
#    calls noise).
THEME_CHAIN_POSITION = {
    # ── BUILDOUT / Hub ──
    "mag7": "hub", "ai_compute": "hub", "datacenter_buildout": "hub",
    "ai_compute_clouds": "hub", "crypto_miners": "hub",
    # ── BUILDOUT / Infrastructure ──
    "ai_optics": "infrastructure", "ai_networking": "infrastructure",
    "ai_connectivity": "infrastructure", "semiconductor_ip": "infrastructure",
    "memory_cycle": "infrastructure", "semi_equipment": "infrastructure",
    "semiconductor_testing": "infrastructure", "electronic_components": "infrastructure",
    # ── BUILDOUT / Power ──
    "power_demand_for_ai": "power", "nuclear_renaissance": "power",
    "uranium_pure_plays": "power", "energy_storage": "power",
    "electrical_grid": "power", "hvac_cooling": "power", "fuel_cells_hydrogen": "power",
    # ── BUILDOUT / Materials ──
    "critical_minerals": "materials",
    # ── OUTPUT (one group) ──
    "quantum_computing": "output", "robotics_automation": "output",
    "space_economy": "output", "drones": "output", "autonomous_mobility": "output",
    "ai_apps_platforms": "output", "defense_tech": "output", "biotech": "output",
    "diagnostics_tools": "output", "cybersecurity": "output", "cloud_infra": "output",
    "data_devops_platforms": "output",
    # ── ADJACENT ──
    "solar": "adjacent", "ev_supply_chain": "adjacent", "reshoring_construction": "adjacent",
    "building_products": "adjacent", "industrial_distribution": "adjacent",
    "gold_silver_miners": "adjacent", "industrial_metals": "adjacent",
    "industrial_materials": "adjacent",
    # ── CRYPTO (what's left once the miners leave) ──
    "crypto_platforms": "crypto", "bitcoin_treasury": "crypto",
    # ── NOISE ──
    "ev_makers": "noise", "fintech_disruptors": "noise",
    "financial_services_specialty": "noise", "alt_asset_managers": "noise",
    "managed_care": "noise", "medtech_devices_consumer_health": "noise",
    "transports": "noise", "airlines": "noise", "oil_gas_ep": "noise",
    "oil_gas_services": "noise", "energy_drinks_wellness": "noise",
    "restaurants_fast_casual": "noise", "footwear_apparel": "noise", "beauty": "noise",
    "discount_retail": "noise", "ecommerce": "noise", "travel_services": "noise",
    "streaming_media": "noise", "gaming_betting": "noise", "social_media": "noise",
    "real_estate_proptech": "noise", "auto_parts_tech": "noise", "consulting_govt": "noise",
    "consumer_retail": "noise", "latam_em": "noise", "china": "noise",
    # the four orphan-SaaS, moved out of Output by the co-movement audit:
    "data_analytics_terminals": "noise", "productivity_saas": "noise",
    "vertical_saas": "noise", "ad_tech_marketing": "noise",
}

# Per-ticker zone fixes for the map only (overrides the ticker's theme-derived
# zone). A list = the ticker straddles those zones (cross-listed into each).
# Drives the region money-flow composites + the Crypto<->Hub straddle bridge.
TICKER_ZONE_OVERRIDE = {
    "TSLA": ["output"],  # robotaxi / Optimus / FSD = AI application, not the compute build
    # crypto miners that genuinely repurposed to AI datacenters straddle both:
    # they still track Bitcoin day-to-day but rip as AI infra (co-movement, 2026-06-07).
    "MARA": ["hub", "crypto"],
    "CLSK": ["hub", "crypto"],
    "HIVE": ["hub", "crypto"],
    "GLXY": ["hub", "crypto"],  # Galaxy: trading/asset-mgmt + Helios CoreWeave AI datacenter
    "BTBT": ["hub", "crypto"],  # Bit Digital: WhiteFiber AI cloud + ETH treasury
}

# Narrative priority for deduping a ticker into ONE primary zone where a single
# placement is needed (lower index = higher priority). Buildout > Output >
# Adjacent > Crypto > Noise (the research's connected-first ordering).
NARRATIVE_ZONE_PRIORITY = [
    "hub", "infrastructure", "power", "materials",  # buildout
    "output", "adjacent", "crypto", "noise",
]
